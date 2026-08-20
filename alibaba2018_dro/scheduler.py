"""Day-ahead flexible-load scheduler with grid/ramp limits and BESS (PySCIPOpt)."""

from __future__ import annotations

import math
from dataclasses import dataclass

from .inputs import HourlyInput

try:  # PySCIPOpt lives in scip_env; keep the module importable elsewhere.
    from pyscipopt import Model
except ImportError:  # pragma: no cover
    Model = None


@dataclass(frozen=True)
class BatchShiftResult:
    baseline_cost: float
    optimal_cost: float
    cost_reduction: float
    batch: list[float]
    grid: list[float]
    bess_charge: list[float]
    bess_discharge: list[float]
    feasible: bool = True


def _infeasible_result() -> BatchShiftResult:
    return BatchShiftResult(
        baseline_cost=0.0,
        optimal_cost=0.0,
        cost_reduction=0.0,
        batch=[],
        grid=[],
        bess_charge=[],
        bess_discharge=[],
        feasible=False,
    )


def solve_batch_shift(
    inputs: list[HourlyInput],
    *,
    g_max_mw: float | None = None,
    r_max_mw: float | None = None,
    p_grid_initial_mw: float | None = None,
    bess_power_mw: float | None = None,
    bess_energy_mwh: float | None = None,
    bess_efficiency: float = 0.90,
    soc_min: float = 0.10,
    soc_max: float = 0.90,
    soc_initial: float = 0.50,
    pv_capacity_mw: float | None = None,
) -> BatchShiftResult:
    """Shift deferrable batch energy to cut cost, with grid/ramp limits and BESS."""

    if Model is None:
        raise RuntimeError("pyscipopt is required; run inside scip_env")
    if (bess_power_mw is None) != (bess_energy_mwh is None):
        raise ValueError("bess_power_mw and bess_energy_mwh must be given together")

    model = Model("batch_shift")
    model.hideOutput()

    hours = len(inputs)
    p_must = inputs[0].online_mw + inputs[0].base_mw

    batch = {
        item.hour: model.addVar(
            lb=0.0,
            ub=item.batch_window_mwh,
            name=f"batch_{item.hour}",
        )
        for item in inputs
    }

    p_grid = {item.hour: p_must + batch[item.hour] for item in inputs}
    p_ch: dict[int, object] = {}
    p_dis: dict[int, object] = {}

    if bess_power_mw is not None:
        eta = math.sqrt(bess_efficiency)
        p_ch = {
            t: model.addVar(lb=0.0, ub=bess_power_mw, name=f"pch_{t}")
            for t in range(hours)
        }
        p_dis = {
            t: model.addVar(lb=0.0, ub=bess_power_mw, name=f"pdis_{t}")
            for t in range(hours)
        }
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
            model.addCons(
                energy[t + 1] == energy[t] + eta * p_ch[t] - p_dis[t] / eta
            )
        model.addCons(energy[hours] == energy[0])
        for t in range(hours):
            p_grid[t] = p_grid[t] + p_ch[t] - p_dis[t]

    if pv_capacity_mw is not None:
        pv = _pv_profile(inputs, pv_capacity_mw)
        for item in inputs:
            p_grid[item.hour] = p_grid[item.hour] - pv[item.hour]

    total_energy = sum(item.batch_baseline_mwh for item in inputs)
    model.addCons(
        sum(batch[item.hour] for item in inputs) == total_energy,
        name="batch_energy_conservation",
    )

    if g_max_mw is not None:
        for item in inputs:
            model.addCons(p_grid[item.hour] <= g_max_mw, name=f"grid_limit_{item.hour}")
    for item in inputs:
        model.addCons(p_grid[item.hour] >= 0.0, name=f"grid_nonneg_{item.hour}")

    if r_max_mw is not None:
        previous = p_grid_initial_mw
        for item in inputs:
            current = p_grid[item.hour]
            if previous is not None:
                model.addCons(current - previous <= r_max_mw, name=f"ramp_up_{item.hour}")
                model.addCons(current - previous >= -r_max_mw, name=f"ramp_down_{item.hour}")
            previous = current

    model.setObjective(
        sum(item.dam_lz_houston_usd_per_mwh * p_grid[item.hour] for item in inputs),
        "minimize",
    )
    model.optimize()

    if model.getStatus() != "optimal":
        return _infeasible_result()

    batch_values = [model.getVal(batch[item.hour]) for item in inputs]
    grid_values = [model.getVal(p_grid[item.hour]) for item in inputs]
    charge_values = [model.getVal(p_ch[t]) if p_ch else 0.0 for t in range(hours)]
    discharge_values = [model.getVal(p_dis[t]) if p_dis else 0.0 for t in range(hours)]
    optimal_cost = sum(
        item.dam_lz_houston_usd_per_mwh * grid_values[index]
        for index, item in enumerate(inputs)
    )
    baseline_cost = sum(
        item.dam_lz_houston_usd_per_mwh * (p_must + item.batch_baseline_mwh)
        for item in inputs
    )

    return BatchShiftResult(
        baseline_cost=baseline_cost,
        optimal_cost=optimal_cost,
        cost_reduction=(
            (baseline_cost - optimal_cost) / baseline_cost if baseline_cost else 0.0
        ),
        batch=batch_values,
        grid=grid_values,
        bess_charge=charge_values,
        bess_discharge=discharge_values,
        feasible=True,
    )


