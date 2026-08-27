"""风光储—柔性算力—碳预算的日前调度与实际回放。"""

from __future__ import annotations

import math
import time
from concurrent.futures import ProcessPoolExecutor
from functools import partial
from dataclasses import dataclass, replace
from typing import Sequence

from .config import (
    BESS_DEGRADATION_COST_USD_PER_MWH_THROUGHPUT,
    DEFAULT_CARBON_BUDGET_REDUCTION,
    SOLAR_REFERENCE_MWH,
    WIND_REFERENCE_MWH,
)
from .inputs import HourlyInput
from .scenarios import ScenarioRealization

try:  # PySCIPOpt 只在 scip_env 里；其它环境仍可导入数据工具。
    from pyscipopt import Model
except ImportError:  # pragma: no cover
    Model = None

try:  # 碳排放 LP 对偶割使用 HiGHS 的稳定 marginal 接口。
    from scipy.optimize import linprog
except ImportError:  # pragma: no cover
    linprog = None


LBS_PER_KG = 0.45359237


@dataclass(frozen=True)
class DayAheadResult:
    """风光储—碳预算日前计划；所有时段均为 1 小时。"""

    baseline_cost: float
    baseline_carbon_kg: float
    grid_cost: float
    bess_degradation_cost: float
    operating_cost: float
    cost_reduction: float
    forecast_carbon_kg: float
    carbon_budget_kg: float
    carbon_budget_slack_kg: float
    batch: list[float]
    grid: list[float]
    bess_charge: list[float]
    bess_discharge: list[float]
    pv_generation: list[float]
    wind_generation: list[float]
    curtailment: list[float]
    feasible: bool = True


@dataclass(frozen=True)
class ActualReplayResult:
    """固定日前 BESS/批处理计划下，以实际风光和碳强度进行的事后核算。"""

    grid: list[float]
    curtailment: list[float]
    pv_generation: list[float]
    wind_generation: list[float]
    grid_cost: float
    operating_cost: float
    carbon_kg: float
    carbon_budget_violation_kg: float
    grid_limit_violation_hours: int
    ramp_violation_hours: int


@dataclass(frozen=True)
class SaaDayAheadResult:
    """SAA 日前计划及其在训练场景上的逐通道违约频率。"""

    plan: DayAheadResult
    scenario_count: int
    workload_violation_rate: float
    carbon_violation_rate: float
    grid_limit_violation_rate: float
    ramp_violation_rate: float
    mean_batch_adjustment_mwh: float = 0.0
    decomposition_iterations: int = 0
    active_scenario_count: int = 0
    carbon_cut_count: int = 0
    carbon_cut_violation_lower_bound: int | None = None
    solver_status: str = "unknown"
    runtime_seconds: float = 0.0
    mip_gap: float = math.inf

    @property
    def feasible(self) -> bool:
        return self.plan.feasible


@dataclass(frozen=True)
class ScenarioReplayResult:
    """日前 BESS 固定、批处理有限追索下的联合场景回放。"""

    batch: list[float]
    batch_adjustment_mwh: float
    grid: list[float]
    curtailment: list[float]
    grid_cost: float
    operating_cost: float
    carbon_kg: float
    workload_violation: bool
    carbon_violation: bool
    grid_limit_violation: bool
    ramp_violation: bool


@dataclass(frozen=True)
class CarbonBendersCut:
    """场景最小碳排放值函数在一个日前计划处的支撑切平面。"""

    scenario_index: int
    intercept_kg: float
    charge_gradient_kg_per_mw: tuple[float, ...]
    discharge_gradient_kg_per_mw: tuple[float, ...]
    big_m_kg: float


@dataclass(frozen=True)
class CarbonSubproblemResult:
    """固定日前 BESS 后的场景最小碳 LP 结果及其次梯度。"""

    feasible: bool
    minimum_carbon_kg: float
    cut: CarbonBendersCut | None
    solver_status: str


def _infeasible_day_ahead_result() -> DayAheadResult:
    return DayAheadResult(
        baseline_cost=0.0,
        baseline_carbon_kg=0.0,
        grid_cost=0.0,
        bess_degradation_cost=0.0,
        operating_cost=0.0,
        cost_reduction=0.0,
        forecast_carbon_kg=0.0,
        carbon_budget_kg=0.0,
        carbon_budget_slack_kg=0.0,
        batch=[],
        grid=[],
        bess_charge=[],
        bess_discharge=[],
        pv_generation=[],
        wind_generation=[],
        curtailment=[],
        feasible=False,
    )


def bess_degradation_cost(
    charge_mw: list[float],
    discharge_mw: list[float],
    cost_usd_per_mwh_throughput: float,
) -> float:
    """计算 1 小时时段下按累计充、放总吞吐量计的 BESS 衰减成本。"""

    if len(charge_mw) != len(discharge_mw):
        raise ValueError("charge_mw and discharge_mw must have the same length")
    if cost_usd_per_mwh_throughput < 0.0:
        raise ValueError("cost_usd_per_mwh_throughput must be non-negative")
    return cost_usd_per_mwh_throughput * sum(
        charge + discharge for charge, discharge in zip(charge_mw, discharge_mw)
    )


def _local_resource_profile(
    values_mwh: list[float],
    *,
    capacity_mw: float,
    reference_mwh: float,
    label: str,
) -> list[float]:
    """以固定 EIA ERCO 参考值缩放为反事实本地可再生出力。"""

    if capacity_mw < 0.0:
        raise ValueError(f"{label}_capacity_mw must be non-negative")
    if reference_mwh <= 0.0:
        raise ValueError(f"{label}_reference_mwh must be positive")
    return [
        capacity_mw * min(1.0, max(0.0, value) / reference_mwh)
        for value in values_mwh
    ]


def _resource_profiles(
    inputs: list[HourlyInput],
    *,
    pv_capacity_mw: float,
    wind_capacity_mw: float,
    solar_reference_mwh: float,
    wind_reference_mwh: float,
    actual: bool,
) -> tuple[list[float], list[float]]:
    """返回本地 PV 和风电可用出力；actual=False 使用日前预测。"""

    if actual:
        solar = [item.actual_erco_solar_generation_mwh for item in inputs]
        wind = [item.actual_erco_wind_generation_mwh for item in inputs]
    else:
        solar = [item.forecast_erco_solar_generation_mwh for item in inputs]
        wind = [item.forecast_erco_wind_generation_mwh for item in inputs]
    return (
        _local_resource_profile(
            solar,
            capacity_mw=pv_capacity_mw,
            reference_mwh=solar_reference_mwh,
            label="pv",
        ),
        _local_resource_profile(
            wind,
            capacity_mw=wind_capacity_mw,
            reference_mwh=wind_reference_mwh,
            label="wind",
        ),
    )


def _carbon_kg(grid_mw: list[float], carbon_lbs_per_kwh: list[float]) -> float:
    """计算消费侧平均碳核算量（kgCO2）。"""

    return sum(
        grid * 1000.0 * carbon * LBS_PER_KG
        for grid, carbon in zip(grid_mw, carbon_lbs_per_kwh, strict=True)
    )


