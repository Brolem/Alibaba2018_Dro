"""按预注册顺序校准有限支持 TV-DRO 的总变差半径 rho。"""

from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from alibaba2018_dro.config import (
    EFFECTIVE_REPLAY_CAPACITY_FRACTION,
    PV_CAPACITY_FRACTION_OF_MUST_LOAD,
    WIND_CAPACITY_FRACTION_OF_MUST_LOAD,
)
from alibaba2018_dro.inputs import build_hourly_input_from_rows
from alibaba2018_dro.scenarios import load_calibration_energy_rows, load_saa_scenarios
from alibaba2018_dro.scheduler import (
    _peak_load,
    replay_joint_scenario_with_batch_recourse,
    solve_finite_support_tv_dro_wind_solar_storage,
    tv_dro_allowed_violation_count,
)
from scripts.run_uncertainty_methods import (
    HELD_OUT_FOLDS,
    VALIDATION_WINDOWS_PER_FOLD,
    VIOLATION_COLUMNS,
    VIOLATION_MAGNITUDE_COLUMNS,
    _as_bool,
    sha256_file,
    wilson_upper_bound,
)

PROCESSED = PROJECT_ROOT / "data" / "processed"
RESULTS = PROJECT_ROOT / "data" / "results"
DEFAULT_RHOS = (0.01, 0.025, 0.05)
RUN_FIELDS = (
    "method", "held_out_fold", "validation_window_id", "rho", "beta",
    "support_size", "allowed_training_violation_count", "feasible",
    "solver_status", "solver_runtime_seconds", "mip_gap",
    "decomposition_iterations", "active_scenario_count",
    "nominal_operating_cost_usd", "actual_operating_cost_usd",
    "actual_carbon_kg", "actual_curtailment_mwh",
    "training_mean_batch_adjustment_mwh", "validation_batch_adjustment_mwh",
    "workload_envelope_violation_mwh", "grid_limit_violation_mw",
    "ramp_violation_mw", *VIOLATION_COLUMNS,
)


def _rho_key(value: float) -> str:
    return format(value, ".12g")


