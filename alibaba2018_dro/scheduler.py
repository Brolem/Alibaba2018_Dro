"""风光储—柔性算力—碳预算的日前调度与实际回放。"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .config import (
    BESS_DEGRADATION_COST_USD_PER_MWH_THROUGHPUT,
    DEFAULT_CARBON_BUDGET_REDUCTION,
    SOLAR_REFERENCE_MWH,
    WIND_REFERENCE_MWH,
)
from .inputs import HourlyInput

try:  # PySCIPOpt 只在 scip_env 里；其它环境仍可导入数据工具。
    from pyscipopt import Model
except ImportError:  # pragma: no cover
    Model = None


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
    bess_mode = {
        t: model.addVar(vtype="B", name=f"bess_charge_mode_{t}")
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
        model.addCons(p_ch[t] <= bess_power_mw * bess_mode[t])
        model.addCons(p_dis[t] <= bess_power_mw * (1.0 - bess_mode[t]))
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