def solve_wind_solar_storage(
    inputs: list[HourlyInput],
    *,
    g_max_mw: float,
    r_max_mw: float,
    p_grid_initial_mw: float,
    bess_power_mw: float,
    bess_energy_mwh: float,
    pv_capacity_mw: float,
    wind_capacity_mw: float,
    carbon_budget_reduction: float = DEFAULT_CARBON_BUDGET_REDUCTION,
    solar_reference_mwh: float = SOLAR_REFERENCE_MWH,
    wind_reference_mwh: float = WIND_REFERENCE_MWH,
    bess_efficiency: float = 0.90,
    soc_min: float = 0.10,
    soc_max: float = 0.90,
    soc_initial: float = 0.50,
    bess_degradation_cost_usd_per_mwh_throughput: float = BESS_DEGRADATION_COST_USD_PER_MWH_THROUGHPUT,
) -> DayAheadResult:
    """求解预测风光、BESS、有效容量和预测碳预算的日前 MILP。

    碳预算基准使用相同风光容量、固定批处理时序和未启用 BESS 的预测购电。
    """

    if Model is None:
        raise RuntimeError("pyscipopt is required; run inside scip_env")
    if not inputs:
        raise ValueError("inputs must not be empty")
    if min(g_max_mw, r_max_mw, bess_power_mw, bess_energy_mwh) < 0.0:
        raise ValueError("grid and BESS capacities must be non-negative")
    if not 0.0 <= carbon_budget_reduction < 1.0:
        raise ValueError("carbon_budget_reduction must be in [0, 1)")
    if not 0.0 < bess_efficiency <= 1.0:
        raise ValueError("bess_efficiency must be in (0, 1]")
    if not 0.0 <= soc_min <= soc_initial <= soc_max <= 1.0:
        raise ValueError("SOC values must satisfy 0 <= min <= initial <= max <= 1")
    if bess_degradation_cost_usd_per_mwh_throughput < 0.0:
        raise ValueError("bess_degradation_cost_usd_per_mwh_throughput must be non-negative")

    hours = len(inputs)
    p_must = inputs[0].online_mw + inputs[0].base_mw
    pv, wind = _resource_profiles(
        inputs,
        pv_capacity_mw=pv_capacity_mw,
        wind_capacity_mw=wind_capacity_mw,
        solar_reference_mwh=solar_reference_mwh,
        wind_reference_mwh=wind_reference_mwh,
        actual=False,
    )
    baseline_grid = [
        max(0.0, p_must + item.batch_baseline_mwh - pv[t] - wind[t])
        for t, item in enumerate(inputs)
    ]
    prices = [item.dam_lz_houston_usd_per_mwh for item in inputs]
    forecast_carbon = [item.forecast_consumed_co2_lbs_per_kwh for item in inputs]
    baseline_cost = sum(price * grid for price, grid in zip(prices, baseline_grid))
    baseline_carbon_kg = _carbon_kg(baseline_grid, forecast_carbon)
    carbon_budget_kg = (1.0 - carbon_budget_reduction) * baseline_carbon_kg

    model = Model("wind_solar_storage_carbon_budget")
    model.hideOutput()
    batch = {
        t: model.addVar(lb=0.0, ub=inputs[t].batch_window_mwh, name=f"batch_{t}")
        for t in range(hours)
    }
    grid = {
        t: model.addVar(lb=0.0, ub=g_max_mw, name=f"grid_{t}")
        for t in range(hours)
    }
    curtailment = {
        t: model.addVar(lb=0.0, ub=pv[t] + wind[t], name=f"curtailment_{t}")
        for t in range(hours)
    }
    p_ch = {
        t: model.addVar(lb=0.0, ub=bess_power_mw, name=f"pch_{t}")
        for t in range(hours)
    }
    p_dis = {
        t: model.addVar(lb=0.0, ub=bess_power_mw, name=f"pdis_{t}")
        for t in range(hours)
    }
    eta = math.sqrt(bess_efficiency)
    energy = {
        t: model.addVar(
            lb=soc_min * bess_energy_mwh,
            ub=soc_max * bess_energy_mwh,
            name=f"bess_energy_{t}",
        )
        for t in range(hours + 1)
    }
    model.addCons(energy[0] == soc_initial * bess_energy_mwh)
    for t in range(hours):
        if inputs[t].batch_capacity_mw is not None:
            model.addCons(
                batch[t] <= inputs[t].batch_capacity_mw,
                name=f"effective_capacity_{t}",
            )
        bess_mode = model.addVar(vtype="B", name=f"bess_charge_mode_{t}")
        model.addCons(p_ch[t] <= bess_power_mw * bess_mode)
        model.addCons(p_dis[t] <= bess_power_mw * (1.0 - bess_mode))
        model.addCons(
            energy[t + 1] == energy[t] + eta * p_ch[t] - p_dis[t] / eta,
            name=f"soc_{t}",
        )
        model.addCons(
            grid[t]
            == p_must
            + batch[t]
            + p_ch[t]
            - p_dis[t]
            - pv[t]
            - wind[t]
            + curtailment[t],
            name=f"power_balance_{t}",
        )
    model.addCons(energy[hours] == energy[0], name="terminal_soc")
    model.addCons(
        sum(batch[t] for t in range(hours))
        == sum(item.batch_baseline_mwh for item in inputs),
        name="batch_energy_conservation",
    )
    has_cumulative_envelope = all(
        item.batch_cumulative_arrived_mwh is not None
        and item.batch_cumulative_due_mwh is not None
        for item in inputs
    )
    if has_cumulative_envelope:
        cumulative_batch = 0.0
        for t in range(hours):
            cumulative_batch += batch[t]
            model.addCons(
                cumulative_batch
                <= float(inputs[t].batch_cumulative_arrived_mwh),
                name=f"batch_arrival_envelope_{t}",
            )
            model.addCons(
                cumulative_batch >= float(inputs[t].batch_cumulative_due_mwh),
                name=f"batch_due_envelope_{t}",
            )
    previous_grid = p_grid_initial_mw
    for t in range(hours):
        model.addCons(grid[t] - previous_grid <= r_max_mw, name=f"ramp_up_{t}")
        model.addCons(grid[t] - previous_grid >= -r_max_mw, name=f"ramp_down_{t}")
        previous_grid = grid[t]

    forecast_carbon_expr = sum(
        forecast_carbon[t] * 1000.0 * LBS_PER_KG * grid[t]
        for t in range(hours)
    )
    model.addCons(
        forecast_carbon_expr <= carbon_budget_kg,
        name="forecast_carbon_budget",
    )
    grid_cost_expr = sum(prices[t] * grid[t] for t in range(hours))
    degradation_cost_expr = (
        bess_degradation_cost_usd_per_mwh_throughput
        * sum(p_ch[t] + p_dis[t] for t in range(hours))
    )
    model.setObjective(grid_cost_expr + degradation_cost_expr, "minimize")
    model.optimize()
    if model.getStatus() != "optimal":
        return _infeasible_day_ahead_result()

    batch_values = [model.getVal(batch[t]) for t in range(hours)]
    grid_values = [model.getVal(grid[t]) for t in range(hours)]
    charge_values = [model.getVal(p_ch[t]) for t in range(hours)]
    discharge_values = [model.getVal(p_dis[t]) for t in range(hours)]
    curtailment_values = [model.getVal(curtailment[t]) for t in range(hours)]
    grid_cost = sum(price * value for price, value in zip(prices, grid_values))
    degradation_cost = bess_degradation_cost(
        charge_values,
        discharge_values,
        bess_degradation_cost_usd_per_mwh_throughput,
    )
    operating_cost = grid_cost + degradation_cost
    forecast_carbon_kg = _carbon_kg(grid_values, forecast_carbon)
    return DayAheadResult(
        baseline_cost=baseline_cost,
        baseline_carbon_kg=baseline_carbon_kg,
        grid_cost=grid_cost,
        bess_degradation_cost=degradation_cost,
        operating_cost=operating_cost,
        cost_reduction=(
            (baseline_cost - operating_cost) / baseline_cost if baseline_cost else 0.0
        ),
        forecast_carbon_kg=forecast_carbon_kg,
        carbon_budget_kg=carbon_budget_kg,
        carbon_budget_slack_kg=carbon_budget_kg - forecast_carbon_kg,
        batch=batch_values,
        grid=grid_values,
        bess_charge=charge_values,
        bess_discharge=discharge_values,
        pv_generation=pv,
        wind_generation=wind,
        curtailment=curtailment_values,
        feasible=True,
    )


