"""运行静态 Γ-RO 三折校准；按由松到紧的分数预算自适应停止。"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import statistics
import sys
from pathlib import Path
from statistics import NormalDist


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from alibaba2018_dro.config import (
    PV_CAPACITY_FRACTION_OF_MUST_LOAD,
    WIND_CAPACITY_FRACTION_OF_MUST_LOAD,
)
from alibaba2018_dro.inputs import build_hourly_input_from_rows
from alibaba2018_dro.scenarios import (
    load_calibration_energy_rows,
    load_hourly_downward_residual_quantiles,
    load_saa_scenarios,
)
from alibaba2018_dro.scheduler import (
    _peak_load,
    replay_joint_scenario_with_batch_recourse,
    solve_static_gamma_ro_wind_solar_storage,
)


PROCESSED = PROJECT_ROOT / "data" / "processed"
RESULTS = PROJECT_ROOT / "data" / "results"
HELD_OUT_FOLDS = ("fold_1", "fold_2", "fold_3")
CANONICAL_WINDOWS_PER_FOLD = 12
RUN_FIELDS = (
    "method",
    "held_out_fold",
    "validation_window_id",
    "gamma",
    "effective_gamma",
    "gamma_saturated",
    "sample_size",
    "energy_quantile",
    "feasible",
    "solver_status",
    "solver_runtime_seconds",
    "mip_gap",
    "max_robust_constraint_violation_mw",
    "nominal_operating_cost_usd",
    "actual_operating_cost_usd",
    "actual_carbon_kg",
    "actual_curtailment_mwh",
    "training_mean_batch_adjustment_mwh",
    "validation_batch_adjustment_mwh",
    "workload_envelope_violation_mwh",
    "grid_limit_violation_mw",
    "ramp_violation_mw",
    "workload_violation",
    "grid_limit_violation",
    "ramp_violation",
)
RISK_COLUMNS = (
    "workload_violation",
    "grid_limit_violation",
    "ramp_violation",
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as input_file:
        for block in iter(lambda: input_file.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def _as_bool(value: object) -> bool:
    return str(value).strip().lower() == "true"


def _wilson_upper(violations: int, total: int) -> float:
    z = NormalDist().inv_cdf(0.95)
    rate = violations / total
    denominator = 1.0 + z * z / total
    center = (rate + z * z / (2.0 * total)) / denominator
    margin = (
        z
        * math.sqrt(rate * (1.0 - rate) / total + z * z / (4.0 * total * total))
        / denominator
    )
    return min(1.0, center + margin)


def _read_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as input_file:
        return list(csv.DictReader(input_file))


def _append_row(path: Path, row: dict[str, object]) -> None:
    with path.open("a", newline="", encoding="utf-8") as output_file:
        writer = csv.DictWriter(output_file, fieldnames=RUN_FIELDS)
        if output_file.tell() == 0:
            writer.writeheader()
        writer.writerow(row)


def _write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    if not rows:
        return
    with path.open("w", newline="", encoding="utf-8") as output_file:
        writer = csv.DictWriter(output_file, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def summarize_runs(
    rows: list[dict[str, str]],
    *,
    gammas: tuple[float, ...],
    expected_windows: int,
) -> list[dict[str, object]]:
    summaries: list[dict[str, object]] = []
    for gamma in gammas:
        selected = [row for row in rows if float(row["gamma"]) == gamma]
        if len(selected) != expected_windows:
            continue
        feasible_rows = [row for row in selected if _as_bool(row["feasible"])]
        summary: dict[str, object] = {
            "method": "static_Gamma_RO",
            "gamma": gamma,
            "effective_gamma": min(2.0, gamma),
            "validation_window_count": len(selected),
            "optimal_solve_count": len(feasible_rows),
            "canonical_36_window_validation": expected_windows == 36,
            "mean_solver_runtime_seconds": statistics.fmean(
                float(row["solver_runtime_seconds"]) for row in selected
            ),
            "mean_nominal_operating_cost_usd": (
                statistics.fmean(
                    float(row["nominal_operating_cost_usd"]) for row in feasible_rows
                )
                if feasible_rows
                else None
            ),
            "mean_actual_operating_cost_usd": (
                statistics.fmean(
                    float(row["actual_operating_cost_usd"]) for row in feasible_rows
                )
                if feasible_rows
                else None
            ),
        }
        infeasible = expected_windows - len(feasible_rows)
        upper_bounds: list[float] = []
        for column in RISK_COLUMNS:
            count = infeasible + sum(_as_bool(row[column]) for row in feasible_rows)
            prefix = column.removesuffix("_violation")
            upper = _wilson_upper(count, expected_windows)
            summary[f"{prefix}_violation_count"] = count
            summary[f"{prefix}_violation_rate"] = count / expected_windows
            summary[f"{prefix}_wilson_upper_95"] = upper
            upper_bounds.append(upper)
        summary["max_wilson_upper_95"] = max(upper_bounds)
        summary["meets_90pct_target"] = (
            expected_windows == 36
            and len(feasible_rows) == expected_windows
            and max(upper_bounds) <= 0.10
        )
        summaries.append(summary)
    return summaries


def _expand_daily(values: tuple[float, ...], hours: int) -> tuple[float, ...]:
    if len(values) != 24:
        raise ValueError("hour-position deviations must contain 24 values")
    return tuple(values[t % 24] for t in range(hours))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--manifest",
        type=Path,
        default=PROCESSED / "scenarios" / "saa_scenarios_manifest.json",
    )
    parser.add_argument(
        "--calibration-csv",
        type=Path,
        default=PROCESSED / "scenarios" / "calibration_day_blocks_2024.csv",
    )
    parser.add_argument(
        "--aggregate-workload-csv",
        type=Path,
        default=PROCESSED / "workload" / "aggregate_workload_8d.csv",
    )
    parser.add_argument(
        "--envelope-csv",
        type=Path,
        default=PROCESSED / "workload" / "nominal_workload_30d.csv",
    )
    parser.add_argument(
        "--stats-json",
        type=Path,
        default=PROCESSED / "workload" / "workload_stats.json",
    )
    parser.add_argument(
        "--output-directory",
        type=Path,
        default=RESULTS / "calibration" / "gamma_ro",
    )
    parser.add_argument(
        "--gammas", type=float, nargs="+", default=[0.0, 0.5, 0.75, 1.0]
    )
    parser.add_argument("--sample-size", type=int, default=20)
    parser.add_argument("--energy-quantile", type=float, default=0.90)
    parser.add_argument(
        "--selection-mode", choices=("adaptive", "full_grid"), default="adaptive"
    )
    parser.add_argument(
        "--folds", nargs="+", choices=HELD_OUT_FOLDS, default=list(HELD_OUT_FOLDS)
    )
    parser.add_argument(
        "--max-windows", type=int, default=CANONICAL_WINDOWS_PER_FOLD
    )
    parser.add_argument("--time-limit-seconds", type=float, default=None)
    parser.add_argument(
        "--day-ahead-solver", choices=("scip", "gurobi"), default="gurobi"
    )
    parser.add_argument(
        "--recourse-solver", choices=("scip", "gurobi"), default="gurobi"
    )
    args = parser.parse_args()

    gammas = tuple(dict.fromkeys(args.gammas))
    if any(gamma < 0.0 for gamma in gammas):
        raise ValueError("gammas must be non-negative")
    if args.sample_size <= 0:
        raise ValueError("sample-size must be positive")
    if not 1 <= args.max_windows <= CANONICAL_WINDOWS_PER_FOLD:
        raise ValueError("max-windows must be in [1, 12]")

    args.output_directory.mkdir(parents=True, exist_ok=True)
    config_path = args.output_directory / "run_config.json"
    run_path = args.output_directory / "gamma_ro_cv_runs.csv"
    config = {
        "schema_version": 1,
        "method": "static_Gamma_RO",
        "parameters": {
            "candidate_order": list(gammas),
            "sample_size": args.sample_size,
            "energy_quantile": args.energy_quantile,
            "folds": list(args.folds),
            "max_windows_per_fold": args.max_windows,
            "selection_mode": args.selection_mode,
            "time_limit_seconds": args.time_limit_seconds,
            "day_ahead_solver": args.day_ahead_solver,
            "recourse_solver": args.recourse_solver,
        },
        "source_sha256": {
            "manifest": _sha256(args.manifest),
            "calibration_day_blocks": _sha256(args.calibration_csv),
            "aggregate_workload": _sha256(args.aggregate_workload_csv),
            "nominal_workload": _sha256(args.envelope_csv),
            "workload_stats": _sha256(args.stats_json),
        },
    }
    if config_path.exists():
        if json.loads(config_path.read_text(encoding="utf-8")) != config:
            raise ValueError("existing run_config.json differs; use another output directory")
    else:
        config_path.write_text(
            json.dumps(config, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    existing = _read_rows(run_path)
    completed = {
        (row["held_out_fold"], int(row["validation_window_id"]), float(row["gamma"]))
        for row in existing
    }
    expected_windows = len(args.folds) * args.max_windows
    for gamma in gammas:
        print("candidate_start:", f"Gamma={gamma:g}", flush=True)
        for held_out_fold in args.folds:
            workload_support = load_saa_scenarios(
                manifest_path=args.manifest,
                calibration_csv=args.calibration_csv,
                workload_csv=args.aggregate_workload_csv,
                split="training",
                held_out_fold=held_out_fold,
                scenario_count=args.sample_size,
            )
            validation = load_saa_scenarios(
                manifest_path=args.manifest,
                calibration_csv=args.calibration_csv,
                workload_csv=args.aggregate_workload_csv,
                split="validation",
                held_out_fold=held_out_fold,
            )
            solar_24, wind_24 = load_hourly_downward_residual_quantiles(
                args.calibration_csv,
                quantile=args.energy_quantile,
                held_out_fold=held_out_fold,
            )
            for validation_scenario in validation[: args.max_windows]:
                key = (held_out_fold, validation_scenario.scenario_id, gamma)
                if key in completed:
                    print("skip:", *key, flush=True)
                    continue
                energy_rows = load_calibration_energy_rows(
                    args.calibration_csv, validation_scenario.energy_delivery_dates
                )
                inputs = build_hourly_input_from_rows(
                    energy_rows, args.envelope_csv, args.stats_json
                )
                hours = len(inputs)
                p_must = inputs[0].online_mw + inputs[0].base_mw
                p_peak = _peak_load(inputs)
                g_max_mw = p_peak
                r_max_mw = 0.1 * p_peak
                bess_power_mw = 0.5 * p_peak
                pv_capacity_mw = PV_CAPACITY_FRACTION_OF_MUST_LOAD * p_must
                wind_capacity_mw = WIND_CAPACITY_FRACTION_OF_MUST_LOAD * p_must
                print(
                    "start:",
                    held_out_fold,
                    f"window={validation_scenario.scenario_id}",
                    f"Gamma={gamma:g}",
                    flush=True,
                )
                result = solve_static_gamma_ro_wind_solar_storage(
                    inputs,
                    workload_support,
                    solar_downward_deviation_mwh=_expand_daily(solar_24, hours),
                    wind_downward_deviation_mwh=_expand_daily(wind_24, hours),
                    gamma=gamma,
                    g_max_mw=g_max_mw,
                    r_max_mw=r_max_mw,
                    p_grid_initial_mw=p_must,
                    bess_power_mw=bess_power_mw,
                    bess_energy_mwh=2.0 * bess_power_mw,
                    pv_capacity_mw=pv_capacity_mw,
                    wind_capacity_mw=wind_capacity_mw,
                    energy_quantile=args.energy_quantile,
                    time_limit_seconds=args.time_limit_seconds,
                    day_ahead_solver=args.day_ahead_solver,
                )
                base_row: dict[str, object] = {
                    "method": "static_Gamma_RO",
                    "held_out_fold": held_out_fold,
                    "validation_window_id": validation_scenario.scenario_id,
                    "gamma": gamma,
                    "effective_gamma": result.effective_gamma,
                    "gamma_saturated": result.gamma_saturated,
                    "sample_size": args.sample_size,
                    "energy_quantile": args.energy_quantile,
                    "feasible": result.feasible,
                    "solver_status": result.solver_status,
                    "solver_runtime_seconds": result.runtime_seconds,
                    "mip_gap": result.mip_gap,
                    "max_robust_constraint_violation_mw": result.max_robust_constraint_violation_mw,
                }
                if result.feasible:
                    replay = replay_joint_scenario_with_batch_recourse(
                        inputs,
                        result.plan,
                        validation_scenario,
                        pv_capacity_mw=pv_capacity_mw,
                        wind_capacity_mw=wind_capacity_mw,
                        g_max_mw=g_max_mw,
                        r_max_mw=r_max_mw,
                        p_grid_initial_mw=p_must,
                        recourse_solver=args.recourse_solver,
                    )
                    row = {
                        **base_row,
                        "nominal_operating_cost_usd": result.plan.operating_cost,
                        "actual_operating_cost_usd": replay.operating_cost,
                        "actual_carbon_kg": replay.carbon_kg,
                        "actual_curtailment_mwh": sum(replay.curtailment),
                        "training_mean_batch_adjustment_mwh": result.mean_batch_adjustment_mwh,
                        "validation_batch_adjustment_mwh": replay.batch_adjustment_mwh,
                        "workload_envelope_violation_mwh": replay.workload_envelope_violation_mwh,
                        "grid_limit_violation_mw": replay.grid_limit_violation_mw,
                        "ramp_violation_mw": replay.ramp_violation_mw,
                        "workload_violation": replay.workload_violation,
                        "grid_limit_violation": replay.grid_limit_violation,
                        "ramp_violation": replay.ramp_violation,
                    }
                else:
                    row = {
                        **base_row,
                        "nominal_operating_cost_usd": "",
                        "actual_operating_cost_usd": "",
                        "actual_carbon_kg": "",
                        "actual_curtailment_mwh": "",
                        "training_mean_batch_adjustment_mwh": "",
                        "validation_batch_adjustment_mwh": "",
                        "workload_envelope_violation_mwh": "",
                        "grid_limit_violation_mw": "",
                        "ramp_violation_mw": "",
                        "workload_violation": True,
                        "grid_limit_violation": True,
                        "ramp_violation": True,
                    }
                _append_row(run_path, row)
                completed.add(key)
                print(
                    "done:",
                    held_out_fold,
                    f"window={validation_scenario.scenario_id}",
                    f"status={result.solver_status}",
                    f"runtime={result.runtime_seconds:.3f}s",
                    flush=True,
                )

        summaries = summarize_runs(
            _read_rows(run_path), gammas=(gamma,), expected_windows=expected_windows
        )
        if summaries:
            summary = summaries[0]
            print(
                "candidate_complete:",
                f"Gamma={gamma:g}",
                f"max_wilson_upper_95={summary['max_wilson_upper_95']}",
                flush=True,
            )
            if args.selection_mode == "adaptive" and bool(summary["meets_90pct_target"]):
                print("adaptive_stop:", f"selected_Gamma={gamma:g}", flush=True)
                break

    summaries = summarize_runs(
        _read_rows(run_path), gammas=gammas, expected_windows=expected_windows
    )
    _write_csv(args.output_directory / "gamma_ro_cv_summary.csv", summaries)
    if expected_windows == 36 and summaries:
        passing = [item for item in summaries if bool(item["meets_90pct_target"])]
        selected = passing[0] if passing else min(
            summaries, key=lambda item: float(item["max_wilson_upper_95"])
        )
        selection = {
            "schema_version": 1,
            "method": "static_Gamma_RO",
            "selection_mode": args.selection_mode,
            "candidate_order": list(gammas),
            "selected_gamma": selected["gamma"],
            "target_achieved": bool(passing),
            "candidate_summaries": summaries,
            "run_config_sha256": _sha256(config_path),
            "run_results_sha256": _sha256(run_path),
        }
        (args.output_directory / "gamma_ro_selection.json").write_text(
            json.dumps(selection, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    print("written:", run_path)


if __name__ == "__main__":
    main()
