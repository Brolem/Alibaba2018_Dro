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


def solve_batch_shift(inputs: list[HourlyInput]) -> BatchShiftResult:
    """Shift deferrable batch energy across the flexible envelope to cut cost."""

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
    model.optimize()

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
    )
