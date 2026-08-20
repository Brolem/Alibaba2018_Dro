"""对照基线：确定性 vs 逐小时预算鲁棒 vs SAA（Jan 2025）。"""

from __future__ import annotations

import csv
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from alibaba2018_dro.inputs import DATA_PROCESSED, DATA_RESULTS, build_hourly_input
from alibaba2018_dro.scheduler import (
    _peak_load,
    solve_batch_shift,
    solve_robust_budgeted,
    solve_saa,
)


def main() -> None:
    inputs = build_hourly_input(
        DATA_PROCESSED / "energy" / "windows" / "2025-01-01_30d_d168_h3_energy.csv",
        DATA_PROCESSED / "workload" / "generated_envelope_30d.csv",
        DATA_PROCESSED / "workload" / "workload_stats.json",
    )
    p_must = inputs[0].online_mw + inputs[0].base_mw
    p_peak = _peak_load(inputs)
    kwargs = dict(
        g_max_mw=1.0 * p_peak,
        r_max_mw=0.1 * p_peak,
        p_grid_initial_mw=p_must + inputs[0].batch_baseline_mwh,
        bess_power_mw=0.5 * p_peak,
        bess_energy_mwh=2.0 * 0.5 * p_peak,
        pv_capacity_mw=1.0 * p_must,
    )

    rows = []

    det = solve_batch_shift(inputs, **kwargs)
    rows.append(("deterministic", det.cost_reduction, det.optimal_cost))

    saa = solve_saa(inputs, scenarios=20, seed=0, **kwargs)
    rows.append(("SAA(20)", saa.cost_reduction, saa.optimal_cost))

    for g in (1, 72, 144, 360, 720):
        rob = solve_robust_budgeted(inputs, gamma_pv=g, **kwargs)
        rows.append((f"robust(Gamma_pv={g})", rob.cost_reduction, rob.optimal_cost))

    out = DATA_RESULTS / "baseline_comparison.csv"
    with out.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["method", "cost_reduction", "optimal_cost"])
        w.writerows(rows)

    for method, reduction, cost in rows:
        print(f"{method}: reduction={reduction:.4f}, cost={cost:.2f}")
    print("written:", out)


if __name__ == "__main__":
    main()
