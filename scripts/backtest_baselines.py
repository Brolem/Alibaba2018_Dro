"""对照基线的样本外回测（四窗口）：确定性 / SAA / 逐小时预算鲁棒的 PV 越限率。"""

from __future__ import annotations

import csv
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import numpy as np

from alibaba2018_dro.inputs import DATA, build_hourly_input
from alibaba2018_dro.scheduler import (
    _peak_load,
    _pv_profile,
    solve_batch_shift,
    solve_robust_budgeted,
    solve_saa,
)


WINDOWS = ["2025-01-01", "2025-04-01", "2025-07-01", "2025-10-01"]
EPS = 0.243
REALIZATIONS = 500


def evaluate_window(window_start: str) -> list[dict]:
    """对单个窗口求解三个方案并回测，返回每方法的 (成本下降, PV 越限率)。"""

    inputs = build_hourly_input(
        DATA / "energy" / "windows" / f"{window_start}_30d_d168_h3_energy.csv",
        DATA / "workload" / "generated_envelope_30d.csv",
        DATA / "workload" / "workload_stats.json",
    )
    p_must = inputs[0].online_mw + inputs[0].base_mw
    p_peak = _peak_load(inputs)
    g_max = 1.0 * p_peak
    kwargs = dict(
        g_max_mw=g_max,
        r_max_mw=0.1 * p_peak,
        p_grid_initial_mw=p_must + inputs[0].batch_baseline_mwh,
        bess_power_mw=0.5 * p_peak,
        bess_energy_mwh=2.0 * 0.5 * p_peak,
        pv_capacity_mw=1.0 * p_must,
    )

    det = solve_batch_shift(inputs, **kwargs)
    rob = solve_robust_budgeted(inputs, gamma_pv=72, **kwargs)
    saa = solve_saa(inputs, scenarios=20, seed=0, **kwargs)

    xi_bar = EPS / 2.0
    pv_nom = _pv_profile(inputs, kwargs["pv_capacity_mw"])
    rng = np.random.default_rng(0)
    det_overload = saa_overload = rob_overload = 0
    for _ in range(REALIZATIONS):
        xi = rng.uniform(0.0, EPS)
        if any(det.grid[t] + xi * pv_nom[t] > g_max + 1e-9 for t in range(len(inputs))):
            det_overload += 1
        if any(rob.grid[t] + xi * pv_nom[t] > g_max + 1e-9 for t in range(len(inputs))):
            rob_overload += 1
        if any(
            saa.grid[t] + (xi - xi_bar) * pv_nom[t] > g_max + 1e-9
            for t in range(len(inputs))
        ):
            saa_overload += 1

    return [
        {
            "window": window_start,
            "method": "deterministic",
            "cost_reduction": round(det.cost_reduction, 4),
            "pv_overload_rate": round(det_overload / REALIZATIONS, 4),
        },
        {
            "window": window_start,
            "method": "SAA(20)",
            "cost_reduction": round(saa.cost_reduction, 4),
            "pv_overload_rate": round(saa_overload / REALIZATIONS, 4),
        },
        {
            "window": window_start,
            "method": "robust(Gamma_pv=72)",
            "cost_reduction": round(rob.cost_reduction, 4),
            "pv_overload_rate": round(rob_overload / REALIZATIONS, 4),
        },
    ]


def main() -> None:
    all_rows: list[dict] = []
    for window in WINDOWS:
        rows = evaluate_window(window)
        all_rows.extend(rows)
        for r in rows:
            print(
                f"{r['window']} {r['method']}: "
                f"cost_reduction={r['cost_reduction']:.4f}, "
                f"pv_overload_rate={r['pv_overload_rate']:.4f}"
            )

    out = DATA / "workload" / "baseline_backtest_four_windows.csv"
    with out.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(all_rows[0].keys()))
        writer.writeheader()
        writer.writerows(all_rows)
    print("written:", out)


if __name__ == "__main__":
    main()
