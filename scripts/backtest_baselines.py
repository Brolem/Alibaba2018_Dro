"""对照基线的样本外回测：比较确定性 / SAA / 逐小时预算鲁棒的 PV 越限率。"""

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


def main() -> None:
    inputs = build_hourly_input(
        DATA / "energy" / "windows" / "2025-01-01_30d_d168_h3_energy.csv",
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

    # 三个方案
    det = solve_batch_shift(inputs, **kwargs)                 # 确定性：假设 PV 完全按预测
    rob = solve_robust_budgeted(inputs, gamma_pv=72, **kwargs)  # 鲁棒：预留 PV 短缺
    saa = solve_saa(inputs, scenarios=20, seed=0, **kwargs)     # SAA：按平均短缺规划

    eps = 0.243
    xi_bar = eps / 2.0  # SAA 场景的平均短缺因子
    pv_nom = _pv_profile(inputs, kwargs["pv_capacity_mw"])

    rng = np.random.default_rng(0)
    realizations = 500
    det_overload = saa_overload = rob_overload = 0
    for _ in range(realizations):
        xi = rng.uniform(0.0, eps)  # 全局 PV 短缺因子（每小时相同，便于解释）
        # 确定性：假设短缺 0，实际短缺 xi
        if any(det.grid[t] + xi * pv_nom[t] > g_max + 1e-9 for t in range(len(inputs))):
            det_overload += 1
        # 鲁棒：名义平衡同确定性，但并网预留了 eps·pv_nom
        if any(rob.grid[t] + xi * pv_nom[t] > g_max + 1e-9 for t in range(len(inputs))):
            rob_overload += 1
        # SAA：按平均短缺 xi_bar 规划，实际短缺 xi
        if any(
            saa.grid[t] + (xi - xi_bar) * pv_nom[t] > g_max + 1e-9
            for t in range(len(inputs))
        ):
            saa_overload += 1

    rows = [
        ("deterministic", det.cost_reduction, det_overload / realizations),
        ("SAA(20)", saa.cost_reduction, saa_overload / realizations),
        ("robust(Gamma_pv=72)", rob.cost_reduction, rob_overload / realizations),
    ]
    out = DATA / "workload" / "baseline_backtest.csv"
    with out.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["method", "cost_reduction", "pv_overload_rate"])
        w.writerows(rows)

    for method, reduction, overload in rows:
        print(f"{method}: cost_reduction={reduction:.4f}, pv_overload_rate={overload:.4f}")
    print("written:", out)


if __name__ == "__main__":
    main()
