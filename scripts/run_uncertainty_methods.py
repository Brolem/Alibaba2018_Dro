"""运行联合不确定性方法的 2024 标定；当前实现 SAA 样本数选择。"""

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
    BESS_DEGRADATION_COST_USD_PER_MWH_THROUGHPUT,
    DEFAULT_CARBON_BUDGET_REDUCTION,
    EFFECTIVE_REPLAY_CAPACITY_FRACTION,
    PV_CAPACITY_FRACTION_OF_MUST_LOAD,
    WIND_CAPACITY_FRACTION_OF_MUST_LOAD,
)
from alibaba2018_dro.inputs import build_hourly_input_from_rows
from alibaba2018_dro.scenarios import (
    SAA_SAMPLE_SIZES,
    load_calibration_energy_rows,
    load_saa_scenarios,
)
from alibaba2018_dro.scheduler import (
    _peak_load,
    replay_joint_scenario_with_batch_recourse,
    solve_decomposed_saa_wind_solar_storage,
)


PROCESSED = PROJECT_ROOT / "data" / "processed"
RESULTS = PROJECT_ROOT / "data" / "results"
HELD_OUT_FOLDS = ("fold_1", "fold_2", "fold_3")
VALIDATION_WINDOWS_PER_FOLD = 12
VIOLATION_COLUMNS = (
    "workload_violation",
    "grid_limit_violation",
    "ramp_violation",
)
RUN_FIELDS = (
    "method",
    "held_out_fold",
    "validation_window_id",
    "sample_size",
    "feasible",
    "solver_status",
    "solver_runtime_seconds",
    "mip_gap",
    "decomposition_iterations",
    "active_scenario_count",
    "nominal_grid_cost_usd",
    "nominal_bess_degradation_cost_usd",
    "nominal_operating_cost_usd",
    "actual_grid_cost_usd",
    "actual_operating_cost_usd",
    "actual_carbon_kg",
    "actual_curtailment_mwh",
    "training_mean_batch_adjustment_mwh",
    "validation_batch_adjustment_mwh",
    *VIOLATION_COLUMNS,
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as input_file:
        for block in iter(lambda: input_file.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def wilson_upper_bound(
    violations: int,
    total: int,
    *,
    confidence: float = 0.95,
) -> float:
    """二项比例的单侧 Wilson 上界。"""

    if total <= 0:
        raise ValueError("total must be positive")
    if not 0 <= violations <= total:
        raise ValueError("violations must be between zero and total")
    if not 0.5 < confidence < 1.0:
        raise ValueError("confidence must be in (0.5, 1)")
    z = NormalDist().inv_cdf(confidence)
    rate = violations / total
    denominator = 1.0 + z * z / total
    center = (rate + z * z / (2.0 * total)) / denominator
    margin = (
        z
        * math.sqrt(
            rate * (1.0 - rate) / total + z * z / (4.0 * total * total)
        )
        / denominator
    )
    return min(1.0, center + margin)


def _as_bool(value: object) -> bool:
    return str(value).strip().lower() == "true"


def summarize_saa_runs(
    rows: list[dict[str, str]],
    *,
    sample_sizes: tuple[int, ...] = SAA_SAMPLE_SIZES,
) -> list[dict[str, object]]:
    """按 N 汇总 36 个留出伪窗口，并计算三类运行风险的 Wilson 上界。"""

    summaries: list[dict[str, object]] = []
    expected = len(HELD_OUT_FOLDS) * VALIDATION_WINDOWS_PER_FOLD
    for sample_size in sample_sizes:
        selected = [row for row in rows if int(row["sample_size"]) == sample_size]
        if len(selected) != expected:
            continue
        feasible_rows = [row for row in selected if _as_bool(row["feasible"])]
        summary: dict[str, object] = {
            "method": "SAA",
            "sample_size": sample_size,
            "validation_window_count": len(selected),
            "optimal_solve_count": len(feasible_rows),
            "all_solves_optimal": len(feasible_rows) == expected,
            "mean_solver_runtime_seconds": statistics.fmean(
                float(row["solver_runtime_seconds"]) for row in selected
            ),
            "mean_nominal_operating_cost_usd": (
                statistics.fmean(
                    float(row["nominal_operating_cost_usd"])
                    for row in feasible_rows
                )
                if feasible_rows
                else None
            ),
            "mean_actual_operating_cost_usd": (
                statistics.fmean(
                    float(row["actual_operating_cost_usd"])
                    for row in feasible_rows
                )
                if feasible_rows
                else None
            ),
        }
        infeasible_count = expected - len(feasible_rows)
        upper_bounds: list[float] = []
        for column in VIOLATION_COLUMNS:
            violations = infeasible_count + sum(
                _as_bool(row[column]) for row in feasible_rows
            )
            rate = violations / expected
            upper = wilson_upper_bound(violations, expected)
            prefix = column.removesuffix("_violation")
            summary[f"{prefix}_violation_count"] = violations
            summary[f"{prefix}_violation_rate"] = rate
            summary[f"{prefix}_wilson_upper_95"] = upper
            upper_bounds.append(upper)
        summary["max_wilson_upper_95"] = max(upper_bounds)
        summary["meets_90pct_target"] = (
            bool(summary["all_solves_optimal"])
            and max(upper_bounds) <= 0.10 + 1e-12
        )
        summaries.append(summary)
    return summaries


def select_saa_sample_size(
    summaries: list[dict[str, object]],
) -> dict[str, object]:
    """按预注册规则选择 N；若无候选达标，则给出最小最大上界的降级选择。"""

    if not summaries:
        raise ValueError("no complete SAA summaries are available")
    passing = [item for item in summaries if bool(item["meets_90pct_target"])]
    if passing:
        minimum_cost = min(
            float(item["mean_nominal_operating_cost_usd"]) for item in passing
        )
        tolerance = max(abs(minimum_cost) * 0.001, 1e-9)
        cost_tied = [
            item
            for item in passing
            if float(item["mean_nominal_operating_cost_usd"])
            <= minimum_cost + tolerance
        ]
        chosen = min(cost_tied, key=lambda item: int(item["sample_size"]))
        return {
            "selected_sample_size": int(chosen["sample_size"]),
            "target_achieved": True,
            "selection_rule": (
                "all four one-sided 95% Wilson upper bounds <= 0.10; "
                "minimum mean nominal cost; <=0.1% cost tie selects smaller N"
            ),
            "selected_max_wilson_upper_95": chosen["max_wilson_upper_95"],
            "selected_mean_nominal_operating_cost_usd": chosen[
                "mean_nominal_operating_cost_usd"
            ],
        }

    minimum_risk = min(float(item["max_wilson_upper_95"]) for item in summaries)
    risk_tied = [
        item
        for item in summaries
        if math.isclose(
            float(item["max_wilson_upper_95"]),
            minimum_risk,
            rel_tol=0.0,
            abs_tol=1e-12,
        )
    ]
    chosen = min(
        risk_tied,
        key=lambda item: (
            float(item["mean_nominal_operating_cost_usd"])
            if item["mean_nominal_operating_cost_usd"] is not None
            else math.inf,
            int(item["sample_size"]),
        ),
    )
    return {
        "selected_sample_size": int(chosen["sample_size"]),
        "target_achieved": False,
        "selection_rule": (
            "2024 calibration target not achieved; minimize the largest of the "
            "four one-sided 95% Wilson upper bounds"
        ),
        "selected_max_wilson_upper_95": chosen["max_wilson_upper_95"],
        "selected_mean_nominal_operating_cost_usd": chosen[
            "mean_nominal_operating_cost_usd"
        ],
    }


def _write_csv(path: Path, rows: list[dict[str, object]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as output_file:
        writer = csv.DictWriter(output_file, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def _append_run(path: Path, row: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
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


def _run_config(
    *,
    manifest_path: Path,
    calibration_csv: Path,
    workload_csv: Path,
    envelope_csv: Path,
    stats_json: Path,
    time_limit_seconds: float | None,
    decomposition_max_iterations: int,
    replay_workers: int,
) -> dict[str, object]:
    return {
        "schema_version": 2,
        "method": "SAA",
        "protocol": "2024_three_fold_12_pseudo_windows_v1",
        "decomposition": "active_scenarios_with_scipy_highs_carbon_dual_cuts",
        "parameters": {
            "beta": 0.10,
            "wilson_confidence_one_sided": 0.95,
            "carbon_budget_reduction": DEFAULT_CARBON_BUDGET_REDUCTION,
            "effective_capacity_fraction": EFFECTIVE_REPLAY_CAPACITY_FRACTION,
            "bess_degradation_cost_usd_per_mwh_throughput": (
                BESS_DEGRADATION_COST_USD_PER_MWH_THROUGHPUT
            ),
            "time_limit_seconds_per_solve": time_limit_seconds,
            "decomposition_max_iterations": decomposition_max_iterations,
            "replay_workers": replay_workers,
        },
        "source_sha256": {
            "scenario_manifest": sha256_file(manifest_path),
            "calibration_day_blocks": sha256_file(calibration_csv),
            "aggregate_workload": sha256_file(workload_csv),
            "nominal_workload": sha256_file(envelope_csv),
            "workload_stats": sha256_file(stats_json),
        },
    }


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
        default=RESULTS / "calibration" / "saa",
    )
    parser.add_argument(
        "--sample-sizes",
        type=int,
        nargs="+",
        default=list(SAA_SAMPLE_SIZES),
    )
    parser.add_argument(
        "--folds",
        nargs="+",
        choices=HELD_OUT_FOLDS,
        default=list(HELD_OUT_FOLDS),
    )
    parser.add_argument(
        "--max-windows",
        type=int,
        default=VALIDATION_WINDOWS_PER_FOLD,
    )
    parser.add_argument(
        "--time-limit-seconds",
        type=float,
        default=None,
    )
    parser.add_argument(
        "--decomposition-max-iterations",
        type=int,
        default=8,
    )
    parser.add_argument(
        "--replay-workers",
        type=int,
        default=1,
    )
    args = parser.parse_args()

    sample_sizes = tuple(dict.fromkeys(args.sample_sizes))
    if any(size not in SAA_SAMPLE_SIZES for size in sample_sizes):
        raise ValueError(f"sample sizes must be selected from {SAA_SAMPLE_SIZES}")
    if not 1 <= args.max_windows <= VALIDATION_WINDOWS_PER_FOLD:
        raise ValueError(
            f"max-windows must be in [1, {VALIDATION_WINDOWS_PER_FOLD}]"
        )
    if args.decomposition_max_iterations <= 0:
        raise ValueError("decomposition-max-iterations must be positive")
    if args.replay_workers <= 0:
        raise ValueError("replay-workers must be positive")

    args.output_directory.mkdir(parents=True, exist_ok=True)
    config_path = args.output_directory / "run_config.json"
    run_path = args.output_directory / "saa_cv_runs.csv"
    expected_config = _run_config(
        manifest_path=args.manifest,
        calibration_csv=args.calibration_csv,
        workload_csv=args.aggregate_workload_csv,
        envelope_csv=args.envelope_csv,
        stats_json=args.stats_json,
        time_limit_seconds=args.time_limit_seconds,
        decomposition_max_iterations=args.decomposition_max_iterations,
        replay_workers=args.replay_workers,
    )
    if config_path.exists():
        existing_config = json.loads(config_path.read_text(encoding="utf-8"))
        if existing_config != expected_config:
            raise ValueError(
                "existing run_config.json differs from current inputs or parameters; "
                "use a separate output directory"
            )
    else:
        config_path.write_text(
            json.dumps(expected_config, ensure_ascii=False, indent=2, sort_keys=True)
            + "\n",
            encoding="utf-8",
        )

    existing_rows = _read_runs(run_path)
    completed_keys = {
        (
            row["held_out_fold"],
            int(row["validation_window_id"]),
            int(row["sample_size"]),
        )
        for row in existing_rows
    }
    for held_out_fold in args.folds:
        training_scenarios = load_saa_scenarios(
            manifest_path=args.manifest,
            calibration_csv=args.calibration_csv,
            workload_csv=args.aggregate_workload_csv,
            split="training",
            held_out_fold=held_out_fold,
            scenario_count=max(sample_sizes),
        )
        validation_scenarios = load_saa_scenarios(
            manifest_path=args.manifest,
            calibration_csv=args.calibration_csv,
            workload_csv=args.aggregate_workload_csv,
            split="validation",
            held_out_fold=held_out_fold,
        )
        for validation_scenario in validation_scenarios[: args.max_windows]:
            energy_rows = load_calibration_energy_rows(
                args.calibration_csv,
                validation_scenario.energy_delivery_dates,
            )
            inputs = build_hourly_input_from_rows(
                energy_rows,
                args.envelope_csv,
                args.stats_json,
            )
            p_must = inputs[0].online_mw + inputs[0].base_mw
            p_peak = _peak_load(inputs)
            g_max_mw = p_peak
            r_max_mw = 0.1 * p_peak
            bess_power_mw = 0.5 * p_peak
            bess_energy_mwh = 2.0 * bess_power_mw
            pv_capacity_mw = PV_CAPACITY_FRACTION_OF_MUST_LOAD * p_must
            wind_capacity_mw = WIND_CAPACITY_FRACTION_OF_MUST_LOAD * p_must
            for sample_size in sample_sizes:
                key = (held_out_fold, validation_scenario.scenario_id, sample_size)
                if key in completed_keys:
                    print("skip:", held_out_fold, validation_scenario.scenario_id, sample_size)
                    continue
                print(
                    "start:",
                    held_out_fold,
                    f"window={validation_scenario.scenario_id}",
                    f"N={sample_size}",
                    flush=True,
                )
                result = solve_decomposed_saa_wind_solar_storage(
                    inputs,
                    training_scenarios[:sample_size],
                    g_max_mw=g_max_mw,
                    r_max_mw=r_max_mw,
                    p_grid_initial_mw=p_must,
                    bess_power_mw=bess_power_mw,
                    bess_energy_mwh=bess_energy_mwh,
                    pv_capacity_mw=pv_capacity_mw,
                    wind_capacity_mw=wind_capacity_mw,
                    carbon_budget_reduction=DEFAULT_CARBON_BUDGET_REDUCTION,
                    time_limit_seconds=args.time_limit_seconds,
                    max_iterations=args.decomposition_max_iterations,
                    display_progress=True,
                    replay_workers=args.replay_workers,
                )
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
                    )
                    row: dict[str, object] = {
                        "method": "SAA",
                        "held_out_fold": held_out_fold,
                        "validation_window_id": validation_scenario.scenario_id,
                        "sample_size": sample_size,
                        "feasible": True,
                        "solver_status": result.solver_status,
                        "solver_runtime_seconds": result.runtime_seconds,
                        "mip_gap": result.mip_gap,
                        "decomposition_iterations": result.decomposition_iterations,
                        "active_scenario_count": result.active_scenario_count,
                        "nominal_grid_cost_usd": result.plan.grid_cost,
                        "nominal_bess_degradation_cost_usd": result.plan.bess_degradation_cost,
                        "nominal_operating_cost_usd": result.plan.operating_cost,
                        "actual_grid_cost_usd": replay.grid_cost,
                        "actual_operating_cost_usd": replay.operating_cost,
                        "actual_carbon_kg": replay.carbon_kg,
                        "actual_curtailment_mwh": sum(replay.curtailment),
                        "training_mean_batch_adjustment_mwh": result.mean_batch_adjustment_mwh,
                        "validation_batch_adjustment_mwh": replay.batch_adjustment_mwh,
                        "workload_violation": replay.workload_violation,
                        "grid_limit_violation": replay.grid_limit_violation,
                        "ramp_violation": replay.ramp_violation,
                    }
                else:
                    row = {
                        "method": "SAA",
                        "held_out_fold": held_out_fold,
                        "validation_window_id": validation_scenario.scenario_id,
                        "sample_size": sample_size,
                        "feasible": False,
                        "solver_status": result.solver_status,
                        "solver_runtime_seconds": result.runtime_seconds,
                        "mip_gap": result.mip_gap,
                        "decomposition_iterations": result.decomposition_iterations,
                        "active_scenario_count": result.active_scenario_count,
                        "nominal_grid_cost_usd": "",
                        "nominal_bess_degradation_cost_usd": "",
                        "nominal_operating_cost_usd": "",
                        "actual_grid_cost_usd": "",
                        "actual_operating_cost_usd": "",
                        "actual_carbon_kg": "",
                        "actual_curtailment_mwh": "",
                        "training_mean_batch_adjustment_mwh": "",
                        "validation_batch_adjustment_mwh": "",
                        "workload_violation": True,
                        "grid_limit_violation": True,
                        "ramp_violation": True,
                    }
                _append_run(run_path, row)
                completed_keys.add(key)
                print(
                    "done:",
                    held_out_fold,
                    f"window={validation_scenario.scenario_id}",
                    f"N={sample_size}",
                    f"status={result.solver_status}",
                    f"runtime={result.runtime_seconds:.3f}s",
                    flush=True,
                )

    all_rows = _read_runs(run_path)
    summaries = summarize_saa_runs(all_rows)
    if len(summaries) == len(SAA_SAMPLE_SIZES):
        summary_path = args.output_directory / "saa_cv_summary.csv"
        summary_fields = list(summaries[0].keys())
        _write_csv(summary_path, summaries, summary_fields)
        selection = {
            "schema_version": 1,
            "method": "SAA",
            **select_saa_sample_size(summaries),
            "candidate_summaries": summaries,
            "run_config_sha256": sha256_file(config_path),
            "run_results_sha256": sha256_file(run_path),
        }
        selection_path = args.output_directory / "saa_selection.json"
        selection_path.write_text(
            json.dumps(selection, ensure_ascii=False, indent=2, sort_keys=True)
            + "\n",
            encoding="utf-8",
        )
        print("selected_sample_size:", selection["selected_sample_size"])
        print("target_achieved:", selection["target_achieved"])
        print("written:", summary_path)
        print("written:", selection_path)
    else:
        print(
            "partial_grid:",
            f"complete_candidates={len(summaries)}/{len(SAA_SAMPLE_SIZES)}",
        )
    print("written:", run_path)


if __name__ == "__main__":
    main()