def solve_saa_wind_solar_storage(
    inputs: list[HourlyInput],
    scenarios: Sequence[ScenarioRealization],
    *,
    g_max_mw: float,
    r_max_mw: float,
    p_grid_initial_mw: float,
    bess_power_mw: float,
    bess_energy_mwh: float,
    pv_capacity_mw: float,
    wind_capacity_mw: float,
    carbon_budget_reduction: float = DEFAULT_CARBON_BUDGET_REDUCTION,
    beta_workload: float = 0.10,
    beta_carbon: float = 0.10,
    beta_grid: float = 0.10,
    beta_ramp: float = 0.10,
    solar_reference_mwh: float = SOLAR_REFERENCE_MWH,
    wind_reference_mwh: float = WIND_REFERENCE_MWH,
    bess_efficiency: float = 0.90,
    soc_min: float = 0.10,
    soc_max: float = 0.90,
    soc_initial: float = 0.50,
    bess_degradation_cost_usd_per_mwh_throughput: float = BESS_DEGRADATION_COST_USD_PER_MWH_THROUGHPUT,
    time_limit_seconds: float | None = None,
    chance_sample_size: int | None = None,
    initial_plan: DayAheadResult | None = None,
    scenario_indices: Sequence[int] | None = None,
    carbon_cuts: Sequence[CarbonBendersCut] = (),
    minimize_carbon_violations: bool = False,
) -> SaaDayAheadResult:
    """求解带有限批处理追索的四通道等概率 SAA 机会约束。

    日前 BESS 动作和名义批处理参考跨场景共享；场景批处理只能在该场景
    的释放/截止包络和物理容量内调整。名义成本最优后，依次最小化批处理
    调整量和场景购电量，不引入人为加权系数。
    """

    if Model is None:
        raise RuntimeError("pyscipopt is required; run inside scip_env")
    if not inputs:
        raise ValueError("inputs must not be empty")
    if not scenarios:
        raise ValueError("scenarios must not be empty")
    if chance_sample_size is not None and chance_sample_size < len(scenarios):
        raise ValueError("chance_sample_size must cover every active scenario")
    if scenario_indices is None:
        scenario_indices = tuple(range(len(scenarios)))
    if len(scenario_indices) != len(scenarios):
        raise ValueError("scenario_indices must match the active scenarios")
    if len(set(scenario_indices)) != len(scenario_indices):
        raise ValueError("scenario_indices must be unique")
    if initial_plan is not None and len(initial_plan.batch) != len(inputs):
        raise ValueError("initial_plan must match the input horizon")
    if min(g_max_mw, r_max_mw, bess_power_mw, bess_energy_mwh) < 0.0:
        raise ValueError("grid and BESS capacities must be non-negative")
    if not 0.0 <= carbon_budget_reduction < 1.0:
        raise ValueError("carbon_budget_reduction must be in [0, 1)")
    if any(
        not 0.0 <= beta <= 1.0
        for beta in (beta_workload, beta_carbon, beta_grid, beta_ramp)
    ):
        raise ValueError("all SAA violation rates must be in [0, 1]")
    if not 0.0 < bess_efficiency <= 1.0:
        raise ValueError("bess_efficiency must be in (0, 1]")
    if not 0.0 <= soc_min <= soc_initial <= soc_max <= 1.0:
        raise ValueError("SOC values must satisfy 0 <= min <= initial <= max <= 1")
    if bess_degradation_cost_usd_per_mwh_throughput < 0.0:
        raise ValueError("bess_degradation_cost_usd_per_mwh_throughput must be non-negative")
    if time_limit_seconds is not None and time_limit_seconds <= 0.0:
        raise ValueError("time_limit_seconds must be positive when provided")

    hours = len(inputs)
    for scenario in scenarios:
        scenario_lengths = (
            len(scenario.cumulative_arrived_core_hours),
            len(scenario.cumulative_due_core_hours),
            len(scenario.residual_solar_mwh),
            len(scenario.residual_wind_mwh),
            len(scenario.residual_carbon_lbs_per_kwh),
        )
        if any(length != hours for length in scenario_lengths):
            raise ValueError("every SAA scenario must match the input horizon")
    conversions = {item.workload_mwh_per_core_hour for item in inputs}
    if len(conversions) != 1 or next(iter(conversions)) <= 0.0:
        raise ValueError(
            "inputs must carry one positive workload_mwh_per_core_hour conversion"
        )
    workload_conversion = next(iter(conversions))

    p_must = inputs[0].online_mw + inputs[0].base_mw
    pv, wind = _resource_profiles(
        inputs,
        pv_capacity_mw=pv_capacity_mw,
        wind_capacity_mw=wind_capacity_mw,
        solar_reference_mwh=solar_reference_mwh,
        wind_reference_mwh=wind_reference_mwh,
        actual=False,
    )
    baseline_grid = [
        max(0.0, p_must + item.batch_baseline_mwh - pv[t] - wind[t])
        for t, item in enumerate(inputs)
    ]
    prices = [item.dam_lz_houston_usd_per_mwh for item in inputs]
    forecast_carbon = [item.forecast_consumed_co2_lbs_per_kwh for item in inputs]
    baseline_cost = sum(price * grid for price, grid in zip(prices, baseline_grid))
    baseline_carbon_kg = _carbon_kg(baseline_grid, forecast_carbon)
    carbon_budget_kg = (1.0 - carbon_budget_reduction) * baseline_carbon_kg
    batch_total_mwh = sum(item.batch_baseline_mwh for item in inputs)

    scenario_workload_total = (
        scenarios[0].cumulative_arrived_core_hours[-1] * workload_conversion
    )
    if not math.isclose(
        scenario_workload_total,
        batch_total_mwh,
        rel_tol=1e-8,
        abs_tol=1e-6,
    ):
        raise ValueError("SAA workload total and nominal batch total must match")

    model = Model("saa_wind_solar_storage")
    model.hideOutput()
    if time_limit_seconds is not None:
        model.setRealParam("limits/time", time_limit_seconds)
    batch = {
        t: model.addVar(lb=0.0, ub=inputs[t].batch_window_mwh, name=f"batch_{t}")
        for t in range(hours)
    }
    grid = {
        t: model.addVar(lb=0.0, ub=g_max_mw, name=f"grid_{t}")
        for t in range(hours)
    }
    curtailment = {
        t: model.addVar(lb=0.0, ub=pv[t] + wind[t], name=f"curtailment_{t}")
        for t in range(hours)
    }
    p_ch = {
        t: model.addVar(lb=0.0, ub=bess_power_mw, name=f"pch_{t}")
        for t in range(hours)
    }
    p_dis = {
        t: model.addVar(lb=0.0, ub=bess_power_mw, name=f"pdis_{t}")
        for t in range(hours)
    }
    eta = math.sqrt(bess_efficiency)
    energy = {
        t: model.addVar(
            lb=soc_min * bess_energy_mwh,
            ub=soc_max * bess_energy_mwh,
            name=f"bess_energy_{t}",
        )
        for t in range(hours + 1)
    }
    model.addCons(energy[0] == soc_initial * bess_energy_mwh)
    for t in range(hours):
        if inputs[t].batch_capacity_mw is not None:
            model.addCons(batch[t] <= inputs[t].batch_capacity_mw, name=f"effective_capacity_{t}")
        # SAA 主问题使用充放功率凸包，并在返回前审计互补性。当前校准
        # 电价下，正的吞吐衰减成本使同时充放严格劣于净充/放等价解。
        model.addCons(
            p_ch[t] + p_dis[t] <= bess_power_mw,
            name=f"bess_charge_discharge_convex_hull_{t}",
        )
        model.addCons(
            energy[t + 1] == energy[t] + eta * p_ch[t] - p_dis[t] / eta,
            name=f"soc_{t}",
        )
        model.addCons(
            grid[t]
            == p_must + batch[t] + p_ch[t] - p_dis[t] - pv[t] - wind[t] + curtailment[t],
            name=f"power_balance_{t}",
        )
    model.addCons(energy[hours] == energy[0], name="terminal_soc")
    model.addCons(sum(batch[t] for t in range(hours)) == batch_total_mwh, name="batch_energy_conservation")
    if all(
        item.batch_cumulative_arrived_mwh is not None
        and item.batch_cumulative_due_mwh is not None
        for item in inputs
    ):
        cumulative_batch = 0.0
        for t in range(hours):
            cumulative_batch += batch[t]
            model.addCons(
                cumulative_batch <= float(inputs[t].batch_cumulative_arrived_mwh),
                name=f"batch_arrival_envelope_{t}",
            )
            model.addCons(
                cumulative_batch >= float(inputs[t].batch_cumulative_due_mwh),
                name=f"batch_due_envelope_{t}",
            )
    previous_grid = p_grid_initial_mw
    for t in range(hours):
        model.addCons(grid[t] - previous_grid <= r_max_mw, name=f"ramp_up_{t}")
        model.addCons(grid[t] - previous_grid >= -r_max_mw, name=f"ramp_down_{t}")
        previous_grid = grid[t]

    forecast_carbon_expr = sum(
        forecast_carbon[t] * 1000.0 * LBS_PER_KG * grid[t]
        for t in range(hours)
    )
    model.addCons(forecast_carbon_expr <= carbon_budget_kg, name="forecast_carbon_budget")
    grid_cost_expr = sum(prices[t] * grid[t] for t in range(hours))
    degradation_cost_expr = bess_degradation_cost_usd_per_mwh_throughput * sum(
        p_ch[t] + p_dis[t] for t in range(hours)
    )
    primary_objective = grid_cost_expr + degradation_cost_expr

    fallback_batch_capacity_mw = max(item.batch_window_mwh for item in inputs)
    scenario_batch_capacity_mw = {
        t: (
            inputs[t].batch_capacity_mw
            if inputs[t].batch_capacity_mw is not None
            else fallback_batch_capacity_mw
        )
        for t in range(hours)
    }
    batch_upper_mw = max(scenario_batch_capacity_mw.values())
    scenario_grid_upper_mw = p_must + batch_upper_mw + bess_power_mw
    workload_big_m = max(batch_total_mwh, scenario_workload_total)
    grid_big_m = scenario_grid_upper_mw
    ramp_big_m = 2.0 * scenario_grid_upper_mw + abs(p_grid_initial_mw)
    scenario_grid: dict[tuple[int, int], object] = {}
    scenario_batch: dict[tuple[int, int], object] = {}
    batch_deviation: dict[tuple[int, int], object] = {}
    violation_workload: dict[int, object] = {}
    violation_carbon: dict[int, object] = {}
    violation_grid: dict[int, object] = {}
    violation_ramp: dict[int, object] = {}
    def carbon_violation_var(scenario_index: int) -> object:
        if scenario_index not in violation_carbon:
            violation_carbon[scenario_index] = model.addVar(
                vtype="B", name=f"violate_carbon_{scenario_index}"
            )
        return violation_carbon[scenario_index]

    for scenario_index, scenario in zip(scenario_indices, scenarios):
        solar = _local_resource_profile(
            [
                inputs[t].forecast_erco_solar_generation_mwh
                + scenario.residual_solar_mwh[t]
                for t in range(hours)
            ],
            capacity_mw=pv_capacity_mw,
            reference_mwh=solar_reference_mwh,
            label="scenario_pv",
        )
        scenario_wind = _local_resource_profile(
            [
                inputs[t].forecast_erco_wind_generation_mwh
                + scenario.residual_wind_mwh[t]
                for t in range(hours)
            ],
            capacity_mw=wind_capacity_mw,
            reference_mwh=wind_reference_mwh,
            label="scenario_wind",
        )
        scenario_carbon = [
            max(
                0.0,
                inputs[t].forecast_consumed_co2_lbs_per_kwh
                + scenario.residual_carbon_lbs_per_kwh[t],
            )
            for t in range(hours)
        ]
        violation_workload[scenario_index] = model.addVar(
            vtype="B", name=f"violate_workload_{scenario_index}"
        )
        carbon_violation_var(scenario_index)
        violation_grid[scenario_index] = model.addVar(
            vtype="B", name=f"violate_grid_{scenario_index}"
        )
        violation_ramp[scenario_index] = model.addVar(
            vtype="B", name=f"violate_ramp_{scenario_index}"
        )
        cumulative_batch = 0.0
        carbon_expr = 0.0
        carbon_big_m = sum(
            scenario_carbon[t] * 1000.0 * LBS_PER_KG * scenario_grid_upper_mw
            for t in range(hours)
        )
        scenario_previous_grid: object = p_grid_initial_mw
        for t in range(hours):
            scenario_batch[scenario_index, t] = model.addVar(
                lb=0.0,
                ub=scenario_batch_capacity_mw[t],
                name=f"saa_batch_{scenario_index}_{t}",
            )
            deviation_positive = model.addVar(
                lb=0.0, name=f"saa_batch_dev_pos_{scenario_index}_{t}"
            )
            deviation_negative = model.addVar(
                lb=0.0, name=f"saa_batch_dev_neg_{scenario_index}_{t}"
            )
            model.addCons(
                scenario_batch[scenario_index, t] - batch[t]
                == deviation_positive - deviation_negative,
                name=f"saa_batch_deviation_{scenario_index}_{t}",
            )
            batch_deviation[scenario_index, t] = (
                deviation_positive + deviation_negative
            )
            cumulative_batch += scenario_batch[scenario_index, t]
            arrived_mwh = (
                scenario.cumulative_arrived_core_hours[t] * workload_conversion
            )
            due_mwh = scenario.cumulative_due_core_hours[t] * workload_conversion
            model.addCons(
                cumulative_batch <= arrived_mwh + workload_big_m * violation_workload[scenario_index],
                name=f"saa_arrival_{scenario_index}_{t}",
            )
            model.addCons(
                cumulative_batch >= due_mwh - workload_big_m * violation_workload[scenario_index],
                name=f"saa_due_{scenario_index}_{t}",
            )
            scenario_grid[scenario_index, t] = model.addVar(
                lb=0.0,
                ub=scenario_grid_upper_mw,
                name=f"saa_grid_{scenario_index}_{t}",
            )
            scenario_curtailment = model.addVar(
                lb=0.0,
                ub=solar[t] + scenario_wind[t],
                name=f"saa_curtailment_{scenario_index}_{t}",
            )
            model.addCons(
                scenario_grid[scenario_index, t]
                == p_must
                + scenario_batch[scenario_index, t]
                + p_ch[t]
                - p_dis[t]
                - solar[t]
                - scenario_wind[t]
                + scenario_curtailment,
                name=f"saa_power_balance_{scenario_index}_{t}",
            )
            model.addCons(
                scenario_grid[scenario_index, t]
                <= g_max_mw + grid_big_m * violation_grid[scenario_index],
                name=f"saa_grid_limit_{scenario_index}_{t}",
            )
            model.addCons(
                scenario_grid[scenario_index, t] - scenario_previous_grid
                <= r_max_mw + ramp_big_m * violation_ramp[scenario_index],
                name=f"saa_ramp_up_{scenario_index}_{t}",
            )
            model.addCons(
                scenario_grid[scenario_index, t] - scenario_previous_grid
                >= -r_max_mw - ramp_big_m * violation_ramp[scenario_index],
                name=f"saa_ramp_down_{scenario_index}_{t}",
            )
            scenario_previous_grid = scenario_grid[scenario_index, t]
            carbon_expr += (
                scenario_carbon[t]
                * 1000.0
                * LBS_PER_KG
                * scenario_grid[scenario_index, t]
            )
        model.addCons(
            sum(scenario_batch[scenario_index, t] for t in range(hours))
            == batch_total_mwh,
            name=f"saa_batch_energy_conservation_{scenario_index}",
        )
        model.addCons(
            carbon_expr <= carbon_budget_kg + carbon_big_m * violation_carbon[scenario_index],
            name=f"saa_carbon_{scenario_index}",
        )

    for cut_index, cut in enumerate(carbon_cuts):
        if len(cut.charge_gradient_kg_per_mw) != hours or len(
            cut.discharge_gradient_kg_per_mw
        ) != hours:
            raise ValueError("every carbon cut must match the input horizon")
        violation = carbon_violation_var(cut.scenario_index)
        model.addCons(
            cut.intercept_kg
            + sum(
                cut.charge_gradient_kg_per_mw[t] * p_ch[t]
                + cut.discharge_gradient_kg_per_mw[t] * p_dis[t]
                for t in range(hours)
            )
            <= carbon_budget_kg + cut.big_m_kg * violation,
            name=f"carbon_benders_{cut.scenario_index}_{cut_index}",
        )

    scenario_count = len(scenarios)
    chance_denominator = (
        chance_sample_size if chance_sample_size is not None else scenario_count
    )
    model.addCons(
        sum(violation_workload.values()) <= beta_workload * chance_denominator,
        name="saa_workload_chance",
    )
    if not minimize_carbon_violations:
        model.addCons(
            sum(violation_carbon.values()) <= beta_carbon * chance_denominator,
            name="saa_carbon_chance",
        )
    model.addCons(
        sum(violation_grid.values()) <= beta_grid * chance_denominator,
        name="saa_grid_chance",
    )
    model.addCons(
        sum(violation_ramp.values()) <= beta_ramp * chance_denominator,
        name="saa_ramp_chance",
    )
    if minimize_carbon_violations:
        model.setObjective(sum(violation_carbon.values()), "minimize")
    else:
        model.setObjective(primary_objective, "minimize")
    if initial_plan is not None:
        warm_start = model.createPartialSol()
        for t in range(hours):
            model.setSolVal(warm_start, batch[t], initial_plan.batch[t])
            model.setSolVal(warm_start, grid[t], initial_plan.grid[t])
            model.setSolVal(warm_start, p_ch[t], initial_plan.bess_charge[t])
            model.setSolVal(warm_start, p_dis[t], initial_plan.bess_discharge[t])
            model.setSolVal(
                warm_start,
                curtailment[t],
                initial_plan.curtailment[t],
            )
        model.addSol(warm_start)
    minimum_carbon_violation_count: int | None = None
    model.optimize()
    if model.getStatus() != "optimal":
        solver_status = str(model.getStatus())
        return SaaDayAheadResult(
            plan=_infeasible_day_ahead_result(),
            scenario_count=scenario_count,
            workload_violation_rate=0.0,
            carbon_violation_rate=0.0,
            grid_limit_violation_rate=0.0,
            ramp_violation_rate=0.0,
            solver_status=solver_status,
            runtime_seconds=model.getSolvingTime(),
            mip_gap=model.getGap(),
            carbon_cut_count=len(carbon_cuts),
        )
    if minimize_carbon_violations:
        minimum_carbon_violation_count = int(round(model.getObjVal()))
        model.freeTransform()
        model.addCons(
            sum(violation_carbon.values()) == minimum_carbon_violation_count,
            name="fix_minimum_carbon_violations",
        )
        model.setObjective(primary_objective, "minimize")
        model.optimize()
        if model.getStatus() != "optimal":
            solver_status = str(model.getStatus())
            return SaaDayAheadResult(
                plan=_infeasible_day_ahead_result(),
                scenario_count=scenario_count,
                workload_violation_rate=0.0,
                carbon_violation_rate=0.0,
                grid_limit_violation_rate=0.0,
                ramp_violation_rate=0.0,
                solver_status=solver_status,
                runtime_seconds=model.getSolvingTime(),
                mip_gap=model.getGap(),
                carbon_cut_count=len(carbon_cuts),
                carbon_cut_violation_lower_bound=(
                    minimum_carbon_violation_count
                ),
            )

    # 主问题只负责选择日前动作并验证“存在可行场景追索”。主问题最优后，
    # 将每个训练场景拆成独立连续回放，避免为不影响日前目标的词典序整理
    # 反复变换整个 30 天联合 MILP。
    batch_values = [model.getVal(batch[t]) for t in range(hours)]
    grid_values = [model.getVal(grid[t]) for t in range(hours)]
    charge_values = [model.getVal(p_ch[t]) for t in range(hours)]
    discharge_values = [model.getVal(p_dis[t]) for t in range(hours)]
    simultaneous_hours = [
        t
        for t in range(hours)
        if min(charge_values[t], discharge_values[t]) > 1e-6
    ]
    if simultaneous_hours:
        raise RuntimeError(
            "SAA convex-hull solution violates charge/discharge complementarity "
            f"in {len(simultaneous_hours)} hours"
        )
    curtailment_values = [model.getVal(curtailment[t]) for t in range(hours)]
    grid_cost = sum(price * value for price, value in zip(prices, grid_values))
    degradation_cost = bess_degradation_cost(
        charge_values,
        discharge_values,
        bess_degradation_cost_usd_per_mwh_throughput,
    )
    forecast_carbon_kg = _carbon_kg(grid_values, forecast_carbon)
    plan = DayAheadResult(
        baseline_cost=baseline_cost,
        baseline_carbon_kg=baseline_carbon_kg,
        grid_cost=grid_cost,
        bess_degradation_cost=degradation_cost,
        operating_cost=grid_cost + degradation_cost,
        cost_reduction=(
            (baseline_cost - grid_cost - degradation_cost) / baseline_cost
            if baseline_cost
            else 0.0
        ),
        forecast_carbon_kg=forecast_carbon_kg,
        carbon_budget_kg=carbon_budget_kg,
        carbon_budget_slack_kg=carbon_budget_kg - forecast_carbon_kg,
        batch=batch_values,
        grid=grid_values,
        bess_charge=charge_values,
        bess_discharge=discharge_values,
        pv_generation=pv,
        wind_generation=wind,
        curtailment=curtailment_values,
        feasible=True,
    )
    training_replays = [
        replay_joint_scenario_with_batch_recourse(
            inputs,
            plan,
            scenario,
            pv_capacity_mw=pv_capacity_mw,
            wind_capacity_mw=wind_capacity_mw,
            g_max_mw=g_max_mw,
            r_max_mw=r_max_mw,
            p_grid_initial_mw=p_grid_initial_mw,
            solar_reference_mwh=solar_reference_mwh,
            wind_reference_mwh=wind_reference_mwh,
        )
        for scenario in scenarios
    ]
    return SaaDayAheadResult(
        plan=plan,
        scenario_count=scenario_count,
        workload_violation_rate=(
            sum(result.workload_violation for result in training_replays)
            / scenario_count
        ),
        carbon_violation_rate=(
            sum(result.carbon_violation for result in training_replays)
            / scenario_count
        ),
        grid_limit_violation_rate=(
            sum(result.grid_limit_violation for result in training_replays)
            / scenario_count
        ),
        ramp_violation_rate=(
            sum(result.ramp_violation for result in training_replays)
            / scenario_count
        ),
        mean_batch_adjustment_mwh=(
            sum(result.batch_adjustment_mwh for result in training_replays)
            / scenario_count
        ),
        solver_status=str(model.getStatus()),
        runtime_seconds=model.getSolvingTime(),
        mip_gap=model.getGap(),
        carbon_cut_count=len(carbon_cuts),
        carbon_cut_violation_lower_bound=minimum_carbon_violation_count,
    )


