"""Baseline day-ahead flexible-load scheduler (PySCIPOpt)."""

from __future__ import annotations

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
    feasible: bool = True


def solve_batch_shift(
    inputs: list[HourlyInput],
    *,
    g_max_mw: float | None = None,
    r_max_mw: float | None = None,
    p_grid_initial_mw: float | None = None,
) -> BatchShiftResult:
    """Shift deferrable batch energy to cut cost, with optional grid/ramp limits."""

    if Model is None:
        raise RuntimeError("pyscipopt is required; run inside scip_env")

    model = Model("batch_shift")
    model.hideOutput()

    batch = {
        item.hour: model.addVar(
            lb=0.0,
            ub=item.batch_window_mwh,
            name=f"batch_{item.hour}",
        )
        for item in inputs
    }

    total_energy = sum(item.batch_baseline_mwh for item in inputs)
    model.addCons(
        sum(batch[item.hour] for item in inputs) == total_energy,
        name="batch_energy_conservation",
    )

    model.setObjective(
        sum(
            item.dam_lz_houston_usd_per_mwh * batch[item.hour]
            for item in inputs
        ),
        "minimize",
    )

    p_must = inputs[0].online_mw + inputs[0].base_mw
    if g_max_mw is not None:
        for item in inputs:
            model.addCons(
                p_must + batch[item.hour] <= g_max_mw,
                name=f"grid_limit_{item.hour}",
            )
    if r_max_mw is not None:
        previous = p_grid_initial_mw
        for item in inputs:
            current = p_must + batch[item.hour]
            if previous is not None:
                model.addCons(
                    current - previous <= r_max_mw,
                    name=f"ramp_up_{item.hour}",
                )
                model.addCons(
                    current - previous >= -r_max_mw,
                    name=f"ramp_down_{item.hour}",
                )
            previous = current

    model.optimize()

    if model.getStatus() != "optimal":
        return BatchShiftResult(
            baseline_cost=0.0,
            optimal_cost=0.0,
            cost_reduction=0.0,
            batch=[],
            feasible=False,
        )

    batch_values = [model.getVal(batch[item.hour]) for item in inputs]
    optimal_cost = sum(
        item.dam_lz_houston_usd_per_mwh * batch_values[index]
        for index, item in enumerate(inputs)
    )
    baseline_cost = sum(
        item.dam_lz_houston_usd_per_mwh * item.batch_baseline_mwh
        for item in inputs
    )

    return BatchShiftResult(
        baseline_cost=baseline_cost,
        optimal_cost=optimal_cost,
        cost_reduction=(
            (baseline_cost - optimal_cost) / baseline_cost
            if baseline_cost
            else 0.0
        ),
        batch=batch_values,
        feasible=True,
    )


def sweep_grid_limit(
    inputs: list[HourlyInput],
    *,
    g_max_fractions: list[float],
    r_max_fraction: float | None = None,
) -> list[tuple[float, bool, float]]:
    """Sweep G_max as a fraction of the baseline peak load."""

    p_must = inputs[0].online_mw + inputs[0].base_mw
    p_peak = max(p_must + item.batch_baseline_mwh for item in inputs)
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

    p_must = inputs[0].online_mw + inputs[0].base_mw
    p_peak = max(p_must + item.batch_baseline_mwh for item in inputs)
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
