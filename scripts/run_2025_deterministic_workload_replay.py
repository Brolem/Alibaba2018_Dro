"""运行确定性日前基线的2025四窗口×100条算力轨迹条件回放。"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
from concurrent.futures import ProcessPoolExecutor
from dataclasses import replace
from functools import partial
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from alibaba2018_dro.config import PV_CAPACITY_FRACTION_OF_MUST_LOAD, WIND_CAPACITY_FRACTION_OF_MUST_LOAD
from alibaba2018_dro.inputs import DATA_PROCESSED, DATA_RESULTS, build_hourly_input
from alibaba2018_dro.scenarios import load_workload_replay_scenarios
from alibaba2018_dro.scheduler import _peak_load, replay_joint_scenario_with_batch_recourse, solve_wind_solar_storage

WINDOWS = ("2025-01-01", "2025-04-01", "2025-07-01", "2025-10-01")
RUN_FIELDS = (
    "method", "window", "workload_replay_id", "solver_status",
    "nominal_operating_cost_usd", "actual_operating_cost_usd", "actual_carbon_kg",
    "actual_curtailment_mwh", "batch_adjustment_mwh",
    "workload_envelope_violation_mwh", "grid_limit_violation_mw", "ramp_violation_mw",
    "workload_violation", "grid_limit_violation", "ramp_violation",
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest().upper()


def _read_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8", newline="") as input_file:
        return list(csv.DictReader(input_file))


def _append_rows(path: Path, rows: list[dict[str, object]]) -> None:
    exists = path.exists()
    with path.open("a", encoding="utf-8", newline="") as output_file:
        writer = csv.DictWriter(output_file, fieldnames=RUN_FIELDS, lineterminator="\n")
        if not exists:
            writer.writeheader()
        writer.writerows(rows)


def _window_actual_scenario(template, inputs):
    return replace(
        template,
        residual_solar_mwh=tuple(item.actual_erco_solar_generation_mwh - item.forecast_erco_solar_generation_mwh for item in inputs),
        residual_wind_mwh=tuple(item.actual_erco_wind_generation_mwh - item.forecast_erco_wind_generation_mwh for item in inputs),
        residual_carbon_lbs_per_kwh=tuple(item.actual_consumed_co2_lbs_per_kwh - item.forecast_consumed_co2_lbs_per_kwh for item in inputs),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DATA_PROCESSED / "scenarios" / "saa_scenarios_manifest.json")
    parser.add_argument("--aggregate-workload-csv", type=Path, default=DATA_PROCESSED / "workload" / "aggregate_workload_8d.csv")
    parser.add_argument("--envelope-csv", type=Path, default=DATA_PROCESSED / "workload" / "nominal_workload_30d.csv")
    parser.add_argument("--stats-json", type=Path, default=DATA_PROCESSED / "workload" / "workload_stats.json")
    parser.add_argument("--output-directory", type=Path, default=DATA_RESULTS / "comparison" / "deterministic_2025_workload_replay")
    parser.add_argument("--windows", nargs="+", choices=WINDOWS, default=list(WINDOWS))
    parser.add_argument("--replay-workers", type=int, default=4)
    args = parser.parse_args()
    if args.replay_workers <= 0:
        raise ValueError("replay-workers must be positive")

    args.output_directory.mkdir(parents=True, exist_ok=True)
    config_path = args.output_directory / "run_config.json"
    run_path = args.output_directory / "replay_runs.csv"
    window_files = {window: DATA_PROCESSED / "energy" / "windows" / f"{window}_30d_d168_h3_energy.csv" for window in args.windows}
    config = {
        "schema_version": 1,
        "method": "deterministic_2025_workload_replay",
        "interpretation": "four energy observations; 100 workload trajectories are conditional replays, not 400 independent energy samples",
        "parameters": {"windows": list(args.windows), "workload_replay_count": 100, "replay_workers": args.replay_workers, "day_ahead_solver": "gurobi", "recourse_solver": "gurobi"},
        "source_sha256": {
            "manifest": _sha256(args.manifest), "aggregate_workload": _sha256(args.aggregate_workload_csv),
            "nominal_workload": _sha256(args.envelope_csv), "workload_stats": _sha256(args.stats_json),
            **{f"energy_{window}": _sha256(path) for window, path in window_files.items()},
        },
    }
    if config_path.exists() and json.loads(config_path.read_text(encoding="utf-8")) != config:
        raise ValueError("existing run_config.json differs; use another output directory")
    if not config_path.exists():
        config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    workload_templates = load_workload_replay_scenarios(manifest_path=args.manifest, workload_csv=args.aggregate_workload_csv)
    completed_windows = {row["window"] for row in _read_rows(run_path)}
    for window in args.windows:
        if window in completed_windows:
            print("skip:", window, flush=True)
            continue
        inputs = build_hourly_input(window_files[window], args.envelope_csv, args.stats_json)
        p_must = inputs[0].online_mw + inputs[0].base_mw
        p_peak = _peak_load(inputs)
        common = {
            "g_max_mw": p_peak, "r_max_mw": 0.1 * p_peak, "p_grid_initial_mw": p_must,
            "bess_power_mw": 0.5 * p_peak, "bess_energy_mwh": p_peak,
            "pv_capacity_mw": PV_CAPACITY_FRACTION_OF_MUST_LOAD * p_must,
            "wind_capacity_mw": WIND_CAPACITY_FRACTION_OF_MUST_LOAD * p_must,
        }
        plan = solve_wind_solar_storage(inputs, **common, day_ahead_solver="gurobi")
        if not plan.feasible:
            raise RuntimeError(f"deterministic plan is infeasible for {window}")
        scenarios = [_window_actual_scenario(template, inputs) for template in workload_templates]
        replay_one = partial(
            replay_joint_scenario_with_batch_recourse, inputs, plan,
            pv_capacity_mw=common["pv_capacity_mw"], wind_capacity_mw=common["wind_capacity_mw"],
            g_max_mw=common["g_max_mw"], r_max_mw=common["r_max_mw"],
            p_grid_initial_mw=common["p_grid_initial_mw"], recourse_solver="gurobi",
        )
        print("start:", window, "replays=100", flush=True)
        if args.replay_workers == 1:
            replays = [replay_one(scenario) for scenario in scenarios]
        else:
            with ProcessPoolExecutor(max_workers=args.replay_workers) as executor:
                replays = list(executor.map(replay_one, scenarios))
        rows = []
        for scenario, replay in zip(scenarios, replays, strict=True):
            rows.append({
                "method": "deterministic", "window": window, "workload_replay_id": scenario.scenario_id,
                "solver_status": "optimal", "nominal_operating_cost_usd": plan.operating_cost,
                "actual_operating_cost_usd": replay.operating_cost, "actual_carbon_kg": replay.carbon_kg,
                "actual_curtailment_mwh": sum(replay.curtailment), "batch_adjustment_mwh": replay.batch_adjustment_mwh,
                "workload_envelope_violation_mwh": replay.workload_envelope_violation_mwh,
                "grid_limit_violation_mw": replay.grid_limit_violation_mw, "ramp_violation_mw": replay.ramp_violation_mw,
                "workload_violation": replay.workload_violation, "grid_limit_violation": replay.grid_limit_violation,
                "ramp_violation": replay.ramp_violation,
            })
        _append_rows(run_path, rows)
        print("done:", window, flush=True)

    all_rows = _read_rows(run_path)
    summaries = []
    for window in args.windows:
        selected = [row for row in all_rows if row["window"] == window]
        if len(selected) != 100:
            continue
        summaries.append({
            "method": "deterministic", "window": window, "workload_replay_count": 100,
            "mean_actual_operating_cost_usd": sum(float(row["actual_operating_cost_usd"]) for row in selected) / 100,
            "mean_actual_carbon_kg": sum(float(row["actual_carbon_kg"]) for row in selected) / 100,
            "workload_violation_count": sum(row["workload_violation"].lower() == "true" for row in selected),
            "grid_limit_violation_count": sum(row["grid_limit_violation"].lower() == "true" for row in selected),
            "ramp_violation_count": sum(row["ramp_violation"].lower() == "true" for row in selected),
        })
    if summaries:
        with (args.output_directory / "window_summary.csv").open("w", encoding="utf-8", newline="") as output_file:
            writer = csv.DictWriter(output_file, fieldnames=list(summaries[0]), lineterminator="\n")
            writer.writeheader(); writer.writerows(summaries)
    print("written:", run_path, flush=True)


if __name__ == "__main__":
    main()