def replay_joint_scenario(
    inputs: list[HourlyInput],
    plan: DayAheadResult,
    scenario: ScenarioRealization,
    *,
    pv_capacity_mw: float,
    wind_capacity_mw: float,
    g_max_mw: float,
    r_max_mw: float,
    p_grid_initial_mw: float,
    solar_reference_mwh: float = SOLAR_REFERENCE_MWH,
    wind_reference_mwh: float = WIND_REFERENCE_MWH,
    tolerance: float = 1e-7,
) -> ScenarioReplayResult:
    """固定日前批处理/BESS，在一个联合残差与算力包络场景上回放。"""

    if not plan.feasible:
        raise ValueError("cannot replay an infeasible day-ahead plan")
    hours = len(inputs)
    if hours != len(plan.batch):
        raise ValueError("inputs and plan must have the same horizon")
    scenario_lengths = (
        len(scenario.cumulative_arrived_core_hours),
        len(scenario.cumulative_due_core_hours),
        len(scenario.residual_solar_mwh),
        len(scenario.residual_wind_mwh),
        len(scenario.residual_carbon_lbs_per_kwh),
    )
    if any(length != hours for length in scenario_lengths):
        raise ValueError("scenario must match the input horizon")
    if tolerance < 0.0:
        raise ValueError("tolerance must be non-negative")
    conversions = {item.workload_mwh_per_core_hour for item in inputs}
    if len(conversions) != 1 or next(iter(conversions)) <= 0.0:
        raise ValueError(
            "inputs must carry one positive workload_mwh_per_core_hour conversion"
        )
    workload_conversion = next(iter(conversions))

    pv = _local_resource_profile(
        [
            inputs[t].forecast_erco_solar_generation_mwh
            + scenario.residual_solar_mwh[t]
            for t in range(hours)
        ],
        capacity_mw=pv_capacity_mw,
        reference_mwh=solar_reference_mwh,
        label="scenario_pv",
    )
    wind = _local_resource_profile(
        [
            inputs[t].forecast_erco_wind_generation_mwh
            + scenario.residual_wind_mwh[t]
            for t in range(hours)
        ],
        capacity_mw=wind_capacity_mw,
        reference_mwh=wind_reference_mwh,
        label="scenario_wind",
    )
    p_must = inputs[0].online_mw + inputs[0].base_mw
    residual_load = [
        p_must + plan.batch[t] + plan.bess_charge[t] - plan.bess_discharge[t]
        for t in range(hours)
    ]
    grid = [max(0.0, residual_load[t] - pv[t] - wind[t]) for t in range(hours)]
    curtailment = [
        max(0.0, pv[t] + wind[t] - residual_load[t]) for t in range(hours)
    ]
    carbon = [
        max(
            0.0,
            inputs[t].forecast_consumed_co2_lbs_per_kwh
            + scenario.residual_carbon_lbs_per_kwh[t],
        )
        for t in range(hours)
    ]
    carbon_kg = _carbon_kg(grid, carbon)
    prices = [item.dam_lz_houston_usd_per_mwh for item in inputs]
    grid_cost = sum(price * value for price, value in zip(prices, grid, strict=True))

    cumulative = 0.0
    workload_violation = False
    for t, value in enumerate(plan.batch):
        cumulative += value
        arrived = scenario.cumulative_arrived_core_hours[t] * workload_conversion
        due = scenario.cumulative_due_core_hours[t] * workload_conversion
        if cumulative > arrived + tolerance or cumulative < due - tolerance:
            workload_violation = True
            break
    grid_limit_violation = any(value > g_max_mw + tolerance for value in grid)
    ramp_violation = any(
        abs(value - previous) > r_max_mw + tolerance
        for value, previous in zip(
            grid,
            [p_grid_initial_mw, *grid[:-1]],
            strict=True,
        )
    )
    return ScenarioReplayResult(
        batch=list(plan.batch),
        batch_adjustment_mwh=0.0,
        grid=grid,
        curtailment=curtailment,
        grid_cost=grid_cost,
        operating_cost=grid_cost + plan.bess_degradation_cost,
        carbon_kg=carbon_kg,
        workload_violation=workload_violation,
        carbon_violation=carbon_kg > plan.carbon_budget_kg + tolerance,
        grid_limit_violation=grid_limit_violation,
        ramp_violation=ramp_violation,
    )


