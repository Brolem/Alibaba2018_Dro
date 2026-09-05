"""统一运行 2025 主比较、2x2 机制消融和必要敏感性分析。"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import statistics
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict, dataclass, replace
from functools import partial
from pathlib import Path
from typing import Iterable, Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from alibaba2018_dro.config import (
    BESS_DEGRADATION_COST_USD_PER_MWH_THROUGHPUT,
    EFFECTIVE_REPLAY_CAPACITY_FRACTION,
    PV_CAPACITY_FRACTION_OF_MUST_LOAD,
    WIND_CAPACITY_FRACTION_OF_MUST_LOAD,
)
from alibaba2018_dro.inputs import DATA_PROCESSED, DATA_RESULTS, HourlyInput, build_hourly_input
from alibaba2018_dro.scenarios import (
    ENERGY_REPLAY_SEED,
    ScenarioRealization,
    attach_bootstrap_energy_replay,
    load_hourly_downward_residual_quantiles,
    load_saa_scenarios,
    load_workload_replay_scenarios,
)
from alibaba2018_dro.scheduler import (
    DayAheadResult,
    _peak_load,
    replay_joint_scenario_with_batch_recourse,
    solve_decomposed_saa_wind_solar_storage,
    solve_finite_support_tv_dro_wind_solar_storage,
    solve_static_gamma_ro_wind_solar_storage,
    solve_wind_solar_storage,
)


WINDOWS = ("2025-01-01", "2025-04-01", "2025-07-01", "2025-10-01")
EXPERIMENTS = ("main", "ablation", "sensitivity")
METHODS = ("deterministic", "saa", "gamma_ro", "tv_dro")
SAMPLE_SIZE = 20
BETA = 0.10
GAMMA = 0.5
ENERGY_QUANTILE = 0.90
RHO = 0.01

RUN_FIELDS = (
    "suite", "configuration", "method", "window", "workload_replay_id",
    "energy_uncertainty", "workload_uncertainty", "sample_size", "gamma", "rho",
    "effective_capacity_fraction", "bess_degradation_cost_usd_per_mwh_throughput",
    "solver_status", "solve_wall_time_seconds", "solver_runtime_seconds", "mip_gap",
    "decomposition_iterations", "active_scenario_count",
    "nominal_grid_cost_usd", "nominal_bess_degradation_cost_usd",
    "nominal_operating_cost_usd", "actual_grid_cost_usd", "actual_operating_cost_usd",
    "actual_carbon_kg", "actual_curtailment_mwh", "batch_adjustment_mwh",
    "workload_envelope_violation_mwh", "grid_limit_violation_mw", "ramp_violation_mw",
    "workload_violation", "grid_limit_violation", "ramp_violation",
)
FAILURE_FIELDS = (
    "suite", "configuration", "method", "window",
    "grid_limit_fraction_of_peak", "ramp_limit_fraction_of_peak",
    "deterministic_constraint_headroom_fraction_of_peak", "phase", "error",
)


@dataclass(frozen=True)
class Configuration:
    suite: str
    name: str
    method: str
    energy_uncertainty: bool = True
    workload_uncertainty: bool = True
    effective_capacity_fraction: float = EFFECTIVE_REPLAY_CAPACITY_FRACTION
    bess_degradation_cost: float = BESS_DEGRADATION_COST_USD_PER_MWH_THROUGHPUT

    @property
    def solve_signature(self) -> tuple[object, ...]:
        return (
            self.method,
            self.energy_uncertainty,
            self.workload_uncertainty,
            self.effective_capacity_fraction,
            self.bess_degradation_cost,
        )


@dataclass(frozen=True)
class SolvedPlan:
    plan: DayAheadResult
    solver_status: str
    wall_time_seconds: float
    solver_runtime_seconds: float
    mip_gap: float
    decomposition_iterations: int
    active_scenario_count: int


def configurations(
    experiments: Sequence[str],
    methods: Sequence[str] | None = None,
    names: Sequence[str] | None = None,
) -> list[Configuration]:
    selected: list[Configuration] = []
    if "main" in experiments:
        selected.extend(
            (
                Configuration("main", "deterministic", "deterministic"),
                Configuration("main", "saa_n20", "saa"),
                Configuration("main", "gamma_ro_gamma_0p5", "gamma_ro"),
                Configuration("main", "tv_dro_rho_0p01", "tv_dro"),
            )
        )
    if "ablation" in experiments:
        for energy, workload in ((False, False), (True, False), (False, True), (True, True)):
            selected.append(
                Configuration(
                    "ablation",
                    f"tv_dro_energy_{int(energy)}_workload_{int(workload)}",
                    "tv_dro",
                    energy_uncertainty=energy,
                    workload_uncertainty=workload,
                )
            )
    if "sensitivity" in experiments:
        for cost in (10.0, 20.0, 40.0):
            selected.append(
                Configuration(
                    "sensitivity", f"tv_dro_bess_cost_{int(cost)}", "tv_dro",
                    bess_degradation_cost=cost,
                )
            )
        for fraction in (0.60, 0.70, 0.80):
            selected.append(
                Configuration(
                    "sensitivity", f"tv_dro_capacity_fraction_{fraction:.1f}".replace(".", "p"),
                    "tv_dro", effective_capacity_fraction=fraction,
                )
            )
    if methods is None:
        filtered = selected
    else:
        selected_methods = set(methods)
        filtered = [config for config in selected if config.method in selected_methods]
    if names is None:
        return filtered
    selected_names = set(names)
    unknown = selected_names - {config.name for config in selected}
    if unknown:
        raise ValueError(
            "unknown configuration(s) for selected experiments: "
            + ", ".join(sorted(unknown))
        )
    return [config for config in filtered if config.name in selected_names]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as input_file:
        for block in iter(lambda: input_file.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def _read_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8", newline="") as input_file:
        return list(csv.DictReader(input_file))


def _append_rows(path: Path, rows: Sequence[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    exists = path.exists()
    with path.open("a", encoding="utf-8", newline="") as output_file:
        writer = csv.DictWriter(output_file, fieldnames=RUN_FIELDS, lineterminator="\n")
        if not exists:
            writer.writeheader()
        writer.writerows(rows)


def _record_failure(path: Path, row: dict[str, object]) -> None:
    existing = _read_rows(path)
    key = (row["suite"], row["configuration"], row["window"])
    if any(
        (item["suite"], item["configuration"], item["window"]) == key
        for item in existing
    ):
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="") as output_file:
        writer = csv.DictWriter(
            output_file, fieldnames=FAILURE_FIELDS, lineterminator="\n"
        )
        if not existing:
            writer.writeheader()
        writer.writerow(row)


def _write_csv(path: Path, rows: Sequence[dict[str, object]]) -> None:
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as output_file:
        writer = csv.DictWriter(output_file, fieldnames=list(rows[0]), lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def _write_plan(
    output_directory: Path,
    config: Configuration,
    window: str,
    solved: SolvedPlan,
    common: dict[str, float],
) -> None:
    """Persist the actual day-ahead vectors used by every replay group."""

    path = output_directory / "day_ahead_plans" / config.name / f"{window}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "suite": config.suite,
        "configuration": config.name,
        "method": config.method,
        "window": window,
        "energy_uncertainty": config.energy_uncertainty,
        "workload_uncertainty": config.workload_uncertainty,
        "physical_parameters": common,
        "solver": {
            "status": solved.solver_status,
            "wall_time_seconds": solved.wall_time_seconds,
            "runtime_seconds": solved.solver_runtime_seconds,
            "mip_gap": solved.mip_gap,
            "decomposition_iterations": solved.decomposition_iterations,
            "active_scenario_count": solved.active_scenario_count,
        },
        "plan": asdict(solved.plan),
    }
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _write_plan_equivalence_summary(
    output_directory: Path,
    configs: Sequence[Configuration],
    windows: Sequence[str],
) -> None:
    """Compare persisted plans without treating equal objective values as equality."""

    if len(configs) < 2:
        return
    reference = next(
        (config for config in configs if config.method == "deterministic"),
        configs[0],
    )
    vector_fields = (
        "batch", "grid", "bess_charge", "bess_discharge", "curtailment"
    )
    rows: list[dict[str, object]] = []
    for window in windows:
        reference_path = (
            output_directory / "day_ahead_plans" / reference.name / f"{window}.json"
        )
        if not reference_path.exists():
            continue
        reference_payload = json.loads(reference_path.read_text(encoding="utf-8"))
        reference_plan = reference_payload["plan"]
        for config in configs:
            if config.name == reference.name:
                continue
            candidate_path = (
                output_directory / "day_ahead_plans" / config.name / f"{window}.json"
            )
            if not candidate_path.exists():
                continue
            candidate_payload = json.loads(candidate_path.read_text(encoding="utf-8"))
            candidate_plan = candidate_payload["plan"]
            row: dict[str, object] = {
                "window": window,
                "reference_configuration": reference.name,
                "candidate_configuration": config.name,
                "operating_cost_difference_usd": (
                    candidate_plan["operating_cost"]
                    - reference_plan["operating_cost"]
                ),
            }
            for field in vector_fields:
                reference_values = reference_plan[field]
                candidate_values = candidate_plan[field]
                if len(reference_values) != len(candidate_values):
                    raise ValueError(f"plan vector length mismatch for {field}")
                row[f"max_abs_{field}_difference"] = max(
                    (
                        abs(float(candidate) - float(baseline))
                        for baseline, candidate in zip(
                            reference_values, candidate_values, strict=True
                        )
                    ),
                    default=0.0,
                )
            row["candidate_max_simultaneous_charge_discharge_mw"] = max(
                (
                    min(float(charge), float(discharge))
                    for charge, discharge in zip(
                        candidate_plan["bess_charge"],
                        candidate_plan["bess_discharge"],
                        strict=True,
                    )
                ),
                default=0.0,
            )
            rows.append(row)
    _write_csv(output_directory / "plan_equivalence_summary.csv", rows)


def _as_bool(value: object) -> bool:
    return str(value).strip().lower() == "true"


def _expand_daily(values: Sequence[float], hours: int) -> tuple[float, ...]:
    if not values or hours % len(values) != 0:
        raise ValueError("daily profile does not tile the study horizon")
    return tuple(values) * (hours // len(values))


def _actual_scenario(template: ScenarioRealization, inputs: Sequence[HourlyInput]) -> ScenarioRealization:
    return replace(
        template,
        residual_solar_mwh=tuple(
            item.actual_erco_solar_generation_mwh - item.forecast_erco_solar_generation_mwh
            for item in inputs
        ),
        residual_wind_mwh=tuple(
            item.actual_erco_wind_generation_mwh - item.forecast_erco_wind_generation_mwh
            for item in inputs
        ),
        residual_carbon_lbs_per_kwh=tuple(
            item.actual_consumed_co2_lbs_per_kwh - item.forecast_consumed_co2_lbs_per_kwh
            for item in inputs
        ),
    )


def _training_support(
    base: Sequence[ScenarioRealization],
    inputs: Sequence[HourlyInput],
    *,
    energy_uncertainty: bool,
    workload_uncertainty: bool,
) -> list[ScenarioRealization]:
    """按机制开关构造支持；评价场景不调用本函数，始终保持完整联合不确定性。"""

    conversion_values = {item.workload_mwh_per_core_hour for item in inputs}
    if len(conversion_values) != 1:
        raise ValueError("inputs must have one workload conversion")
    conversion = next(iter(conversion_values))
    if conversion <= 0.0:
        raise ValueError("workload conversion must be positive")
    nominal_arrived = tuple(
        float(item.batch_cumulative_arrived_mwh or 0.0) / conversion for item in inputs
    )
    nominal_due = tuple(
        float(item.batch_cumulative_due_mwh or 0.0) / conversion for item in inputs
    )
    zeros = (0.0,) * len(inputs)
    return [
        replace(
            scenario,
            cumulative_arrived_core_hours=(
                scenario.cumulative_arrived_core_hours if workload_uncertainty else nominal_arrived
            ),
            cumulative_due_core_hours=(
                scenario.cumulative_due_core_hours if workload_uncertainty else nominal_due
            ),
            residual_solar_mwh=scenario.residual_solar_mwh if energy_uncertainty else zeros,
            residual_wind_mwh=scenario.residual_wind_mwh if energy_uncertainty else zeros,
            residual_carbon_lbs_per_kwh=(
                scenario.residual_carbon_lbs_per_kwh if energy_uncertainty else zeros
            ),
        )
        for scenario in base
    ]


def _deduplicate_support(
    scenarios: Sequence[ScenarioRealization],
) -> list[ScenarioRealization]:
    """按进入模型的五组随机数组折叠完全相同的经验原子。

    该辅助函数只用于名义能源+名义算力消融，此时全部登记抽样必然退化为
    同一个概率为 1 的原子；不对部分重复的经验支持调用，以免改变经验权重。
    """

    unique: list[ScenarioRealization] = []
    seen: set[tuple[tuple[float, ...], ...]] = set()
    for scenario in scenarios:
        key = (
            scenario.cumulative_arrived_core_hours,
            scenario.cumulative_due_core_hours,
            scenario.residual_solar_mwh,
            scenario.residual_wind_mwh,
            scenario.residual_carbon_lbs_per_kwh,
        )
        if key not in seen:
            seen.add(key)
            unique.append(scenario)
    return unique


def _common_parameters(
    inputs: Sequence[HourlyInput],
    *,
    grid_limit_fraction_of_peak: float = 1.0,
    ramp_limit_fraction_of_peak: float = 0.10,
) -> dict[str, float]:
    if not 0.0 < grid_limit_fraction_of_peak <= 1.0:
        raise ValueError("grid_limit_fraction_of_peak must be in (0, 1]")
    if not 0.0 < ramp_limit_fraction_of_peak <= 1.0:
        raise ValueError("ramp_limit_fraction_of_peak must be in (0, 1]")
    p_must = inputs[0].online_mw + inputs[0].base_mw
    p_peak = _peak_load(list(inputs))
    return {
        "g_max_mw": grid_limit_fraction_of_peak * p_peak,
        "r_max_mw": ramp_limit_fraction_of_peak * p_peak,
        "p_grid_initial_mw": p_must,
        "bess_power_mw": 0.5 * p_peak,
        "bess_energy_mwh": p_peak,
        "pv_capacity_mw": PV_CAPACITY_FRACTION_OF_MUST_LOAD * p_must,
        "wind_capacity_mw": WIND_CAPACITY_FRACTION_OF_MUST_LOAD * p_must,
    }


def _deterministic_planning_parameters(
    inputs: Sequence[HourlyInput],
    physical_common: dict[str, float],
    *,
    headroom_fraction_of_peak: float = 0.0,
) -> dict[str, float]:
    """Reserve equal grid and ramp headroom in planning, not evaluation."""

    if headroom_fraction_of_peak < 0.0:
        raise ValueError("headroom_fraction_of_peak must be nonnegative")
    planning_common = dict(physical_common)
    headroom_mw = headroom_fraction_of_peak * _peak_load(list(inputs))
    planning_common["g_max_mw"] -= headroom_mw
    planning_common["r_max_mw"] -= headroom_mw
    if planning_common["g_max_mw"] <= 0.0 or planning_common["r_max_mw"] <= 0.0:
        raise ValueError("deterministic planning headroom leaves a nonpositive boundary")
    return planning_common


def _solve(
    config: Configuration,
    inputs: list[HourlyInput],
    base_training: Sequence[ScenarioRealization],
    solar_downward_24: Sequence[float],
    wind_downward_24: Sequence[float],
    *,
    time_limit_seconds: float | None,
    max_iterations: int,
    replay_workers: int,
    grid_limit_fraction_of_peak: float = 1.0,
    ramp_limit_fraction_of_peak: float = 0.10,
    deterministic_constraint_headroom_fraction_of_peak: float = 0.0,
) -> tuple[SolvedPlan, dict[str, float]]:
    common = _common_parameters(
        inputs,
        grid_limit_fraction_of_peak=grid_limit_fraction_of_peak,
        ramp_limit_fraction_of_peak=ramp_limit_fraction_of_peak,
    )
    training = _training_support(
        base_training,
        inputs,
        energy_uncertainty=config.energy_uncertainty,
        workload_uncertainty=config.workload_uncertainty,
    )
    if not config.energy_uncertainty and not config.workload_uncertainty:
        training = _deduplicate_support(training)
        if len(training) != 1:
            raise ValueError(
                "nominal-energy + nominal-workload support must collapse to one atom"
            )
    started = time.perf_counter()
    if config.method == "deterministic":
        planning_common = _deterministic_planning_parameters(
            inputs,
            common,
            headroom_fraction_of_peak=(
                deterministic_constraint_headroom_fraction_of_peak
            ),
        )
        plan = solve_wind_solar_storage(
            inputs,
            **planning_common,
            bess_degradation_cost_usd_per_mwh_throughput=config.bess_degradation_cost,
            day_ahead_solver="gurobi",
        )
        result = SolvedPlan(
            plan=plan, solver_status="optimal" if plan.feasible else "infeasible",
            wall_time_seconds=time.perf_counter() - started,
            solver_runtime_seconds=time.perf_counter() - started,
            mip_gap=0.0, decomposition_iterations=0, active_scenario_count=0,
        )
    elif config.method == "saa":
        solved = solve_decomposed_saa_wind_solar_storage(
            inputs, training, **common,
            bess_degradation_cost_usd_per_mwh_throughput=config.bess_degradation_cost,
            time_limit_seconds=time_limit_seconds, max_iterations=max_iterations,
            display_progress=True, replay_workers=replay_workers,
        )
        result = SolvedPlan(
            solved.plan, solved.solver_status, time.perf_counter() - started,
            solved.runtime_seconds, solved.mip_gap, solved.decomposition_iterations,
            solved.active_scenario_count,
        )
    elif config.method == "gamma_ro":
        solved = solve_static_gamma_ro_wind_solar_storage(
            inputs, training,
            solar_downward_deviation_mwh=_expand_daily(solar_downward_24, len(inputs)),
            wind_downward_deviation_mwh=_expand_daily(wind_downward_24, len(inputs)),
            gamma=GAMMA, energy_quantile=ENERGY_QUANTILE, **common,
            bess_degradation_cost_usd_per_mwh_throughput=config.bess_degradation_cost,
            time_limit_seconds=time_limit_seconds, day_ahead_solver="gurobi",
        )
        result = SolvedPlan(
            solved.plan, solved.solver_status, time.perf_counter() - started,
            solved.runtime_seconds, solved.mip_gap, 0, len(training),
        )
    elif config.method == "tv_dro":
        solved = solve_finite_support_tv_dro_wind_solar_storage(
            inputs, training, rho=RHO, beta=BETA, **common,
            bess_degradation_cost_usd_per_mwh_throughput=config.bess_degradation_cost,
            time_limit_seconds=time_limit_seconds, max_iterations=max_iterations,
            display_progress=True, replay_workers=replay_workers,
        )
        result = SolvedPlan(
            solved.plan, solved.solver_status, time.perf_counter() - started,
            solved.runtime_seconds, solved.mip_gap,
            solved.saa_result.decomposition_iterations,
            solved.saa_result.active_scenario_count,
        )
    else:
        raise ValueError(f"unknown method: {config.method}")
    if not result.plan.feasible:
        raise RuntimeError(f"{config.name} is infeasible")
    return result, common


def _replay_rows(
    config: Configuration,
    window: str,
    inputs: list[HourlyInput],
    solved: SolvedPlan,
    common: dict[str, float],
    scenarios: Sequence[ScenarioRealization],
    *,
    replay_workers: int,
) -> list[dict[str, object]]:
    replay_one = partial(
        replay_joint_scenario_with_batch_recourse,
        inputs,
        solved.plan,
        pv_capacity_mw=common["pv_capacity_mw"],
        wind_capacity_mw=common["wind_capacity_mw"],
        g_max_mw=common["g_max_mw"],
        r_max_mw=common["r_max_mw"],
        p_grid_initial_mw=common["p_grid_initial_mw"],
        recourse_solver="gurobi",
    )
    if replay_workers == 1:
        replay_results = [replay_one(scenario) for scenario in scenarios]
    else:
        with ProcessPoolExecutor(max_workers=replay_workers) as executor:
            replay_results = list(executor.map(replay_one, scenarios))
    rows: list[dict[str, object]] = []
    for scenario, replay in zip(scenarios, replay_results, strict=True):
        rows.append(
            {
                "suite": config.suite,
                "configuration": config.name,
                "method": config.method,
                "window": window,
                "workload_replay_id": scenario.scenario_id,
                "energy_uncertainty": config.energy_uncertainty,
                "workload_uncertainty": config.workload_uncertainty,
                "sample_size": SAMPLE_SIZE if config.method != "deterministic" else 0,
                "gamma": GAMMA if config.method == "gamma_ro" else "",
                "rho": RHO if config.method == "tv_dro" else "",
                "effective_capacity_fraction": config.effective_capacity_fraction,
                "bess_degradation_cost_usd_per_mwh_throughput": config.bess_degradation_cost,
                "solver_status": solved.solver_status,
                "solve_wall_time_seconds": solved.wall_time_seconds,
                "solver_runtime_seconds": solved.solver_runtime_seconds,
                "mip_gap": solved.mip_gap,
                "decomposition_iterations": solved.decomposition_iterations,
                "active_scenario_count": solved.active_scenario_count,
                "nominal_grid_cost_usd": solved.plan.grid_cost,
                "nominal_bess_degradation_cost_usd": solved.plan.bess_degradation_cost,
                "nominal_operating_cost_usd": solved.plan.operating_cost,
                "actual_grid_cost_usd": replay.grid_cost,
                "actual_operating_cost_usd": replay.operating_cost,
                "actual_carbon_kg": replay.carbon_kg,
                "actual_curtailment_mwh": sum(replay.curtailment),
                "batch_adjustment_mwh": replay.batch_adjustment_mwh,
                "workload_envelope_violation_mwh": replay.workload_envelope_violation_mwh,
                "grid_limit_violation_mw": replay.grid_limit_violation_mw,
                "ramp_violation_mw": replay.ramp_violation_mw,
                "workload_violation": replay.workload_violation,
                "grid_limit_violation": replay.grid_limit_violation,
                "ramp_violation": replay.ramp_violation,
            }
        )
    return rows


def _mean(rows: Sequence[dict[str, str]], field: str) -> float:
    return statistics.fmean(float(row[field]) for row in rows)


def _summarize_group(rows: Sequence[dict[str, str]]) -> dict[str, object]:
    first = rows[0]
    summary: dict[str, object] = {
        "suite": first["suite"], "configuration": first["configuration"],
        "method": first["method"], "window": first["window"],
        "workload_replay_count": len(rows),
        "energy_uncertainty": _as_bool(first["energy_uncertainty"]),
        "workload_uncertainty": _as_bool(first["workload_uncertainty"]),
        "sample_size": int(first["sample_size"]), "gamma": first["gamma"], "rho": first["rho"],
        "effective_capacity_fraction": float(first["effective_capacity_fraction"]),
        "bess_degradation_cost_usd_per_mwh_throughput": float(first["bess_degradation_cost_usd_per_mwh_throughput"]),
        "solver_status": first["solver_status"],
        "solve_wall_time_seconds": float(first["solve_wall_time_seconds"]),
        "solver_runtime_seconds": float(first["solver_runtime_seconds"]),
        "nominal_grid_cost_usd": float(first["nominal_grid_cost_usd"]),
        "nominal_bess_degradation_cost_usd": float(first["nominal_bess_degradation_cost_usd"]),
        "nominal_operating_cost_usd": float(first["nominal_operating_cost_usd"]),
        "mean_actual_grid_cost_usd": _mean(rows, "actual_grid_cost_usd"),
        "mean_actual_operating_cost_usd": _mean(rows, "actual_operating_cost_usd"),
        "mean_actual_carbon_kg": _mean(rows, "actual_carbon_kg"),
        "mean_actual_curtailment_mwh": _mean(rows, "actual_curtailment_mwh"),
        "mean_batch_adjustment_mwh": _mean(rows, "batch_adjustment_mwh"),
        "max_batch_adjustment_mwh": max(float(row["batch_adjustment_mwh"]) for row in rows),
    }
    risk_fields = (
        ("workload", "workload_violation", "workload_envelope_violation_mwh"),
        ("grid_limit", "grid_limit_violation", "grid_limit_violation_mw"),
        ("ramp", "ramp_violation", "ramp_violation_mw"),
    )
    for prefix, flag, magnitude in risk_fields:
        count = sum(_as_bool(row[flag]) for row in rows)
        # The first recourse lexicographic stage fixes the binary risk choice.
        # Later continuous stages can leave sub-tolerance primal residuals, so a
        # false risk flag must report zero magnitude rather than solver noise.
        magnitudes = [
            float(row[magnitude]) if _as_bool(row[flag]) else 0.0
            for row in rows
        ]
        positive = [
            float(row[magnitude])
            for row in rows
            if _as_bool(row[flag])
        ]
        summary[f"{prefix}_violation_count"] = count
        summary[f"{prefix}_violation_rate"] = count / len(rows)
        summary[f"{prefix}_mean_violation_magnitude_all"] = statistics.fmean(magnitudes)
        summary[f"{prefix}_mean_violation_magnitude_conditional"] = (
            statistics.fmean(positive) if positive else 0.0
        )
        summary[f"{prefix}_max_violation_magnitude"] = max(magnitudes)
    return summary


def summarize(run_path: Path, selected_configs: Sequence[Configuration], windows: Sequence[str]) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    rows = _read_rows(run_path)
    window_summaries: list[dict[str, object]] = []
    for config in selected_configs:
        for window in windows:
            group = [
                row for row in rows
                if row["suite"] == config.suite and row["configuration"] == config.name and row["window"] == window
            ]
            if len(group) == 100:
                window_summaries.append(_summarize_group(group))
    overall: list[dict[str, object]] = []
    for config in selected_configs:
        selected = [
            item for item in window_summaries
            if item["suite"] == config.suite and item["configuration"] == config.name
        ]
        if len(selected) != len(windows):
            continue
        row: dict[str, object] = {
            "suite": config.suite, "configuration": config.name, "method": config.method,
            "energy_window_count": len(selected), "conditional_workload_replay_count": 100,
            "mean_nominal_operating_cost_usd_across_windows": statistics.fmean(float(item["nominal_operating_cost_usd"]) for item in selected),
            "mean_actual_operating_cost_usd_across_windows": statistics.fmean(float(item["mean_actual_operating_cost_usd"]) for item in selected),
            "mean_actual_curtailment_mwh_across_windows": statistics.fmean(float(item["mean_actual_curtailment_mwh"]) for item in selected),
            "mean_batch_adjustment_mwh_across_windows": statistics.fmean(float(item["mean_batch_adjustment_mwh"]) for item in selected),
            "total_solve_wall_time_seconds": sum(float(item["solve_wall_time_seconds"]) for item in selected),
        }
        for prefix in ("workload", "grid_limit", "ramp"):
            counts = [int(item[f"{prefix}_violation_count"]) for item in selected]
            row[f"{prefix}_mean_conditional_violation_rate_across_windows"] = statistics.fmean(count / 100 for count in counts)
            row[f"{prefix}_windows_with_any_violation"] = sum(count > 0 for count in counts)
            row[f"{prefix}_mean_violation_magnitude_all_across_windows"] = statistics.fmean(
                float(item[f"{prefix}_mean_violation_magnitude_all"])
                for item in selected
            )
            conditional = [
                float(item[f"{prefix}_mean_violation_magnitude_conditional"])
                for item in selected
                if int(item[f"{prefix}_violation_count"]) > 0
            ]
            row[f"{prefix}_mean_violation_magnitude_conditional_across_violating_windows"] = (
                statistics.fmean(conditional) if conditional else 0.0
            )
            row[f"{prefix}_max_violation_magnitude_across_windows"] = max(float(item[f"{prefix}_max_violation_magnitude"]) for item in selected)
        overall.append(row)
    return window_summaries, overall


def _ablation_effects(
    rows: Sequence[dict[str, str]], windows: Sequence[str]
) -> list[dict[str, object]]:
    """Return paired 2x2 mean contrasts; no pseudo-replicated inference."""

    names = {
        (False, False): "tv_dro_energy_0_workload_0",
        (True, False): "tv_dro_energy_1_workload_0",
        (False, True): "tv_dro_energy_0_workload_1",
        (True, True): "tv_dro_energy_1_workload_1",
    }
    metrics = (
        "actual_operating_cost_usd",
        "actual_curtailment_mwh",
        "batch_adjustment_mwh",
    )
    effects = {
        "energy_effect_at_nominal_workload": ((True, False), (False, False)),
        "energy_effect_at_uncertain_workload": ((True, True), (False, True)),
        "workload_effect_at_nominal_energy": ((False, True), (False, False)),
        "workload_effect_at_uncertain_energy": ((True, True), (True, False)),
    }
    output: list[dict[str, object]] = []
    for window_label, included_windows in (
        *((window, (window,)) for window in windows),
        ("all_four_windows", tuple(windows)),
    ):
        keyed = {
            (row["configuration"], row["window"], int(row["workload_replay_id"])): row
            for row in rows
            if row["suite"] == "ablation" and row["window"] in included_windows
        }
        for metric in metrics:
            for effect_name, (high, low) in effects.items():
                differences = []
                for window in included_windows:
                    for replay_id in range(100):
                        high_row = keyed[(names[high], window, replay_id)]
                        low_row = keyed[(names[low], window, replay_id)]
                        differences.append(float(high_row[metric]) - float(low_row[metric]))
                output.append({
                    "window_scope": window_label,
                    "paired_observation_count": len(differences),
                    "effect": effect_name,
                    "metric": metric,
                    "mean_paired_difference": statistics.fmean(differences),
                })
            interaction = []
            for window in included_windows:
                for replay_id in range(100):
                    values = {
                        key: float(keyed[(name, window, replay_id)][metric])
                        for key, name in names.items()
                    }
                    interaction.append(
                        values[(True, True)] - values[(True, False)]
                        - values[(False, True)] + values[(False, False)]
                    )
            output.append({
                "window_scope": window_label,
                "paired_observation_count": len(interaction),
                "effect": "energy_by_workload_interaction",
                "metric": metric,
                "mean_paired_difference": statistics.fmean(interaction),
            })
    return output


def _sensitivity_effects(
    overall: Sequence[dict[str, object]],
) -> list[dict[str, object]]:
    by_name = {
        str(row["configuration"]): row
        for row in overall
        if row["suite"] == "sensitivity"
    }
    axes = {
        "bess_degradation_cost": (
            "tv_dro_bess_cost_20",
            ("tv_dro_bess_cost_10", "tv_dro_bess_cost_20", "tv_dro_bess_cost_40"),
        ),
        "effective_capacity_fraction": (
            "tv_dro_capacity_fraction_0p7",
            (
                "tv_dro_capacity_fraction_0p6",
                "tv_dro_capacity_fraction_0p7",
                "tv_dro_capacity_fraction_0p8",
            ),
        ),
    }
    metrics = (
        "mean_nominal_operating_cost_usd_across_windows",
        "mean_actual_operating_cost_usd_across_windows",
        "mean_actual_curtailment_mwh_across_windows",
        "mean_batch_adjustment_mwh_across_windows",
        "total_solve_wall_time_seconds",
    )
    output: list[dict[str, object]] = []
    for axis, (base_name, levels) in axes.items():
        base = by_name[base_name]
        for name in levels:
            current = by_name[name]
            for metric in metrics:
                base_value = float(base[metric])
                value = float(current[metric])
                difference = value - base_value
                output.append({
                    "axis": axis, "configuration": name,
                    "base_configuration": base_name, "metric": metric,
                    "value": value, "base_value": base_value,
                    "absolute_difference": difference,
                    "relative_difference_percent": (
                        100.0 * difference / base_value if base_value else 0.0
                    ),
                })
    return output


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DATA_PROCESSED / "scenarios" / "saa_scenarios_manifest.json")
    parser.add_argument("--calibration-csv", type=Path, default=DATA_PROCESSED / "scenarios" / "calibration_day_blocks_2024.csv")
    parser.add_argument("--aggregate-workload-csv", type=Path, default=DATA_PROCESSED / "workload" / "aggregate_workload_8d.csv")
    parser.add_argument("--envelope-csv", type=Path, default=DATA_PROCESSED / "workload" / "nominal_workload_30d.csv")
    parser.add_argument("--stats-json", type=Path, default=DATA_PROCESSED / "workload" / "workload_stats.json")
    parser.add_argument("--output-directory", type=Path, default=DATA_RESULTS / "comparison" / "unified_2025")
    parser.add_argument("--experiments", nargs="+", choices=EXPERIMENTS, default=list(EXPERIMENTS))
    parser.add_argument(
        "--methods",
        nargs="+",
        choices=METHODS,
        default=list(METHODS),
        help="run only selected optimization methods",
    )
    parser.add_argument(
        "--configurations",
        nargs="+",
        default=None,
        help=(
            "optional exact configuration names after experiment/method filtering; "
            "useful for low-cost diagnostics such as deterministic plus the "
            "nominal/nominal ablation"
        ),
    )
    parser.add_argument("--windows", nargs="+", choices=WINDOWS, default=list(WINDOWS))
    parser.add_argument(
        "--evaluation-energy-mode",
        choices=("actual_2025", "bootstrap_2024"),
        default="actual_2025",
        help=(
            "actual_2025 reuses the observed residual path within each window; "
            "bootstrap_2024 pairs each workload replay with a reproducible 30-day "
            "joint residual-block bootstrap for boundary discovery"
        ),
    )
    parser.add_argument(
        "--energy-residual-scale",
        type=float,
        default=1.0,
        help="positive multiplier for bootstrap_2024 energy residuals",
    )
    parser.add_argument(
        "--energy-replay-seed",
        type=int,
        default=ENERGY_REPLAY_SEED,
        help=(
            "energy block-bootstrap seed; use a new explicit value for an "
            "independent bootstrap confirmation run"
        ),
    )
    parser.add_argument("--replay-workers", type=int, default=4)
    parser.add_argument("--time-limit-seconds", type=float, default=300.0)
    parser.add_argument("--decomposition-max-iterations", type=int, default=8)
    parser.add_argument(
        "--solve-only",
        action="store_true",
        help="persist day-ahead plans without launching the 100-scenario replay",
    )
    parser.add_argument(
        "--grid-limit-fraction-of-peak",
        type=float,
        default=1.0,
        help="G_max / P_peak; use a separate output directory for stress runs",
    )
    parser.add_argument(
        "--ramp-limit-fraction-of-peak",
        type=float,
        default=0.10,
        help="R_max / P_peak; use a separate output directory for stress runs",
    )
    parser.add_argument(
        "--deterministic-constraint-headroom-fraction-of-peak",
        type=float,
        default=0.0,
        help=(
            "planning-only deterministic reserve subtracted from both G_max and "
            "R_max as a fraction of P_peak; replay evaluation keeps the physical "
            "grid and ramp boundaries unchanged"
        ),
    )
    return parser.parse_args()


def _write_available_summaries(
    output_directory: Path,
    run_path: Path,
    selected_configs: Sequence[Configuration],
    windows: Sequence[str],
    experiments: Sequence[str],
) -> None:
    """Write summaries for every currently complete four-window configuration."""

    window_summary, overall_summary = summarize(run_path, selected_configs, windows)
    _write_csv(output_directory / "window_summary.csv", window_summary)
    _write_csv(output_directory / "overall_summary.csv", overall_summary)
    if "ablation" in experiments and window_summary:
        ablation_rows = [
            row for row in _read_rows(run_path) if row["suite"] == "ablation"
        ]
        if len(ablation_rows) == 4 * len(windows) * 100:
            _write_csv(
                output_directory / "ablation_effects.csv",
                _ablation_effects(ablation_rows, windows),
            )
    if "sensitivity" in experiments and overall_summary:
        sensitivity_names = {
            row["configuration"]
            for row in overall_summary
            if row["suite"] == "sensitivity"
        }
        if len(sensitivity_names) == 6:
            _write_csv(
                output_directory / "sensitivity_effects.csv",
                _sensitivity_effects(overall_summary),
            )


def main() -> None:
    args = _parse_args()
    if args.replay_workers <= 0:
        raise ValueError("replay-workers must be positive")
    if args.decomposition_max_iterations <= 0:
        raise ValueError("decomposition-max-iterations must be positive")
    if not 0.0 < args.grid_limit_fraction_of_peak <= 1.0:
        raise ValueError("grid-limit-fraction-of-peak must be in (0, 1]")
    if not 0.0 < args.ramp_limit_fraction_of_peak <= 1.0:
        raise ValueError("ramp-limit-fraction-of-peak must be in (0, 1]")
    if args.deterministic_constraint_headroom_fraction_of_peak < 0.0:
        raise ValueError(
            "deterministic-constraint-headroom-fraction-of-peak must be nonnegative"
        )
    if args.energy_residual_scale <= 0.0:
        raise ValueError("energy-residual-scale must be positive")
    if args.evaluation_energy_mode == "actual_2025" and abs(args.energy_residual_scale - 1.0) > 1e-12:
        raise ValueError("energy-residual-scale is only valid with bootstrap_2024")
    if (
        args.evaluation_energy_mode == "actual_2025"
        and args.energy_replay_seed != ENERGY_REPLAY_SEED
    ):
        raise ValueError("energy-replay-seed is only valid with bootstrap_2024")
    experiments = tuple(dict.fromkeys(args.experiments))
    methods = tuple(dict.fromkeys(args.methods))
    windows = tuple(dict.fromkeys(args.windows))
    selected_configs = configurations(experiments, methods, args.configurations)
    if not selected_configs:
        raise ValueError("the selected experiments contain none of the selected methods")
    if (
        args.deterministic_constraint_headroom_fraction_of_peak > 0.0
        and any(config.method != "deterministic" for config in selected_configs)
    ):
        raise ValueError(
            "deterministic planning headroom requires a deterministic-only run"
        )
    args.output_directory.mkdir(parents=True, exist_ok=True)
    run_path = args.output_directory / "replay_runs.csv"
    config_path = args.output_directory / "run_config.json"
    energy_files = {
        window: DATA_PROCESSED / "energy" / "windows" / f"{window}_30d_d168_h3_energy.csv"
        for window in windows
    }
    config_payload = {
        "schema_version": 2,
        "protocol": "unified_2025_four_energy_windows_100_conditional_workload_replays_v1",
        "interpretation": "four energy observations; within each window 100 workload trajectories estimate conditional workload risk and are not independent energy samples",
        "experiments": list(experiments), "windows": list(windows),
        "parameters": {
            "numerical_protocol": "risk_count_lock_canonical_dayahead_v2",
            "energy_random_stream": "SeedSequence([energy_seed, scenario_id])",
            "day_ahead_tie_break": "sha256_variable_name_linear_v1",
            "day_ahead_cost_lock_tolerance_usd": 1e-4,
            "recourse_cost_lock_tolerance_usd": 1e-4,
            "recourse_deviation_lock_tolerance_mwh": 1e-5,
            "sample_size": SAMPLE_SIZE, "beta": BETA, "gamma": GAMMA,
            "gamma_energy_quantile": ENERGY_QUANTILE, "rho": RHO,
            "replay_workers": args.replay_workers,
            "time_limit_seconds_per_solve": args.time_limit_seconds,
            "decomposition_max_iterations": args.decomposition_max_iterations,
            "recourse_lexicographic_order": [
                "minimum_total_three_risk_violations", "minimum_DAM_grid_cost",
                "minimum_batch_adjustment", "minimum_total_grid_import",
            ],
            "configurations": [config.__dict__ for config in selected_configs],
        },
        "source_sha256": {
            "manifest": _sha256(args.manifest), "calibration_day_blocks": _sha256(args.calibration_csv),
            "aggregate_workload": _sha256(args.aggregate_workload_csv),
            "nominal_workload": _sha256(args.envelope_csv), "workload_stats": _sha256(args.stats_json),
            "scheduler_code": _sha256(PROJECT_ROOT / "alibaba2018_dro" / "scheduler.py"),
            "scenarios_code": _sha256(PROJECT_ROOT / "alibaba2018_dro" / "scenarios.py"),
            "runner_code": _sha256(Path(__file__)),
            **{f"energy_{window}": _sha256(path) for window, path in energy_files.items()},
        },
    }
    if args.evaluation_energy_mode == "bootstrap_2024":
        config_payload["protocol"] = (
            "unified_2025_forecast_contexts_100_joint_workload_energy_bootstrap_replays_v1"
        )
        config_payload["interpretation"] = (
            "four 2025 forecast contexts; within each window 100 workload trajectories "
            "are paired by scenario id with reproducible 2024 joint 24h energy-residual "
            "block bootstraps; this is a boundary-discovery stress experiment, not 100 "
            "independent observed 2025 energy samples"
        )
        config_payload["parameters"].update(
            {
                "evaluation_energy_mode": args.evaluation_energy_mode,
                "energy_replay_seed": args.energy_replay_seed,
                "energy_residual_scale": args.energy_residual_scale,
            }
        )
    # Non-default physical boundaries are explicit. Schema v2 intentionally
    # rejects resume from an older numerical/random-stream protocol directory.
    if abs(args.ramp_limit_fraction_of_peak - 0.10) > 1e-12:
        config_payload["parameters"]["ramp_limit_fraction_of_peak"] = (
            args.ramp_limit_fraction_of_peak
        )
    if abs(args.grid_limit_fraction_of_peak - 1.0) > 1e-12:
        config_payload["parameters"]["grid_limit_fraction_of_peak"] = (
            args.grid_limit_fraction_of_peak
        )
    if args.deterministic_constraint_headroom_fraction_of_peak > 0.0:
        config_payload["parameters"][
            "deterministic_constraint_headroom_fraction_of_peak"
        ] = args.deterministic_constraint_headroom_fraction_of_peak
    if config_path.exists() and json.loads(config_path.read_text(encoding="utf-8")) != config_payload:
        raise ValueError("existing run_config.json differs; use another output directory")
    if not config_path.exists():
        config_path.write_text(json.dumps(config_payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    base_training = load_saa_scenarios(
        manifest_path=args.manifest, calibration_csv=args.calibration_csv,
        workload_csv=args.aggregate_workload_csv, split="final_training", scenario_count=SAMPLE_SIZE,
    )
    replay_templates = load_workload_replay_scenarios(
        manifest_path=args.manifest, workload_csv=args.aggregate_workload_csv,
    )
    if len(replay_templates) != 100:
        raise ValueError("the registered 2025 workload replay set must contain 100 trajectories")
    bootstrap_replay_scenarios = (
        attach_bootstrap_energy_replay(
            replay_templates,
            calibration_csv=args.calibration_csv,
            energy_seed=args.energy_replay_seed,
            residual_scale=args.energy_residual_scale,
        )
        if args.evaluation_energy_mode == "bootstrap_2024"
        else None
    )
    solar_24, wind_24 = load_hourly_downward_residual_quantiles(
        args.calibration_csv, quantile=ENERGY_QUANTILE,
    )
    existing = _read_rows(run_path)
    counts: dict[tuple[str, str, str], int] = {}
    for row in existing:
        key = (row["suite"], row["configuration"], row["window"])
        counts[key] = counts.get(key, 0) + 1
    partial = {key: count for key, count in counts.items() if count != 100}
    if partial:
        raise ValueError(f"replay_runs.csv contains incomplete groups: {partial}")
    if existing:
        _write_available_summaries(
            args.output_directory, run_path, selected_configs, windows, experiments
        )

    solve_cache: dict[
        tuple[tuple[object, ...], str, float],
        tuple[SolvedPlan, dict[str, float], list[dict[str, object]]],
    ] = {}
    for config in selected_configs:
        for window in windows:
            output_key = (config.suite, config.name, window)
            if counts.get(output_key) == 100:
                print("skip:", *output_key, flush=True)
                continue
            cache_key = (
                config.solve_signature,
                window,
                args.deterministic_constraint_headroom_fraction_of_peak,
            )
            if cache_key not in solve_cache:
                print("solve_start:", config.name, window, flush=True)
                inputs = build_hourly_input(
                    energy_files[window], args.envelope_csv, args.stats_json,
                    effective_capacity_fraction=config.effective_capacity_fraction,
                )
                try:
                    solved, common = _solve(
                        config, inputs, base_training, solar_24, wind_24,
                        time_limit_seconds=args.time_limit_seconds,
                        max_iterations=args.decomposition_max_iterations,
                        replay_workers=args.replay_workers,
                        grid_limit_fraction_of_peak=(
                            args.grid_limit_fraction_of_peak
                        ),
                        ramp_limit_fraction_of_peak=(
                            args.ramp_limit_fraction_of_peak
                        ),
                        deterministic_constraint_headroom_fraction_of_peak=(
                            args.deterministic_constraint_headroom_fraction_of_peak
                        ),
                    )
                except RuntimeError as error:
                    _record_failure(
                        args.output_directory / "failures.csv",
                        {
                            "suite": config.suite,
                            "configuration": config.name,
                            "method": config.method,
                            "window": window,
                            "grid_limit_fraction_of_peak": (
                                args.grid_limit_fraction_of_peak
                            ),
                            "ramp_limit_fraction_of_peak": (
                                args.ramp_limit_fraction_of_peak
                            ),
                            "deterministic_constraint_headroom_fraction_of_peak": (
                                args.deterministic_constraint_headroom_fraction_of_peak
                            ),
                            "phase": "solve",
                            "error": str(error),
                        },
                    )
                    raise
                _write_plan(
                    args.output_directory, config, window, solved, common
                )
                if args.solve_only:
                    print(
                        "solve_only_done:", config.name, window,
                        f"cost={solved.plan.operating_cost:.9f}", flush=True,
                    )
                    continue
                evaluation_scenarios = (
                    bootstrap_replay_scenarios
                    if bootstrap_replay_scenarios is not None
                    else [_actual_scenario(template, inputs) for template in replay_templates]
                )
                print(
                    "replay_start:", config.name, window, "count=100",
                    f"solve_wall={solved.wall_time_seconds:.3f}s", flush=True,
                )
                try:
                    cached_rows = _replay_rows(
                        config, window, inputs, solved, common,
                        evaluation_scenarios,
                        replay_workers=args.replay_workers,
                    )
                except RuntimeError as error:
                    _record_failure(
                        args.output_directory / "failures.csv",
                        {
                            "suite": config.suite,
                            "configuration": config.name,
                            "method": config.method,
                            "window": window,
                            "grid_limit_fraction_of_peak": (
                                args.grid_limit_fraction_of_peak
                            ),
                            "ramp_limit_fraction_of_peak": (
                                args.ramp_limit_fraction_of_peak
                            ),
                            "deterministic_constraint_headroom_fraction_of_peak": (
                                args.deterministic_constraint_headroom_fraction_of_peak
                            ),
                            "phase": "replay",
                            "error": str(error),
                        },
                    )
                    raise
                solve_cache[cache_key] = (solved, common, cached_rows)
            _, _, source_rows = solve_cache[cache_key]
            rows = [
                {**row, "suite": config.suite, "configuration": config.name}
                for row in source_rows
            ]
            _append_rows(run_path, rows)
            counts[output_key] = 100
            print("done:", *output_key, flush=True)
            _write_available_summaries(
                args.output_directory,
                run_path,
                selected_configs,
                windows,
                experiments,
            )

    _write_available_summaries(
        args.output_directory, run_path, selected_configs, windows, experiments
    )
    if args.solve_only:
        _write_plan_equivalence_summary(
            args.output_directory, selected_configs, windows
        )
        print("written:", args.output_directory / "day_ahead_plans", flush=True)
        print(
            "written:", args.output_directory / "plan_equivalence_summary.csv",
            flush=True,
        )
    else:
        print("written:", run_path, flush=True)
        print("written:", args.output_directory / "window_summary.csv", flush=True)
        print("written:", args.output_directory / "overall_summary.csv", flush=True)


if __name__ == "__main__":
    main()
