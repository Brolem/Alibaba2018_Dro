"""汇总2025并网-爬坡边界搜索，并配对比较确定性与TV-DRO。"""

from __future__ import annotations

import argparse
import csv
import json
import math
from pathlib import Path
from typing import Sequence


def _read(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8", newline="") as input_file:
        return list(csv.DictReader(input_file))


def _write(path: Path, rows: Sequence[dict[str, object]]) -> None:
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as output_file:
        writer = csv.DictWriter(
            output_file, fieldnames=list(rows[0]), lineterminator="\n"
        )
        writer.writeheader()
        writer.writerows(rows)


def _as_bool(value: object) -> bool:
    return str(value).strip().lower() == "true"


def _wilson(successes: int, total: int, z: float = 1.959963984540054) -> tuple[float, float]:
    if total <= 0:
        return 0.0, 0.0
    rate = successes / total
    denominator = 1.0 + z * z / total
    centre = (rate + z * z / (2.0 * total)) / denominator
    half_width = (
        z
        * math.sqrt(rate * (1.0 - rate) / total + z * z / (4.0 * total * total))
        / denominator
    )
    lower = 0.0 if successes == 0 else max(0.0, centre - half_width)
    upper = 1.0 if successes == total else min(1.0, centre + half_width)
    return lower, upper


def _mcnemar_exact(det_only: int, tv_only: int) -> float:
    discordant = det_only + tv_only
    if discordant == 0:
        return 1.0
    tail = sum(
        math.comb(discordant, index)
        for index in range(min(det_only, tv_only) + 1)
    ) / (2.0**discordant)
    return min(1.0, 2.0 * tail)


def _physical_violation(row: dict[str, str]) -> bool:
    return _as_bool(row["grid_limit_violation"]) or _as_bool(row["ramp_violation"])


def _physical_magnitude(row: dict[str, str]) -> float:
    return max(float(row["grid_limit_violation_mw"]), float(row["ramp_violation_mw"]))


def _boundary(directory: Path) -> tuple[float, float]:
    payload = json.loads((directory / "run_config.json").read_text(encoding="utf-8"))
    parameters = payload["parameters"]
    return (
        float(parameters.get("grid_limit_fraction_of_peak", 1.0)),
        float(parameters.get("ramp_limit_fraction_of_peak", 0.10)),
    )


def summarize(search_root: Path, peak_load_mw: float) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    summaries: list[dict[str, object]] = []
    pairs: list[dict[str, object]] = []
    for directory in sorted(path for path in search_root.iterdir() if path.is_dir()):
        config_path = directory / "run_config.json"
        if not config_path.exists():
            continue
        grid_fraction, ramp_fraction = _boundary(directory)
        replay = _read(directory / "replay_runs.csv")
        failures = _read(directory / "failures.csv")
        grouped: dict[tuple[str, str], list[dict[str, str]]] = {}
        for row in replay:
            grouped.setdefault((row["method"], row["window"]), []).append(row)
        for (method, window), rows in sorted(grouped.items()):
            first = rows[0]
            physical_count = sum(_physical_violation(row) for row in rows)
            summaries.append(
                {
                    "directory": directory.name,
                    "grid_limit_fraction_of_peak": grid_fraction,
                    "ramp_limit_fraction_of_peak": ramp_fraction,
                    "configuration": first["configuration"],
                    "method": method,
                    "window": window,
                    "status": "complete",
                    "replay_count": len(rows),
                    "nominal_operating_cost_usd": first["nominal_operating_cost_usd"],
                    "mean_actual_operating_cost_usd": sum(float(row["actual_operating_cost_usd"]) for row in rows) / len(rows),
                    "mean_batch_adjustment_mwh": sum(float(row["batch_adjustment_mwh"]) for row in rows) / len(rows),
                    "mean_actual_curtailment_mwh": sum(float(row["actual_curtailment_mwh"]) for row in rows) / len(rows),
                    "workload_violation_rate": sum(_as_bool(row["workload_violation"]) for row in rows) / len(rows),
                    "workload_max_violation_mwh": max(float(row["workload_envelope_violation_mwh"]) for row in rows),
                    "grid_violation_rate": sum(_as_bool(row["grid_limit_violation"]) for row in rows) / len(rows),
                    "grid_max_violation_mw": max(float(row["grid_limit_violation_mw"]) for row in rows),
                    "ramp_violation_rate": sum(_as_bool(row["ramp_violation"]) for row in rows) / len(rows),
                    "ramp_max_violation_mw": max(float(row["ramp_violation_mw"]) for row in rows),
                    "physical_union_violation_rate": physical_count / len(rows),
                    "physical_union_max_violation_mw": max(_physical_magnitude(row) for row in rows),
                    "solve_wall_time_seconds": first["solve_wall_time_seconds"],
                }
            )
        for failure in failures:
            summaries.append(
                {
                    "directory": directory.name,
                    "grid_limit_fraction_of_peak": grid_fraction,
                    "ramp_limit_fraction_of_peak": ramp_fraction,
                    "configuration": failure["configuration"],
                    "method": failure["method"],
                    "window": failure["window"],
                    "status": "infeasible",
                    "replay_count": 0,
                    "nominal_operating_cost_usd": "",
                    "mean_actual_operating_cost_usd": "",
                    "mean_batch_adjustment_mwh": "",
                    "mean_actual_curtailment_mwh": "",
                    "workload_violation_rate": "",
                    "workload_max_violation_mwh": "",
                    "grid_violation_rate": "",
                    "grid_max_violation_mw": "",
                    "ramp_violation_rate": "",
                    "ramp_max_violation_mw": "",
                    "physical_union_violation_rate": "",
                    "physical_union_max_violation_mw": "",
                    "solve_wall_time_seconds": "",
                }
            )
        windows = sorted({window for _, window in grouped})
        for window in windows:
            det = {row["workload_replay_id"]: row for row in grouped.get(("deterministic", window), [])}
            tv = {row["workload_replay_id"]: row for row in grouped.get(("tv_dro", window), [])}
            common_ids = sorted(set(det) & set(tv), key=int)
            if not common_ids:
                continue
            det_count = sum(_physical_violation(det[item]) for item in common_ids)
            tv_count = sum(_physical_violation(tv[item]) for item in common_ids)
            det_only = sum(_physical_violation(det[item]) and not _physical_violation(tv[item]) for item in common_ids)
            tv_only = sum(_physical_violation(tv[item]) and not _physical_violation(det[item]) for item in common_ids)
            det_max = max(_physical_magnitude(det[item]) for item in common_ids)
            tv_max = max(_physical_magnitude(tv[item]) for item in common_ids)
            det_low, det_high = _wilson(det_count, len(common_ids))
            tv_low, tv_high = _wilson(tv_count, len(common_ids))
            risk_difference = (det_count - tv_count) / len(common_ids)
            magnitude_ratio = tv_max / det_max if det_max > 0.0 else 0.0
            pairs.append(
                {
                    "directory": directory.name,
                    "grid_limit_fraction_of_peak": grid_fraction,
                    "ramp_limit_fraction_of_peak": ramp_fraction,
                    "window": window,
                    "paired_replay_count": len(common_ids),
                    "deterministic_physical_violation_rate": det_count / len(common_ids),
                    "deterministic_wilson_low": det_low,
                    "deterministic_wilson_high": det_high,
                    "tv_dro_physical_violation_rate": tv_count / len(common_ids),
                    "tv_dro_wilson_low": tv_low,
                    "tv_dro_wilson_high": tv_high,
                    "paired_risk_reduction_percentage_points": 100.0 * risk_difference,
                    "deterministic_only_violation_count": det_only,
                    "tv_dro_only_violation_count": tv_only,
                    "mcnemar_exact_two_sided_p": _mcnemar_exact(det_only, tv_only),
                    "deterministic_max_physical_violation_mw": det_max,
                    "tv_dro_max_physical_violation_mw": tv_max,
                    "tv_to_deterministic_max_magnitude_ratio": magnitude_ratio,
                    "material_magnitude_floor_mw": 0.005 * peak_load_mw,
                    "valuable_candidate": (
                        0.08 <= det_count / len(common_ids) <= 0.20
                        and tv_count / len(common_ids) <= 0.02
                        and risk_difference >= 0.06
                        and det_max >= 0.005 * peak_load_mw
                        and magnitude_ratio <= 0.20
                    ),
                }
            )
    return summaries, pairs


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--search-root",
        type=Path,
        default=Path("data/results/comparison/boundary_search_2025"),
    )
    parser.add_argument("--peak-load-mw", type=float, default=1.70202528)
    args = parser.parse_args()
    summaries, pairs = summarize(args.search_root, args.peak_load_mw)
    _write(args.search_root / "boundary_search_summary.csv", summaries)
    _write(args.search_root / "boundary_pairwise.csv", pairs)
    print("summary_rows:", len(summaries))
    print("pairwise_rows:", len(pairs))


if __name__ == "__main__":
    main()
