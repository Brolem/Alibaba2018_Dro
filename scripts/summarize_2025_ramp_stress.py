"""汇总统一2025比较的爬坡边界压力实验。"""

from __future__ import annotations

import argparse
import csv
from pathlib import Path
from typing import Sequence


METHODS = (
    ("deterministic", "deterministic"),
    ("saa_n20", "saa"),
    ("gamma_ro_gamma_0p5", "gamma_ro"),
    ("tv_dro_rho_0p01", "tv_dro"),
)


def _read(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8", newline="") as input_file:
        return list(csv.DictReader(input_file))


def _write(path: Path, rows: Sequence[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as output_file:
        writer = csv.DictWriter(
            output_file, fieldnames=list(rows[0]), lineterminator="\n"
        )
        writer.writeheader()
        writer.writerows(rows)


def summarize(
    baseline_directory: Path,
    stress_root: Path,
) -> list[dict[str, object]]:
    levels = (
        (0.10, baseline_directory),
        (0.075, stress_root / "ramp_0p075"),
        (0.06, stress_root / "ramp_0p06"),
        (0.05, stress_root / "ramp_0p05"),
    )
    output: list[dict[str, object]] = []
    for fraction, directory in levels:
        overall = {row["configuration"]: row for row in _read(directory / "overall_summary.csv")}
        failures = {row["configuration"]: row for row in _read(directory / "failures.csv")}
        replay = _read(directory / "replay_runs.csv")
        for configuration, method in METHODS:
            completed = overall.get(configuration)
            failure = failures.get(configuration)
            method_rows = [row for row in replay if row["configuration"] == configuration]
            if completed is not None:
                status = "complete"
            elif failure is not None:
                status = "infeasible"
            else:
                status = "not_run_after_prior_method_infeasible"
            output.append(
                {
                    "ramp_limit_fraction_of_peak": fraction,
                    "configuration": configuration,
                    "method": method,
                    "status": status,
                    "completed_energy_windows": (
                        int(completed["energy_window_count"]) if completed else 0
                    ),
                    "maximum_decomposition_iterations": max(
                        (int(row["decomposition_iterations"]) for row in method_rows),
                        default=0,
                    ),
                    "mean_nominal_operating_cost_usd": (
                        completed["mean_nominal_operating_cost_usd_across_windows"]
                        if completed else ""
                    ),
                    "mean_actual_operating_cost_usd": (
                        completed["mean_actual_operating_cost_usd_across_windows"]
                        if completed else ""
                    ),
                    "mean_actual_curtailment_mwh": (
                        completed["mean_actual_curtailment_mwh_across_windows"]
                        if completed else ""
                    ),
                    "mean_batch_adjustment_mwh": (
                        completed["mean_batch_adjustment_mwh_across_windows"]
                        if completed else ""
                    ),
                    "workload_violation_rate": (
                        completed["workload_mean_conditional_violation_rate_across_windows"]
                        if completed else ""
                    ),
                    "grid_limit_violation_rate": (
                        completed["grid_limit_mean_conditional_violation_rate_across_windows"]
                        if completed else ""
                    ),
                    "ramp_violation_rate": (
                        completed["ramp_mean_conditional_violation_rate_across_windows"]
                        if completed else ""
                    ),
                    "total_solve_wall_time_seconds": (
                        completed["total_solve_wall_time_seconds"] if completed else ""
                    ),
                    "failure_detail": failure["error"] if failure else "",
                }
            )
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--baseline-directory",
        type=Path,
        default=Path("data/results/comparison/unified_2025"),
    )
    parser.add_argument(
        "--stress-root",
        type=Path,
        default=Path("data/results/comparison/ramp_stress_2025"),
    )
    args = parser.parse_args()
    output = args.stress_root / "ramp_stress_summary.csv"
    rows = summarize(args.baseline_directory, args.stress_root)
    _write(output, rows)
    print("written:", output)


if __name__ == "__main__":
    main()
