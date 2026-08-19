"""Generate an extended hourly flexibility envelope by resampling the 8-day trace.

Method 2 (task-level Monte Carlo): fit the empirical hourly arrival rate, then
simulate ``--days`` days with Poisson arrivals; each arrival resamples a real
task record (duration + energy) so the joint distribution is preserved and the
8-day periodicity of naive rolling is removed.

Dependencies: numpy only.
"""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

import numpy as np


ROOT = Path(__file__).resolve().parents[1]
WORKLOAD = ROOT / "data" / "workload"
SECONDS_PER_HOUR = 3600
TRACE_TASKS = 14_295_731


def _load_arrival_rate(uncertainty_path: Path) -> np.ndarray:
    payload = json.loads(uncertainty_path.read_text(encoding="utf-8"))
    hourly = payload["arrival"]["hourly_nominal"]["tasks"]
    return np.array([float(hourly[str(h)]) for h in range(len(hourly))])


def _build_task_pool(
    batch_task_path: Path, *, max_tasks: int, seed: int
) -> tuple[np.ndarray, np.ndarray]:
    """Uniformly subsample real task records into (duration_minutes, energy)."""

    rng = np.random.default_rng(seed)
    durations: list[int] = []
    energies: list[float] = []
    keep_prob = max_tasks / TRACE_TASKS

    with batch_task_path.open("r", encoding="utf-8", newline="") as f:
        for line in f:
            p = line.rstrip("\r\n").split(",")
            if rng.random() >= keep_prob:
                continue
            instance_num = int(p[1]) if p[1] else 0
            start = int(p[5])
            end = int(p[6])
            plan_cpu = float(p[7]) if p[7] else 0.0
            duration = end - start
            if duration < 0:
                duration = 0
            cores = (plan_cpu / 100.0) * instance_num
            energy = cores * duration / SECONDS_PER_HOUR
            durations.append(duration // 60)
            energies.append(energy)
            if len(durations) >= max_tasks:
                break

    return np.asarray(durations, dtype=np.int64), np.asarray(energies, dtype=np.float64)


def generate_envelope(
    *,
    days: int,
    seed: int,
    slack_ratio: float,
    arrival_rate: np.ndarray,
    durations: np.ndarray,
    energies: np.ndarray,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    rng = np.random.default_rng(seed)
    hours = days * 24
    base_rate = arrival_rate[:192]  # 8-day core; ignore the settlement tail
    counts = rng.poisson(np.tile(base_rate, days)[:hours])

    baseline_energy = np.zeros(hours)
    baseline_cores = np.zeros(hours)
    window_energy = np.zeros(hours)

    for h in range(hours):
        count = int(counts[h])
        if count == 0:
            continue
        idx = rng.integers(0, len(durations), size=count)
        hour_durations = durations[idx]
        hour_energies = energies[idx]

        baseline_energy[h] = hour_energies.sum()
        baseline_cores[h] = hour_energies.sum()

        window_hours = np.clip(
            np.ceil((1.0 + slack_ratio) * hour_durations / 60.0).astype(int),
            1,
            None,
        )
        short = window_hours == 1
        window_energy[h] += hour_energies[short].sum()
        for index in np.flatnonzero(~short):
            for offset in range(int(window_hours[index])):
                target = h + offset
                if target < hours:
                    window_energy[target] += hour_energies[index]

    return baseline_cores, baseline_energy, window_energy


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--slack-ratio", type=float, default=0.5)
    parser.add_argument("--pool-size", type=int, default=3_000_000)
    parser.add_argument(
        "--out",
        type=Path,
        default=WORKLOAD / "generated_envelope_30d.csv",
    )
    args = parser.parse_args()

    arrival_rate = _load_arrival_rate(WORKLOAD / "compute_uncertainty.json")
    durations, energies = _build_task_pool(
        WORKLOAD / "batch_task.csv",
        max_tasks=args.pool_size,
        seed=args.seed,
    )
    baseline_cores, baseline_energy, window_energy = generate_envelope(
        days=args.days,
        seed=args.seed,
        slack_ratio=args.slack_ratio,
        arrival_rate=arrival_rate,
        durations=durations,
        energies=energies,
    )

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(
            [
                "hour",
                "baseline_cores",
                "baseline_energy_core_hours",
                "flexible_window_energy_core_hours",
            ]
        )
        for h in range(len(baseline_energy)):
            writer.writerow(
                [
                    h,
                    round(float(baseline_cores[h]), 6),
                    round(float(baseline_energy[h]), 6),
                    round(float(window_energy[h]), 6),
                ]
            )

    print(f"written: {args.out}")
    print("pool_size:", len(durations))
    print("hours:", len(baseline_energy))
    print("total_baseline_energy_core_hours:", round(float(baseline_energy.sum()), 2))
    print("total_window_energy_core_hours:", round(float(window_energy.sum()), 2))


if __name__ == "__main__":
    main()