def _peak_load(inputs: list[HourlyInput]) -> float:
    p_must = inputs[0].online_mw + inputs[0].base_mw
    return max(p_must + item.batch_baseline_mwh for item in inputs)


def _pv_profile(inputs: list[HourlyInput], pv_capacity_mw: float) -> list[float]:
    """Scale the ERCO system-solar forecast shape to a local PV capacity."""

    solar = [item.forecast_erco_solar_generation_mwh for item in inputs]
    peak = max(solar)
    if peak <= 0:
        return [0.0] * len(inputs)
    return [pv_capacity_mw * value / peak for value in solar]


def sweep_grid_limit(
    inputs: list[HourlyInput],
    *,
    g_max_fractions: list[float],
    r_max_fraction: float | None = None,
) -> list[tuple[float, bool, float]]:
    """Sweep G_max as a fraction of the baseline peak load."""

    p_peak = _peak_load(inputs)
    p_must = inputs[0].online_mw + inputs[0].base_mw
    r_max_mw = r_max_fraction * p_peak if r_max_fraction is not None else None
    p_grid_initial = p_must + inputs[0].batch_baseline_mwh

    rows: list[tuple[float, bool, float]] = []
    for fraction in g_max_fractions:
        result = solve_batch_shift(
            inputs,
            g_max_mw=fraction * p_peak,
            r_max_mw=r_max_mw,
            p_grid_initial_mw=p_grid_initial,
        )
        rows.append((fraction, result.feasible, result.cost_reduction))
    return rows


def sweep_ramp_limit(
    inputs: list[HourlyInput],
    *,
    g_max_fraction: float,
    r_max_fractions: list[float],
) -> list[tuple[float, bool, float]]:
    """Sweep R_max as a fraction of the baseline peak load, fixing G_max."""

    p_peak = _peak_load(inputs)
    p_must = inputs[0].online_mw + inputs[0].base_mw
    g_max_mw = g_max_fraction * p_peak
    p_grid_initial = p_must + inputs[0].batch_baseline_mwh

    rows: list[tuple[float, bool, float]] = []
    for fraction in r_max_fractions:
        result = solve_batch_shift(
            inputs,
            g_max_mw=g_max_mw,
            r_max_mw=fraction * p_peak,
            p_grid_initial_mw=p_grid_initial,
        )
        rows.append((fraction, result.feasible, result.cost_reduction))
    return rows


def sweep_bess_power(
    inputs: list[HourlyInput],
    *,
    g_max_fraction: float,
    r_max_fraction: float,
    power_fractions: list[float],
    energy_hours: float = 2.0,
    bess_efficiency: float = 0.90,
) -> list[tuple[float, bool, float, float]]:
    """Sweep BESS power (energy fixed in hours of power), reporting cost reduction."""

    p_peak = _peak_load(inputs)
    p_must = inputs[0].online_mw + inputs[0].base_mw
    g_max_mw = g_max_fraction * p_peak
    r_max_mw = r_max_fraction * p_peak
    p_grid_initial = p_must + inputs[0].batch_baseline_mwh

    rows: list[tuple[float, bool, float, float]] = []
    for fraction in power_fractions:
        power = fraction * p_peak
        result = solve_batch_shift(
            inputs,
            g_max_mw=g_max_mw,
            r_max_mw=r_max_mw,
            p_grid_initial_mw=p_grid_initial,
            bess_power_mw=power,
            bess_energy_mwh=energy_hours * power,
            bess_efficiency=bess_efficiency,
        )
        rows.append((fraction, result.feasible, result.cost_reduction, power))
    return rows


def sweep_pv_capacity(
    inputs: list[HourlyInput],
    *,
    g_max_fraction: float,
    r_max_fraction: float,
    pv_fractions: list[float],
    bess_power_fraction: float = 0.0,
    bess_energy_hours: float = 2.0,
) -> list[tuple[float, bool, float, float]]:
    """Sweep local PV capacity (fraction of must-serve load), reporting reduction."""

    p_peak = _peak_load(inputs)
    p_must = inputs[0].online_mw + inputs[0].base_mw
    g_max_mw = g_max_fraction * p_peak
    r_max_mw = r_max_fraction * p_peak
    p_grid_initial = p_must + inputs[0].batch_baseline_mwh

    rows: list[tuple[float, bool, float, float]] = []
    for fraction in pv_fractions:
        capacity = fraction * p_must
        result = solve_batch_shift(
            inputs,
            g_max_mw=g_max_mw,
            r_max_mw=r_max_mw,
            p_grid_initial_mw=p_grid_initial,
            bess_power_mw=bess_power_fraction * p_peak,
            bess_energy_mwh=bess_energy_hours * bess_power_fraction * p_peak,
            pv_capacity_mw=capacity,
        )
        rows.append((fraction, result.feasible, result.cost_reduction, capacity))
    return rows
