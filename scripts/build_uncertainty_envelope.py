"""从 batch_task.csv 构造算力侧不确定集与逐小时柔性包络。

只用 batch_task（不依赖 usage / batch_instance）。其中：
- 到达率/时长不确定集：由经验分布给出名义值、波动与分位数；
- 逐小时柔性包络：给出观测基准功率/能量，以及带 slack 的可调度窗口能量上界。

slack 是“deadline = end_time + slack_ratio * duration”的占位参数；
真正的“标定 slack”需要 batch_instance 的真实调度延迟，当前未标定。
"""

from __future__ import annotations

import argparse
import csv
import json
from collections import defaultdict
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RAW_WORKLOAD = ROOT / "data" / "raw" / "workload"
PROCESSED_WORKLOAD = ROOT / "data" / "processed" / "workload"
SECONDS_PER_HOUR = 3600


def _quantiles_from_hist(hist: dict[int, int], qs=()):
    """从以小时为 bin 的直方图近似计算多个分位数（小时）。"""
    total = sum(hist.values())
    if total == 0:
        return {q: 0.0 for q in qs}
    out = {}
    cum = 0
    qs = sorted(qs)
    qi = 0
    for hour in sorted(hist):
        cum += hist[hour]
        while qi < len(qs) and cum >= total * qs[qi]:
            out[qs[qi]] = float(hour)
            qi += 1
        if qi >= len(qs):
            break
    return out


def _welford_summary(rec: dict) -> dict:
    """把 Welford 在线统计的原始累计量汇总为均值/标准差/最小/最大（小时）。"""
    n = rec["n"]
    if n == 0:
        return {}
    std = (rec["m2"] / n) ** 0.5
    return {
        "n": n,
        "mean_hours": rec["mean"] / SECONDS_PER_HOUR,
        "std_hours": std / SECONDS_PER_HOUR,
        "min_hours": rec["mn"] / SECONDS_PER_HOUR,
        "max_hours": rec["mx"] / SECONDS_PER_HOUR,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--slack-ratio", type=float, default=0.5)
    ap.add_argument(
        "--out-unc",
        type=Path,
        default=PROCESSED_WORKLOAD / "compute_uncertainty.json",
    )
    ap.add_argument(
        "--out-env",
        type=Path,
        default=PROCESSED_WORKLOAD / "hourly_flexibility_envelope.csv",
    )
    args = ap.parse_args()

    dur_rec = defaultdict(lambda: {"n": 0, "mean": 0.0, "m2": 0.0, "mn": 1e18, "mx": -1.0})
    dur_hist = defaultdict(lambda: defaultdict(int))
    arrival_tasks = defaultdict(int)
    arrival_instances = defaultdict(int)
    arrival_cores = defaultdict(float)
    base_cores = defaultdict(float)
    base_energy = defaultdict(float)
    window_energy = defaultdict(float)
    max_hour = 0

    def add_duration(key: str, dsec: int) -> None:
        r = dur_rec[key]
        r["n"] += 1
        delta = dsec - r["mean"]
        r["mean"] += delta / r["n"]
        r["m2"] += delta * (dsec - r["mean"])
        if dsec < r["mn"]:
            r["mn"] = dsec
        if dsec > r["mx"]:
            r["mx"] = dsec
        dur_hist[key][dsec // 60] += 1

    with (RAW_WORKLOAD / "batch_task.csv").open("r", encoding="utf-8", newline="") as f:
        for line in f:
            p = line.rstrip("\r\n").split(",")
            instance_num = int(p[1]) if p[1] else 0
            task_type = p[3]
            start = int(p[5])
            end = int(p[6])
            plan_cpu = float(p[7]) if p[7] else 0.0

            cores = (plan_cpu / 100.0) * instance_num
            duration = end - start
            if duration < 0:
                duration = 0

            hour_arrival = start // SECONDS_PER_HOUR
            arrival_tasks[hour_arrival] += 1
            arrival_instances[hour_arrival] += instance_num
            arrival_cores[hour_arrival] += cores
            max_hour = max(max_hour, hour_arrival)

            add_duration("overall", duration)
            add_duration(task_type, duration)

            if duration <= 0:
                continue

            h0 = start // SECONDS_PER_HOUR
            h1 = (end - 1) // SECONDS_PER_HOUR
            max_hour = max(max_hour, h1)
            for h in range(h0, h1 + 1):
                base_cores[h] += cores
                overlap = min(end, (h + 1) * SECONDS_PER_HOUR) - max(
                    start, h * SECONDS_PER_HOUR
                )
                if overlap > 0:
                    base_energy[h] += cores * overlap / SECONDS_PER_HOUR

            deadline = end + int(args.slack_ratio * duration)
            hw0 = start // SECONDS_PER_HOUR
            hw1 = deadline // SECONDS_PER_HOUR
            max_hour = max(max_hour, hw1)
            energy = cores * duration / SECONDS_PER_HOUR
            for h in range(hw0, hw1 + 1):
                window_energy[h] += energy

    # 到达率不确定集
    def series_stats(series: dict[int, float]) -> dict:
        vals = list(series.values())
        if not vals:
            return {}
        n = len(vals)
        mean = sum(vals) / n
        var = sum((v - mean) ** 2 for v in vals) / n
        srt = sorted(vals)
        q = lambda p: srt[int(p * (n - 1))]
        return {
            "mean": mean,
            "std": var**0.5,
            "min": srt[0],
            "max": srt[-1],
            "p50": q(0.50),
            "p90": q(0.90),
        }

    duration_out = {
        key: {
            **_welford_summary(rec),
            "quantiles_minutes": _quantiles_from_hist(
                dur_hist[key], (0.05, 0.25, 0.50, 0.75, 0.95)
            ),
        }
        for key, rec in dur_rec.items()
    }

    uncertainty = {
        "note": (
            "由 batch_task 经验分布构造；到达率为逐小时经验值，"
            "时长为任务级经验分布，均未使用 usage/batch_instance。"
        ),
        "slack_ratio": args.slack_ratio,
        "slack_note": "deadline = end_time + slack_ratio * duration（占位，未标定）",
        "horizon_hours": max_hour + 1,
        "arrival": {
            "tasks_per_hour": series_stats(arrival_tasks),
            "instances_per_hour": series_stats(arrival_instances),
            "arriving_cores_per_hour": series_stats(arrival_cores),
            "hourly_nominal": {
                "tasks": {str(h): arrival_tasks.get(h, 0) for h in range(max_hour + 1)},
                "instances": {str(h): arrival_instances.get(h, 0) for h in range(max_hour + 1)},
                "cores": {str(h): arrival_cores.get(h, 0.0) for h in range(max_hour + 1)},
            },
        },
        "duration": duration_out,
    }

    args.out_unc.parent.mkdir(parents=True, exist_ok=True)
    args.out_unc.write_text(
        json.dumps(uncertainty, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    with args.out_env.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(
            [
                "hour",
                "baseline_cores",
                "baseline_energy_core_hours",
                "flexible_window_energy_core_hours",
            ]
        )
        for h in range(max_hour + 1):
            w.writerow(
                [
                    h,
                    round(base_cores.get(h, 0.0), 6),
                    round(base_energy.get(h, 0.0), 6),
                    round(window_energy.get(h, 0.0), 6),
                ]
            )

    print(f"written: {args.out_unc}")
    print(f"written: {args.out_env}")
    print("horizon_hours:", max_hour + 1)
    print("arrival tasks/hour:", uncertainty["arrival"]["tasks_per_hour"])
    print("duration overall:", duration_out.get("overall"))


if __name__ == "__main__":
    main()
