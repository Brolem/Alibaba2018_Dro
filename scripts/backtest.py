"""样本外回测：用多个随机实现验证名义/鲁棒方案的违约率与收益。

思路：名义方案按预测值调度，鲁棒方案按最坏情况多排能量、少算 PV。
随机生成批处理总能量与 PV 出力的实现，检查“实际值超过计划预留”和“购电越限”两种违约。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

from alibaba2018_dro.inputs import DATA, build_hourly_input
from alibaba2018_dro.scheduler import _peak_load, _pv_profile, solve_batch_shift


FIGURES = PROJECT_ROOT / "docs" / "figures"


def run_backtest(
    inputs,
    *,
    gamma: float,
    gamma_pv: float,
    p_peak: float,
    p_must: float,
    g_max_mw: float,
    r_max_mw: float,
    bess_power_mw: float,
    bess_energy_mwh: float,
    pv_capacity_mw: float,
    delta: float,
    eps: float,
    seed: int,
    realizations: int,
) -> dict:
    plan = solve_batch_shift(
        inputs,
        g_max_mw=g_max_mw,
        r_max_mw=r_max_mw,
        p_grid_initial_mw=p_must + inputs[0].batch_baseline_mwh,
        bess_power_mw=bess_power_mw,
        bess_energy_mwh=bess_energy_mwh,
        pv_capacity_mw=pv_capacity_mw,
        pv_robustness_budget=gamma_pv,
        pv_relative_error=eps,
        robustness_budget=gamma,
        energy_uncertainty_fraction=delta,
    )
    if not plan.feasible:
        return {"feasible": False, "gamma": gamma, "gamma_pv": gamma_pv}

    energy_nominal = sum(item.batch_baseline_mwh for item in inputs)
    energy_planned = sum(plan.batch)
    pv_nominal = _pv_profile(inputs, pv_capacity_mw)
    pv_assumed = _pv_profile(inputs, pv_capacity_mw * (1.0 - gamma_pv * eps))

    rng = np.random.default_rng(seed)
    batch_violations = 0
    grid_violations = 0
    shortfalls: list[float] = []

    for _ in range(realizations):
        zeta = rng.normal(0.0, delta)
        energy_real = energy_nominal * (1.0 + zeta)
        xi = rng.uniform(0.0, eps)

        if energy_real > energy_planned:
            batch_violations += 1
            shortfalls.append(energy_real - energy_planned)

        for t, item in enumerate(inputs):
            realized_grid = plan.grid[t] + (
                pv_assumed[t] - pv_nominal[t] * (1.0 - xi)
            )
            if realized_grid > g_max_mw + 1e-9:
                grid_violations += 1
                break

    return {
        "feasible": True,
        "gamma": gamma,
        "gamma_pv": gamma_pv,
        "cost_reduction": plan.cost_reduction,
        "batch_violation_rate": batch_violations / realizations,
        "grid_violation_rate": grid_violations / realizations,
        "mean_energy_shortfall_mwh": float(np.mean(shortfalls)) if shortfalls else 0.0,
    }


def main() -> None:
    inputs = build_hourly_input(
        DATA / "energy" / "windows" / "2025-01-01_30d_d168_h3_energy.csv",
        DATA / "workload" / "generated_envelope_30d.csv",
        DATA / "workload" / "workload_stats.json",
    )
    p_must = inputs[0].online_mw + inputs[0].base_mw
    p_peak = _peak_load(inputs)
    g_max_mw = 1.0 * p_peak
    r_max_mw = 0.1 * p_peak
    bess_power_mw = 0.5 * p_peak
    bess_energy_mwh = 2.0 * bess_power_mw
    pv_capacity_mw = 1.0 * p_must
    delta = 0.079
    eps = 0.243

    gammas = [0.0, 0.25, 0.5, 0.75, 1.0]
    rows = []
    for gamma in gammas:
        row = run_backtest(
            inputs,
            gamma=gamma,
            gamma_pv=gamma,
            p_peak=p_peak,
            p_must=p_must,
            g_max_mw=g_max_mw,
            r_max_mw=r_max_mw,
            bess_power_mw=bess_power_mw,
            bess_energy_mwh=bess_energy_mwh,
            pv_capacity_mw=pv_capacity_mw,
            delta=delta,
            eps=eps,
            seed=0,
            realizations=400,
        )
        rows.append(row)
        print(
            f"Gamma={gamma}: cost_reduction={row.get('cost_reduction', 0):.4f}, "
            f"batch_violation={row.get('batch_violation_rate', 0):.4f}, "
            f"grid_violation={row.get('grid_violation_rate', 0):.4f}"
        )

    out = PROJECT_ROOT / "data" / "workload" / "backtest_results.json"
    out.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")

    FIGURES.mkdir(parents=True, exist_ok=True)
    feasible = [r for r in rows if r.get("feasible")]
    gamma_vals = [r["gamma"] for r in feasible]
    cost = [r["cost_reduction"] * 100.0 for r in feasible]
    viol = [
        max(r["batch_violation_rate"], r["grid_violation_rate"]) * 100.0
        for r in feasible
    ]

    fig, ax1 = plt.subplots(figsize=(6, 4))
    ax1.plot(gamma_vals, cost, "o-", color="#1f77b4", label="cost reduction (%)")
    ax1.set_xlabel("Robustness budget $\\Gamma = \\Gamma_{pv}$")
    ax1.set_ylabel("Total cost reduction (%)", color="#1f77b4")
    ax1.tick_params(axis="y", labelcolor="#1f77b4")
    ax2 = ax1.twinx()
    ax2.plot(gamma_vals, viol, "s--", color="#d62728", label="violation rate (%)")
    ax2.set_ylabel("Violation rate (%)", color="#d62728")
    ax2.tick_params(axis="y", labelcolor="#d62728")
    ax1.set_title("Robustness-reliability tradeoff (out-of-sample)")
    ax1.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(FIGURES / "fig_backtest_tradeoff.png", dpi=160)
    plt.close(fig)

    print("written:", out)


if __name__ == "__main__":
    main()