def replay_joint_scenario_with_batch_recourse(
    inputs: list[HourlyInput],
    plan: DayAheadResult,
    scenario: ScenarioRealization,
    *,
    pv_capacity_mw: float,
    wind_capacity_mw: float,
    g_max_mw: float,
    r_max_mw: float,
    p_grid_initial_mw: float,
    solar_reference_mwh: float = SOLAR_REFERENCE_MWH,
    wind_reference_mwh: float = WIND_REFERENCE_MWH,
    tolerance: float = 1e-7,
) -> ScenarioReplayResult:
    """固定日前 BESS，在一个场景中求风险优先的有限批处理追索。

    依次最小化碳/并网/爬坡违反数、相对名义计划的 L1 调整量和购电量。
    这是用于标定的离线有限追索，不表示已实现在线因果控制。
    """

    if Model is None:
        raise RuntimeError("pyscipopt is required; run inside scip_env")
    if not plan.feasible:
        raise ValueError("cannot replay an infeasible day-ahead plan")
    hours = len(inputs)
    if hours != len(plan.batch):
        raise ValueError("inputs and plan must have the same horizon")
    scenario_lengths = (
        len(scenario.cumulative_arrived_core_hours),
        len(scenario.cumulative_due_core_hours),
        len(scenario.residual_solar_mwh),
        len(scenario.residual_wind_mwh),
        len(scenario.residual_carbon_lbs_per_kwh),
    )
    if any(length != hours for length in scenario_lengths):
        raise ValueError("scenario must match the input horizon")
    if tolerance < 0.0:
        raise ValueError("tolerance must be non-negative")
    conversions = {item.workload_mwh_per_core_hour for item in inputs}
    if len(conversions) != 1 or next(iter(conversions)) <= 0.0:
        raise ValueError(
            "inputs must carry one positive workload_mwh_per_core_hour conversion"
        )
    workload_conversion = next(iter(conversions))

    pv = _local_resource_profile(
        [
            inputs[t].forecast_erco_solar_generation_mwh
            + scenario.residual_solar_mwh[t]
            for t in range(hours)
        ],
        capacity_mw=pv_capacity_mw,
        reference_mwh=solar_reference_mwh,
        label="scenario_pv",
    )
    wind = _local_resource_profile(
        [
            inputs[t].forecast_erco_wind_generation_mwh
            + scenario.residual_wind_mwh[t]
            for t in range(hours)
        ],
        capacity_mw=wind_capacity_mw,
        reference_mwh=wind_reference_mwh,
        label="scenario_wind",
    )
    fallback_capacity_mw = max(item.batch_window_mwh for item in inputs)
    capacity_mw = [
        item.batch_capacity_mw
        if item.batch_capacity_mw is not None
        else fallback_capacity_mw
        for item in inputs
    ]
    model = Model("scenario_batch_recourse")
    model.hideOutput()
    model.setIntParam("parallel/maxnthreads", 1)
    batch = {
        t: model.addVar(lb=0.0, ub=capacity_mw[t], name=f"batch_{t}")
        for t in range(hours)
    }
    grid_upper_mw = (
        inputs[0].online_mw
        + inputs[0].base_mw
        + max(capacity_mw)
        + max(plan.bess_charge, default=0.0)
    )
    grid = {
        t: model.addVar(lb=0.0, ub=grid_upper_mw, name=f"grid_{t}")
        for t in range(hours)
    }
    curtailment = {
        t: model.addVar(lb=0.0, ub=pv[t] + wind[t], name=f"curtailment_{t}")
        for t in range(hours)
    }
    violation_carbon = model.addVar(vtype="B", name="violate_carbon")
    violation_grid = model.addVar(vtype="B", name="violate_grid")
    violation_ramp = model.addVar(vtype="B", name="violate_ramp")
    deviations: list[object] = []
    cumulative_batch = 0.0
    p_must = inputs[0].online_mw + inputs[0].base_mw
    previous_grid: object = p_grid_initial_mw
    for t in range(hours):
        deviation_positive = model.addVar(lb=0.0, name=f"dev_pos_{t}")
        deviation_negative = model.addVar(lb=0.0, name=f"dev_neg_{t}")
        model.addCons(
            batch[t] - plan.batch[t] == deviation_positive - deviation_negative
        )
        deviations.append(deviation_positive + deviation_negative)
        cumulative_batch += batch[t]
        model.addCons(
            cumulative_batch
            <= scenario.cumulative_arrived_core_hours[t] * workload_conversion
        )
        model.addCons(
            cumulative_batch
            >= scenario.cumulative_due_core_hours[t] * workload_conversion
        )
        model.addCons(
            grid[t]
            == p_must
            + batch[t]
            + plan.bess_charge[t]
            - plan.bess_discharge[t]
            - pv[t]
            - wind[t]
            + curtailment[t]
        )
        model.addCons(
            grid[t] <= g_max_mw + grid_upper_mw * violation_grid
        )
        ramp_big_m = 2.0 * grid_upper_mw + abs(p_grid_initial_mw)
        model.addCons(
            grid[t] - previous_grid
            <= r_max_mw + ramp_big_m * violation_ramp
        )
        model.addCons(
            grid[t] - previous_grid
            >= -r_max_mw - ramp_big_m * violation_ramp
        )
        previous_grid = grid[t]
    model.addCons(sum(batch.values()) == sum(plan.batch))
    carbon = [
        max(
            0.0,
            inputs[t].forecast_consumed_co2_lbs_per_kwh
            + scenario.residual_carbon_lbs_per_kwh[t],
        )
        for t in range(hours)
    ]
    carbon_expr = sum(
        carbon[t] * 1000.0 * LBS_PER_KG * grid[t] for t in range(hours)
    )
    carbon_big_m = sum(
        carbon[t] * 1000.0 * LBS_PER_KG * grid_upper_mw
        for t in range(hours)
    )
    model.addCons(
        carbon_expr
        <= plan.carbon_budget_kg + carbon_big_m * violation_carbon
    )
    total_risk_violations = violation_carbon + violation_grid + violation_ramp
    model.setObjective(total_risk_violations, "minimize")
    model.optimize()
    if model.getStatus() != "optimal":
        raise RuntimeError(
            f"batch recourse risk stage is {model.getStatus()}"
        )
    selected_risk = (
        round(model.getVal(violation_carbon)),
        round(model.getVal(violation_grid)),
        round(model.getVal(violation_ramp)),
    )
    model.freeTransform()
    model.addCons(violation_carbon == selected_risk[0])
    model.addCons(violation_grid == selected_risk[1])
    model.addCons(violation_ramp == selected_risk[2])
    total_deviation = sum(deviations)
    model.setObjective(total_deviation, "minimize")
    model.optimize()
    if model.getStatus() != "optimal":
        raise RuntimeError(
            f"batch recourse replay is {model.getStatus()}; scenario may be physically infeasible"
        )
    deviation_value = model.getObjVal()
    model.freeTransform()
    model.addCons(
        total_deviation
        <= deviation_value + max(1e-6, abs(deviation_value) * 1e-9)
    )
    model.setObjective(sum(grid.values()), "minimize")
    model.optimize()
    if model.getStatus() != "optimal":
        raise RuntimeError(f"batch recourse replay second stage is {model.getStatus()}")

    batch_values = [model.getVal(batch[t]) for t in range(hours)]
    grid_values = [model.getVal(grid[t]) for t in range(hours)]
    curtailment_values = [model.getVal(curtailment[t]) for t in range(hours)]
    carbon_kg = _carbon_kg(grid_values, carbon)
    prices = [item.dam_lz_houston_usd_per_mwh for item in inputs]
    grid_cost = sum(
        price * value for price, value in zip(prices, grid_values, strict=True)
    )
    grid_limit_violation = any(
        value > g_max_mw + tolerance for value in grid_values
    )
    ramp_violation = any(
        abs(value - previous) > r_max_mw + tolerance
        for value, previous in zip(
            grid_values,
            [p_grid_initial_mw, *grid_values[:-1]],
            strict=True,
        )
    )
    return ScenarioReplayResult(
        batch=batch_values,
        batch_adjustment_mwh=sum(
            abs(actual - nominal)
            for actual, nominal in zip(batch_values, plan.batch, strict=True)
        ),
        grid=grid_values,
        curtailment=curtailment_values,
        grid_cost=grid_cost,
        operating_cost=grid_cost + plan.bess_degradation_cost,
        carbon_kg=carbon_kg,
        workload_violation=False,
        carbon_violation=carbon_kg > plan.carbon_budget_kg + tolerance,
        grid_limit_violation=grid_limit_violation,
        ramp_violation=ramp_violation,
    )