def _write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as output_file:
        writer = csv.DictWriter(output_file, fieldnames=list(rows[0]), lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def _append_run(path: Path, row: dict[str, object]) -> None:
    exists = path.exists()
    with path.open("a", encoding="utf-8", newline="") as output_file:
        writer = csv.DictWriter(output_file, fieldnames=RUN_FIELDS, lineterminator="\n")
        if not exists:
            writer.writeheader()
        writer.writerow(row)


def _read_runs(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8", newline="") as input_file:
        return list(csv.DictReader(input_file))


def summarize_tv_dro_runs(
    rows: list[dict[str, str]],
    *,
    rhos: tuple[float, ...] = DEFAULT_RHOS,
) -> list[dict[str, object]]:
    """按 rho 汇总固定的 36 个留出窗口。"""

    expected = len(HELD_OUT_FOLDS) * VALIDATION_WINDOWS_PER_FOLD
    summaries: list[dict[str, object]] = []
    for rho in rhos:
        selected = [row for row in rows if _rho_key(float(row["rho"])) == _rho_key(rho)]
        if len(selected) != expected:
            continue
        feasible = [row for row in selected if _as_bool(row["feasible"])]
        summary: dict[str, object] = {
            "method": "finite_support_TV_DRO",
            "rho": rho,
            "support_size": int(selected[0]["support_size"]),
            "allowed_training_violation_count": int(selected[0]["allowed_training_violation_count"]),
            "validation_window_count": expected,
            "optimal_solve_count": len(feasible),
            "all_solves_optimal": len(feasible) == expected,
            "mean_solver_runtime_seconds": statistics.fmean(float(row["solver_runtime_seconds"]) for row in selected),
            "mean_nominal_operating_cost_usd": statistics.fmean(float(row["nominal_operating_cost_usd"]) for row in feasible) if feasible else None,
            "mean_actual_operating_cost_usd": statistics.fmean(float(row["actual_operating_cost_usd"]) for row in feasible) if feasible else None,
        }
        infeasible_count = expected - len(feasible)
        uppers: list[float] = []
        for column in VIOLATION_COLUMNS:
            count = infeasible_count + sum(_as_bool(row[column]) for row in feasible)
            prefix = column.removesuffix("_violation")
            upper = wilson_upper_bound(count, expected)
            magnitudes = [float(row.get(VIOLATION_MAGNITUDE_COLUMNS[column], 0.0) or 0.0) for row in feasible]
            summary[f"{prefix}_violation_count"] = count
            summary[f"{prefix}_violation_rate"] = count / expected
            summary[f"{prefix}_wilson_upper_95"] = upper
            summary[f"{prefix}_mean_violation_magnitude"] = statistics.fmean(magnitudes) if magnitudes else None
            summary[f"{prefix}_max_violation_magnitude"] = max(magnitudes) if magnitudes else None
            uppers.append(upper)
        summary["max_wilson_upper_95"] = max(uppers)
        summary["meets_90pct_target"] = bool(summary["all_solves_optimal"]) and max(uppers) <= 0.10 + 1e-12
        summaries.append(summary)
    return summaries


def select_tv_radius(summaries: list[dict[str, object]]) -> dict[str, object]:
    """选择首个达到三通道门槛的正半径，否则返回风险最小候选。"""

    if not summaries:
        raise ValueError("no complete TV-DRO summaries are available")
    passing = [item for item in summaries if bool(item["meets_90pct_target"])]
    if passing:
        chosen = min(passing, key=lambda item: float(item["rho"]))
        achieved = True
        rule = "smallest tested positive rho whose three Wilson upper bounds are all <= 0.10"
    else:
        chosen = min(summaries, key=lambda item: (float(item["max_wilson_upper_95"]), float(item["rho"])))
        achieved = False
        rule = "target not achieved; minimize the largest Wilson upper bound, then rho"
    return {
        "selected_rho": float(chosen["rho"]),
        "target_achieved": achieved,
        "selection_rule": rule,
        "selected_max_wilson_upper_95": chosen["max_wilson_upper_95"],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=PROCESSED / "scenarios" / "saa_scenarios_manifest.json")
    parser.add_argument("--calibration-csv", type=Path, default=PROCESSED / "scenarios" / "calibration_day_blocks_2024.csv")
    parser.add_argument("--aggregate-workload-csv", type=Path, default=PROCESSED / "workload" / "aggregate_workload_8d.csv")
    parser.add_argument("--envelope-csv", type=Path, default=PROCESSED / "workload" / "nominal_workload_30d.csv")
    parser.add_argument("--stats-json", type=Path, default=PROCESSED / "workload" / "workload_stats.json")
    parser.add_argument("--output-directory", type=Path, default=RESULTS / "calibration" / "tv_dro_three_fold")
    parser.add_argument("--rhos", type=float, nargs="+", default=list(DEFAULT_RHOS))
    parser.add_argument("--sample-size", type=int, default=20)
    parser.add_argument("--beta", type=float, default=0.10)
    parser.add_argument("--selection-mode", choices=("adaptive", "full_grid"), default="adaptive")
    parser.add_argument("--folds", nargs="+", choices=HELD_OUT_FOLDS, default=list(HELD_OUT_FOLDS))
    parser.add_argument("--max-windows", type=int, default=VALIDATION_WINDOWS_PER_FOLD)
    parser.add_argument("--time-limit-seconds", type=float, default=300.0)
    parser.add_argument("--decomposition-max-iterations", type=int, default=8)
    parser.add_argument("--replay-workers", type=int, default=1)
    args = parser.parse_args()

    rhos = tuple(sorted(set(args.rhos)))
    if any(rho <= 0.0 or rho > args.beta for rho in rhos):
        raise ValueError("every rho must be positive and no greater than beta")
    if not 1 <= args.max_windows <= VALIDATION_WINDOWS_PER_FOLD:
        raise ValueError("max-windows is outside the registered range")
    allowed_counts = {rho: tv_dro_allowed_violation_count(args.sample_size, beta=args.beta, rho=rho) for rho in rhos}
    args.output_directory.mkdir(parents=True, exist_ok=True)
    config_path = args.output_directory / "run_config.json"
    run_path = args.output_directory / "tv_dro_cv_runs.csv"
    config = {
        "schema_version": 1,
        "method": "finite_support_TV_DRO",
        "protocol": "2024_three_fold_adaptive_tv_radius_v1",
        "parameters": {
            "rho_candidates": list(rhos), "beta": args.beta, "sample_size": args.sample_size,
            "allowed_violation_counts": {_rho_key(k): v for k, v in allowed_counts.items()},
            "held_out_folds": list(args.folds), "validation_windows_per_fold": args.max_windows,
            "selection_mode": args.selection_mode, "day_ahead_solver": "gurobi",
            "recourse_solver": "gurobi", "time_limit_seconds_per_solve": args.time_limit_seconds,
            "decomposition_max_iterations": args.decomposition_max_iterations,
            "replay_workers": args.replay_workers,
            "effective_capacity_fraction": EFFECTIVE_REPLAY_CAPACITY_FRACTION,
        },
        "source_sha256": {
            "scenario_manifest": sha256_file(args.manifest), "calibration_day_blocks": sha256_file(args.calibration_csv),
            "aggregate_workload": sha256_file(args.aggregate_workload_csv), "nominal_workload": sha256_file(args.envelope_csv),
            "workload_stats": sha256_file(args.stats_json),
        },
    }
    if config_path.exists() and json.loads(config_path.read_text(encoding="utf-8")) != config:
        raise ValueError("existing run_config.json differs; use a separate output directory")
    if not config_path.exists():
        config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    existing = _read_runs(run_path)
    completed = {(row["held_out_fold"], int(row["validation_window_id"]), _rho_key(float(row["rho"]))) for row in existing}
    for rho in rhos:
        print("candidate_start:", f"rho={rho}", f"allowed={allowed_counts[rho]}/{args.sample_size}", flush=True)
        for fold in args.folds:
            training = load_saa_scenarios(
                manifest_path=args.manifest,
                calibration_csv=args.calibration_csv,
                workload_csv=args.aggregate_workload_csv,
                split="training",
                held_out_fold=fold,
                scenario_count=args.sample_size,
            )
            validation = load_saa_scenarios(
                manifest_path=args.manifest,
                calibration_csv=args.calibration_csv,
                workload_csv=args.aggregate_workload_csv,
                split="validation",
                held_out_fold=fold,
            )
            for scenario in validation[: args.max_windows]:
                key = (fold, scenario.scenario_id, _rho_key(rho))
                if key in completed:
                    continue
                energy_rows = load_calibration_energy_rows(args.calibration_csv, scenario.energy_delivery_dates)
                inputs = build_hourly_input_from_rows(energy_rows, args.envelope_csv, args.stats_json)
                p_must = inputs[0].online_mw + inputs[0].base_mw
                p_peak = _peak_load(inputs)
                common = dict(g_max_mw=p_peak, r_max_mw=0.1 * p_peak, p_grid_initial_mw=p_must,
                              bess_power_mw=0.5 * p_peak, bess_energy_mwh=p_peak,
                              pv_capacity_mw=PV_CAPACITY_FRACTION_OF_MUST_LOAD * p_must,
                              wind_capacity_mw=WIND_CAPACITY_FRACTION_OF_MUST_LOAD * p_must)
                print("start:", fold, f"window={scenario.scenario_id}", f"rho={rho}", flush=True)
                result = solve_finite_support_tv_dro_wind_solar_storage(
                    inputs, training, rho=rho, beta=args.beta, **common,
                    time_limit_seconds=args.time_limit_seconds, max_iterations=args.decomposition_max_iterations,
                    display_progress=True, replay_workers=args.replay_workers,
                )
                base = {"method": "finite_support_TV_DRO", "held_out_fold": fold,
                        "validation_window_id": scenario.scenario_id, "rho": rho, "beta": args.beta,
                        "support_size": args.sample_size, "allowed_training_violation_count": result.allowed_violation_count,
                        "feasible": result.feasible, "solver_status": result.solver_status,
                        "solver_runtime_seconds": result.runtime_seconds, "mip_gap": result.mip_gap,
                        "decomposition_iterations": result.saa_result.decomposition_iterations,
                        "active_scenario_count": result.saa_result.active_scenario_count}
                if result.feasible:
                    replay = replay_joint_scenario_with_batch_recourse(inputs, result.plan, scenario, **{k: common[k] for k in ("pv_capacity_mw", "wind_capacity_mw", "g_max_mw", "r_max_mw", "p_grid_initial_mw")}, recourse_solver="gurobi")
                    row = {**base, "nominal_operating_cost_usd": result.plan.operating_cost,
                           "actual_operating_cost_usd": replay.operating_cost, "actual_carbon_kg": replay.carbon_kg,
                           "actual_curtailment_mwh": sum(replay.curtailment),
                           "training_mean_batch_adjustment_mwh": result.saa_result.mean_batch_adjustment_mwh,
                           "validation_batch_adjustment_mwh": replay.batch_adjustment_mwh,
                           "workload_envelope_violation_mwh": replay.workload_envelope_violation_mwh,
                           "grid_limit_violation_mw": replay.grid_limit_violation_mw, "ramp_violation_mw": replay.ramp_violation_mw,
                           "workload_violation": replay.workload_violation, "grid_limit_violation": replay.grid_limit_violation,
                           "ramp_violation": replay.ramp_violation}
                else:
                    row = {**base, **{field: "" for field in RUN_FIELDS if field not in base},
                           "workload_violation": True, "grid_limit_violation": True, "ramp_violation": True}
                _append_run(run_path, row)
                completed.add(key)
                print("done:", fold, f"window={scenario.scenario_id}", f"rho={rho}", f"status={result.solver_status}", f"runtime={result.runtime_seconds:.3f}s", flush=True)

        summary = summarize_tv_dro_runs(_read_runs(run_path), rhos=(rho,))
        if summary:
            print("candidate_complete:", f"rho={rho}", f"max_wilson={summary[0]['max_wilson_upper_95']}", f"pass={summary[0]['meets_90pct_target']}", flush=True)
            if args.selection_mode == "adaptive" and bool(summary[0]["meets_90pct_target"]):
                break

    summaries = summarize_tv_dro_runs(_read_runs(run_path), rhos=rhos)
    if summaries:
        _write_csv(args.output_directory / "tv_dro_cv_summary.csv", summaries)
    if summaries and (args.selection_mode == "adaptive" and any(bool(item["meets_90pct_target"]) for item in summaries) or len(summaries) == len(rhos)):
        selection = {"schema_version": 1, "method": "finite_support_TV_DRO", "selection_mode": args.selection_mode,
                     "candidate_order": list(rhos), **select_tv_radius(summaries), "candidate_summaries": summaries,
                     "run_config_sha256": sha256_file(config_path), "run_results_sha256": sha256_file(run_path)}
        (args.output_directory / "tv_dro_selection.json").write_text(json.dumps(selection, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print("selected_rho:", selection["selected_rho"], flush=True)
    print("written:", run_path, flush=True)


if __name__ == "__main__":
    main()