def solve_carbon_recourse_subproblem(
    inputs: list[HourlyInput],
    plan: DayAheadResult,
    scenario: ScenarioRealization,
    *,
    scenario_index: int,
    pv_capacity_mw: float,
    wind_capacity_mw: float,
    g_max_mw: float,
    r_max_mw: float,
    p_grid_initial_mw: float,
    bess_power_mw: float,
    solar_reference_mwh: float = SOLAR_REFERENCE_MWH,
    wind_reference_mwh: float = WIND_REFERENCE_MWH,
) -> CarbonSubproblemResult:
    """求固定 BESS 场景最小碳排放，并返回关于日前充放电的对偶割。"""

    if linprog is None:
        raise RuntimeError("scipy is required for carbon recourse dual cuts")
    if not plan.feasible or len(plan.batch) != len(inputs):
        raise ValueError("plan must be feasible and match the input horizon")
    hours = len(inputs)
    conversion_values = {item.workload_mwh_per_core_hour for item in inputs}
    if len(conversion_values) != 1 or next(iter(conversion_values)) <= 0.0:
        raise ValueError("inputs must carry one positive workload conversion")
    workload_conversion = next(iter(conversion_values))
    pv = _local_resource_profile(
        [
            inputs[t].forecast_erco_solar_generation_mwh
            + scenario.residual_solar_mwh[t]
            for t in range(hours)
        ],
        capacity_mw=pv_capacity_mw,
        reference_mwh=solar_reference_mwh,
        label="scenario_pv",
    )
    wind = _local_resource_profile(
        [
            inputs[t].forecast_erco_wind_generation_mwh
            + scenario.residual_wind_mwh[t]
            for t in range(hours)
        ],
        capacity_mw=wind_capacity_mw,
        reference_mwh=wind_reference_mwh,
        label="scenario_wind",
    )
    carbon = [
        max(
            0.0,
            inputs[t].forecast_consumed_co2_lbs_per_kwh
            + scenario.residual_carbon_lbs_per_kwh[t],
        )
        for t in range(hours)
    ]
    fallback_capacity_mw = max(item.batch_window_mwh for item in inputs)
    capacity_mw = [
        item.batch_capacity_mw
        if item.batch_capacity_mw is not None
        else fallback_capacity_mw
        for item in inputs
    ]
    # 变量块依次为 batch、grid、curtailment；显式矩阵便于稳定读取 LP 对偶值。
    variable_count = 3 * hours
    batch_index = lambda t: t
    grid_index = lambda t: hours + t
    curtailment_index = lambda t: 2 * hours + t
    objective = [0.0] * variable_count
    bounds: list[tuple[float, float]] = []
    bounds.extend((0.0, capacity_mw[t]) for t in range(hours))
    bounds.extend((0.0, g_max_mw) for _ in range(hours))
    bounds.extend((0.0, pv[t] + wind[t]) for t in range(hours))
    for t in range(hours):
        objective[grid_index(t)] = carbon[t] * 1000.0 * LBS_PER_KG

    a_ub: list[list[float]] = []
    b_ub: list[float] = []
    for t in range(hours):
        arrived_row = [0.0] * variable_count
        due_row = [0.0] * variable_count
        for tau in range(t + 1):
            arrived_row[batch_index(tau)] = 1.0
            due_row[batch_index(tau)] = -1.0
        a_ub.append(arrived_row)
        b_ub.append(
            scenario.cumulative_arrived_core_hours[t] * workload_conversion
        )
        a_ub.append(due_row)
        b_ub.append(-scenario.cumulative_due_core_hours[t] * workload_conversion)

    for t in range(hours):
        ramp_up = [0.0] * variable_count
        ramp_down = [0.0] * variable_count
        ramp_up[grid_index(t)] = 1.0
        ramp_down[grid_index(t)] = -1.0
        if t == 0:
            ramp_up_rhs = r_max_mw + p_grid_initial_mw
            ramp_down_rhs = r_max_mw - p_grid_initial_mw
        else:
            ramp_up[grid_index(t - 1)] = -1.0
            ramp_down[grid_index(t - 1)] = 1.0
            ramp_up_rhs = r_max_mw
            ramp_down_rhs = r_max_mw
        a_ub.extend((ramp_up, ramp_down))
        b_ub.extend((ramp_up_rhs, ramp_down_rhs))

    p_must = inputs[0].online_mw + inputs[0].base_mw
    a_eq: list[list[float]] = []
    b_eq: list[float] = []
    for t in range(hours):
        balance_row = [0.0] * variable_count
        balance_row[grid_index(t)] = 1.0
        balance_row[batch_index(t)] = -1.0
        balance_row[curtailment_index(t)] = -1.0
        a_eq.append(balance_row)
        b_eq.append(
            p_must
            + plan.bess_charge[t]
            - plan.bess_discharge[t]
            - pv[t]
            - wind[t]
        )
    batch_total_row = [0.0] * variable_count
    for t in range(hours):
        batch_total_row[batch_index(t)] = 1.0
    a_eq.append(batch_total_row)
    b_eq.append(sum(plan.batch))

    result = linprog(
        objective,
        A_ub=a_ub,
        b_ub=b_ub,
        A_eq=a_eq,
        b_eq=b_eq,
        bounds=bounds,
        method="highs",
    )
    status = "optimal" if result.success else f"linprog_status_{result.status}"
    if not result.success:
        return CarbonSubproblemResult(
            feasible=False,
            minimum_carbon_kg=math.inf,
            cut=None,
            solver_status=f"{status}: {result.message}",
        )
    minimum_carbon_kg = float(result.fun)
    balance_duals = [float(value) for value in result.eqlin.marginals[:hours]]
    charge_gradient = tuple(balance_duals)
    discharge_gradient = tuple(-value for value in balance_duals)
    intercept = minimum_carbon_kg - sum(
        charge_gradient[t] * plan.bess_charge[t]
        + discharge_gradient[t] * plan.bess_discharge[t]
        for t in range(hours)
    )
    maximum_cut_value_kg = intercept + bess_power_mw * sum(
        max(0.0, charge_gradient[t], discharge_gradient[t])
        for t in range(hours)
    )
    return CarbonSubproblemResult(
        feasible=True,
        minimum_carbon_kg=minimum_carbon_kg,
        cut=CarbonBendersCut(
            scenario_index=scenario_index,
            intercept_kg=intercept,
            charge_gradient_kg_per_mw=charge_gradient,
            discharge_gradient_kg_per_mw=discharge_gradient,
            big_m_kg=max(0.0, maximum_cut_value_kg - plan.carbon_budget_kg),
        ),
        solver_status=status,
    )


def solve_decomposed_saa_wind_solar_storage(
    inputs: list[HourlyInput],
    scenarios: Sequence[ScenarioRealization],
    *,
    g_max_mw: float,
    r_max_mw: float,
    p_grid_initial_mw: float,
    bess_power_mw: float,
    bess_energy_mwh: float,
    pv_capacity_mw: float,
    wind_capacity_mw: float,
    carbon_budget_reduction: float = DEFAULT_CARBON_BUDGET_REDUCTION,
    beta_workload: float = 0.10,
    beta_carbon: float = 0.10,
    beta_grid: float = 0.10,
    beta_ramp: float = 0.10,
    solar_reference_mwh: float = SOLAR_REFERENCE_MWH,
    wind_reference_mwh: float = WIND_REFERENCE_MWH,
    bess_efficiency: float = 0.90,
    soc_min: float = 0.10,
    soc_max: float = 0.90,
    soc_initial: float = 0.50,
    bess_degradation_cost_usd_per_mwh_throughput: float = BESS_DEGRADATION_COST_USD_PER_MWH_THROUGHPUT,
    time_limit_seconds: float | None = None,
    max_iterations: int = 8,
    display_progress: bool = False,
    replay_workers: int = 1,
) -> SaaDayAheadResult:
    """用活动场景约束生成求解 SAA，并用全部场景 LP 回放验收。

    主问题从 ``max_j floor(beta_j N)+1`` 条场景开始。每轮固定日前计划，
    在全部训练场景上执行有限批处理追索；每个超标风险通道至多加入一条
    尚未激活的违反场景。只有全部通道的实际回放违反数均达标才返回可行。
    """

    if not scenarios:
        raise ValueError("scenarios must not be empty")
    if max_iterations <= 0:
        raise ValueError("max_iterations must be positive")
    if replay_workers <= 0:
        raise ValueError("replay_workers must be positive")
    total_scenarios = len(scenarios)
    allowed = {
        "workload": math.floor(beta_workload * total_scenarios + 1e-9),
        "carbon": math.floor(beta_carbon * total_scenarios + 1e-9),
        "grid": math.floor(beta_grid * total_scenarios + 1e-9),
        "ramp": math.floor(beta_ramp * total_scenarios + 1e-9),
    }
    initial_count = min(total_scenarios, max(allowed.values()) + 1)
    active_indices = set(range(max(1, initial_count)))
    started = time.perf_counter()
    last_result: SaaDayAheadResult | None = None
    previous_plan: DayAheadResult | None = None
    last_rates = {name: 1.0 for name in allowed}
    carbon_cuts: list[CarbonBendersCut] = []
    carbon_cut_violation_lower_bound: int | None = None

    for iteration in range(1, max_iterations + 1):
        ordered_active = sorted(active_indices)
        active_scenarios = [scenarios[index] for index in ordered_active]
        if display_progress:
            print(
                "decomposition_start:",
                f"iteration={iteration}",
                f"active={len(active_indices)}",
                flush=True,
            )
        master_result = solve_saa_wind_solar_storage(
            inputs,
            active_scenarios,
            g_max_mw=g_max_mw,
            r_max_mw=r_max_mw,
            p_grid_initial_mw=p_grid_initial_mw,
            bess_power_mw=bess_power_mw,
            bess_energy_mwh=bess_energy_mwh,
            pv_capacity_mw=pv_capacity_mw,
            wind_capacity_mw=wind_capacity_mw,
            carbon_budget_reduction=carbon_budget_reduction,
            beta_workload=beta_workload,
            beta_carbon=beta_carbon,
            beta_grid=beta_grid,
            beta_ramp=beta_ramp,
            solar_reference_mwh=solar_reference_mwh,
            wind_reference_mwh=wind_reference_mwh,
            bess_efficiency=bess_efficiency,
            soc_min=soc_min,
            soc_max=soc_max,
            soc_initial=soc_initial,
            bess_degradation_cost_usd_per_mwh_throughput=(
                bess_degradation_cost_usd_per_mwh_throughput
            ),
            time_limit_seconds=time_limit_seconds,
            chance_sample_size=total_scenarios,
            initial_plan=previous_plan,
            scenario_indices=ordered_active,
            carbon_cuts=carbon_cuts,
        )
        last_result = master_result
        if display_progress:
            print(
                "decomposition_master:",
                f"iteration={iteration}",
                f"status={master_result.solver_status}",
                f"runtime={master_result.runtime_seconds:.3f}s",
                flush=True,
            )
        if not master_result.feasible:
            return replace(
                master_result,
                scenario_count=total_scenarios,
                runtime_seconds=time.perf_counter() - started,
                decomposition_iterations=iteration,
                active_scenario_count=len(active_indices),
                carbon_cut_count=len(carbon_cuts),
                carbon_cut_violation_lower_bound=carbon_cut_violation_lower_bound,
            )
        previous_plan = master_result.plan

        replay_one = partial(
            replay_joint_scenario_with_batch_recourse,
            inputs,
            master_result.plan,
            pv_capacity_mw=pv_capacity_mw,
            wind_capacity_mw=wind_capacity_mw,
            g_max_mw=g_max_mw,
            r_max_mw=r_max_mw,
            p_grid_initial_mw=p_grid_initial_mw,
            solar_reference_mwh=solar_reference_mwh,
            wind_reference_mwh=wind_reference_mwh,
        )
        if replay_workers == 1:
            replays = [replay_one(scenario) for scenario in scenarios]
        else:
            with ProcessPoolExecutor(max_workers=replay_workers) as executor:
                replays = list(executor.map(replay_one, scenarios))
        violations = {
            "workload": [
                index for index, result in enumerate(replays)
                if result.workload_violation
            ],
            "carbon": [
                index for index, result in enumerate(replays)
                if result.carbon_violation
            ],
            "grid": [
                index for index, result in enumerate(replays)
                if result.grid_limit_violation
            ],
            "ramp": [
                index for index, result in enumerate(replays)
                if result.ramp_violation
            ],
        }
        last_rates = {
            name: len(indices) / total_scenarios
            for name, indices in violations.items()
        }
        if display_progress:
            print(
                "decomposition_replay:",
                f"iteration={iteration}",
                *(f"{name}={len(violations[name])}/{total_scenarios}" for name in allowed),
                f"workers={replay_workers}",
                flush=True,
            )
        if all(len(violations[name]) <= allowed[name] for name in allowed):
            return replace(
                master_result,
                scenario_count=total_scenarios,
                workload_violation_rate=last_rates["workload"],
                carbon_violation_rate=last_rates["carbon"],
                grid_limit_violation_rate=last_rates["grid"],
                ramp_violation_rate=last_rates["ramp"],
                mean_batch_adjustment_mwh=(
                    sum(result.batch_adjustment_mwh for result in replays)
                    / total_scenarios
                ),
                solver_status="optimal_decomposed",
                runtime_seconds=time.perf_counter() - started,
                decomposition_iterations=iteration,
                active_scenario_count=len(active_indices),
                carbon_cut_count=len(carbon_cuts),
                carbon_cut_violation_lower_bound=carbon_cut_violation_lower_bound,
            )

        cuts_added = 0
        if len(violations["carbon"]) > allowed["carbon"]:
            for scenario_index in violations["carbon"]:
                subproblem = solve_carbon_recourse_subproblem(
                    inputs,
                    master_result.plan,
                    scenarios[scenario_index],
                    scenario_index=scenario_index,
                    pv_capacity_mw=pv_capacity_mw,
                    wind_capacity_mw=wind_capacity_mw,
                    g_max_mw=g_max_mw,
                    r_max_mw=r_max_mw,
                    p_grid_initial_mw=p_grid_initial_mw,
                    bess_power_mw=bess_power_mw,
                    solar_reference_mwh=solar_reference_mwh,
                    wind_reference_mwh=wind_reference_mwh,
                )
                if subproblem.cut is not None:
                    carbon_cuts.append(subproblem.cut)
                    cuts_added += 1
            if display_progress:
                details = []
                if cuts_added:
                    new_cuts = carbon_cuts[-cuts_added:]
                    details = [
                        f"big_m_min={min(cut.big_m_kg for cut in new_cuts):.3f}",
                        f"big_m_max={max(cut.big_m_kg for cut in new_cuts):.3f}",
                    ]
                print(
                    "decomposition_carbon_cuts:",
                    f"iteration={iteration}",
                    f"added={cuts_added}",
                    f"total={len(carbon_cuts)}",
                    *details,
                    flush=True,
                )

        additions: set[int] = set()
        for name in ("workload", "grid", "ramp"):
            if len(violations[name]) <= allowed[name]:
                continue
            inactive = [
                index for index in violations[name] if index not in active_indices
            ]
            if inactive:
                additions.add(inactive[0])
        if not additions and cuts_added == 0:
            return SaaDayAheadResult(
                plan=_infeasible_day_ahead_result(),
                scenario_count=total_scenarios,
                workload_violation_rate=last_rates["workload"],
                carbon_violation_rate=last_rates["carbon"],
                grid_limit_violation_rate=last_rates["grid"],
                ramp_violation_rate=last_rates["ramp"],
                solver_status="decomposition_policy_mismatch",
                runtime_seconds=time.perf_counter() - started,
                decomposition_iterations=iteration,
                active_scenario_count=len(active_indices),
                carbon_cut_count=len(carbon_cuts),
                carbon_cut_violation_lower_bound=carbon_cut_violation_lower_bound,
            )
        active_indices.update(additions)
        if display_progress:
            print(
                "decomposition_add:",
                f"iteration={iteration}",
                f"added={','.join(str(index) for index in sorted(additions)) or 'none'}",
                flush=True,
            )
        if iteration == max_iterations:
            continue
        if additions:
            restoration_indices = sorted(additions)
            restoration_scenarios = [
                scenarios[index] for index in restoration_indices
            ]
            restoration = solve_saa_wind_solar_storage(
                inputs,
                restoration_scenarios,
                g_max_mw=g_max_mw,
                r_max_mw=r_max_mw,
                p_grid_initial_mw=p_grid_initial_mw,
                bess_power_mw=bess_power_mw,
                bess_energy_mwh=bess_energy_mwh,
                pv_capacity_mw=pv_capacity_mw,
                wind_capacity_mw=wind_capacity_mw,
                carbon_budget_reduction=carbon_budget_reduction,
                beta_workload=0.0,
                beta_carbon=0.0,
                beta_grid=0.0,
                beta_ramp=0.0,
                solar_reference_mwh=solar_reference_mwh,
                wind_reference_mwh=wind_reference_mwh,
                bess_efficiency=bess_efficiency,
                soc_min=soc_min,
                soc_max=soc_max,
                soc_initial=soc_initial,
                bess_degradation_cost_usd_per_mwh_throughput=(
                    bess_degradation_cost_usd_per_mwh_throughput
                ),
                time_limit_seconds=time_limit_seconds,
                initial_plan=previous_plan,
                scenario_indices=restoration_indices,
            )
            if restoration.feasible:
                previous_plan = restoration.plan
            if display_progress:
                print(
                    "decomposition_restore:",
                    f"iteration={iteration}",
                    f"status={restoration.solver_status}",
                    f"runtime={restoration.runtime_seconds:.3f}s",
                    flush=True,
                )

        if cuts_added:
            restoration_index = ordered_active[0]
            cut_restoration = solve_saa_wind_solar_storage(
                inputs,
                [scenarios[restoration_index]],
                g_max_mw=g_max_mw,
                r_max_mw=r_max_mw,
                p_grid_initial_mw=p_grid_initial_mw,
                bess_power_mw=bess_power_mw,
                bess_energy_mwh=bess_energy_mwh,
                pv_capacity_mw=pv_capacity_mw,
                wind_capacity_mw=wind_capacity_mw,
                carbon_budget_reduction=carbon_budget_reduction,
                beta_workload=beta_workload,
                beta_carbon=beta_carbon,
                beta_grid=beta_grid,
                beta_ramp=beta_ramp,
                solar_reference_mwh=solar_reference_mwh,
                wind_reference_mwh=wind_reference_mwh,
                bess_efficiency=bess_efficiency,
                soc_min=soc_min,
                soc_max=soc_max,
                soc_initial=soc_initial,
                bess_degradation_cost_usd_per_mwh_throughput=(
                    bess_degradation_cost_usd_per_mwh_throughput
                ),
                time_limit_seconds=time_limit_seconds,
                chance_sample_size=total_scenarios,
                initial_plan=previous_plan,
                scenario_indices=[restoration_index],
                carbon_cuts=carbon_cuts,
            )
            if cut_restoration.feasible:
                previous_plan = cut_restoration.plan
            if display_progress:
                print(
                    "decomposition_cut_restore:",
                    f"iteration={iteration}",
                    f"status={cut_restoration.solver_status}",
                    f"runtime={cut_restoration.runtime_seconds:.3f}s",
                    flush=True,
                )
            if not cut_restoration.feasible:
                cut_diagnostic = solve_saa_wind_solar_storage(
                    inputs,
                    [scenarios[restoration_index]],
                    g_max_mw=g_max_mw,
                    r_max_mw=r_max_mw,
                    p_grid_initial_mw=p_grid_initial_mw,
                    bess_power_mw=bess_power_mw,
                    bess_energy_mwh=bess_energy_mwh,
                    pv_capacity_mw=pv_capacity_mw,
                    wind_capacity_mw=wind_capacity_mw,
                    carbon_budget_reduction=carbon_budget_reduction,
                    beta_workload=beta_workload,
                    beta_carbon=1.0,
                    beta_grid=beta_grid,
                    beta_ramp=beta_ramp,
                    solar_reference_mwh=solar_reference_mwh,
                    wind_reference_mwh=wind_reference_mwh,
                    bess_efficiency=bess_efficiency,
                    soc_min=soc_min,
                    soc_max=soc_max,
                    soc_initial=soc_initial,
                    bess_degradation_cost_usd_per_mwh_throughput=(
                        bess_degradation_cost_usd_per_mwh_throughput
                    ),
                    time_limit_seconds=time_limit_seconds,
                    chance_sample_size=total_scenarios,
                    initial_plan=previous_plan,
                    scenario_indices=[restoration_index],
                    carbon_cuts=carbon_cuts,
                    minimize_carbon_violations=True,
                )
                carbon_cut_violation_lower_bound = (
                    cut_diagnostic.carbon_cut_violation_lower_bound
                )
                if display_progress:
                    print(
                        "decomposition_cut_diagnostic:",
                        f"iteration={iteration}",
                        f"status={cut_diagnostic.solver_status}",
                        "minimum_violations="
                        f"{carbon_cut_violation_lower_bound}",
                        f"runtime={cut_diagnostic.runtime_seconds:.3f}s",
                        flush=True,
                    )
                if (
                    carbon_cut_violation_lower_bound is not None
                    and carbon_cut_violation_lower_bound > allowed["carbon"]
                ):
                    return SaaDayAheadResult(
                        plan=_infeasible_day_ahead_result(),
                        scenario_count=total_scenarios,
                        workload_violation_rate=last_rates["workload"],
                        carbon_violation_rate=last_rates["carbon"],
                        grid_limit_violation_rate=last_rates["grid"],
                        ramp_violation_rate=last_rates["ramp"],
                        solver_status=(
                            "carbon_chance_infeasible_by_cut_lower_bound"
                        ),
                        runtime_seconds=time.perf_counter() - started,
                        mip_gap=0.0,
                        decomposition_iterations=iteration,
                        active_scenario_count=len(active_indices),
                        carbon_cut_count=len(carbon_cuts),
                        carbon_cut_violation_lower_bound=(
                            carbon_cut_violation_lower_bound
                        ),
                    )

    assert last_result is not None
    return SaaDayAheadResult(
        plan=_infeasible_day_ahead_result(),
        scenario_count=total_scenarios,
        workload_violation_rate=last_rates["workload"],
        carbon_violation_rate=last_rates["carbon"],
        grid_limit_violation_rate=last_rates["grid"],
        ramp_violation_rate=last_rates["ramp"],
        solver_status="decomposition_iteration_limit",
        runtime_seconds=time.perf_counter() - started,
        mip_gap=last_result.mip_gap,
        decomposition_iterations=max_iterations,
        active_scenario_count=len(active_indices),
        carbon_cut_count=len(carbon_cuts),
        carbon_cut_violation_lower_bound=carbon_cut_violation_lower_bound,
    )


def replay_actual_wind_solar(
    inputs: list[HourlyInput],
    plan: DayAheadResult,
    *,
    pv_capacity_mw: float,
    wind_capacity_mw: float,
    solar_reference_mwh: float = SOLAR_REFERENCE_MWH,
    wind_reference_mwh: float = WIND_REFERENCE_MWH,
    g_max_mw: float | None = None,
    r_max_mw: float | None = None,
    p_grid_initial_mw: float | None = None,
) -> ActualReplayResult:
    """固定日前批处理/BESS动作，用实际风光和碳强度进行事后回放。"""

    if not plan.feasible:
        raise ValueError("cannot replay an infeasible day-ahead plan")
    if len(inputs) != len(plan.grid):
        raise ValueError("inputs and plan must have the same horizon")
    pv, wind = _resource_profiles(
        inputs,
        pv_capacity_mw=pv_capacity_mw,
        wind_capacity_mw=wind_capacity_mw,
        solar_reference_mwh=solar_reference_mwh,
        wind_reference_mwh=wind_reference_mwh,
        actual=True,
    )
    p_must = inputs[0].online_mw + inputs[0].base_mw
    residual_load = [
        p_must + plan.batch[t] + plan.bess_charge[t] - plan.bess_discharge[t]
        for t in range(len(inputs))
    ]
    actual_grid = [
        max(0.0, residual_load[t] - pv[t] - wind[t])
        for t in range(len(inputs))
    ]
    actual_curtailment = [
        max(0.0, pv[t] + wind[t] - residual_load[t])
        for t in range(len(inputs))
    ]
    actual_carbon = [item.actual_consumed_co2_lbs_per_kwh for item in inputs]
    actual_carbon_kg = _carbon_kg(actual_grid, actual_carbon)
    prices = [item.dam_lz_houston_usd_per_mwh for item in inputs]
    actual_grid_cost = sum(
        price * value for price, value in zip(prices, actual_grid)
    )
    previous = p_grid_initial_mw
    grid_limit_violations = 0
    ramp_violations = 0
    for value in actual_grid:
        if g_max_mw is not None and value > g_max_mw + 1e-7:
            grid_limit_violations += 1
        if previous is not None and r_max_mw is not None:
            if abs(value - previous) > r_max_mw + 1e-7:
                ramp_violations += 1
        previous = value
    return ActualReplayResult(
        grid=actual_grid,
        curtailment=actual_curtailment,
        pv_generation=pv,
        wind_generation=wind,
        grid_cost=actual_grid_cost,
        operating_cost=actual_grid_cost + plan.bess_degradation_cost,
        carbon_kg=actual_carbon_kg,
        carbon_budget_violation_kg=max(0.0, actual_carbon_kg - plan.carbon_budget_kg),
        grid_limit_violation_hours=grid_limit_violations,
        ramp_violation_hours=ramp_violations,
    )


def _peak_load(inputs: list[HourlyInput]) -> float:
    """基线峰值负荷 = 固定负荷 + 批处理基线功率的最大值。"""

    p_must = inputs[0].online_mw + inputs[0].base_mw
    return max(p_must + item.batch_baseline_mwh for item in inputs)
