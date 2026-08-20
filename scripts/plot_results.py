"""基于 Jan 2025 扫参结果生成论文插图。"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

from alibaba2018_dro.inputs import DATA_PROCESSED, build_hourly_input
from alibaba2018_dro.scheduler import (
    solve_batch_shift,
    sweep_bess_power,
    sweep_grid_limit,
    sweep_pv_capacity,
    sweep_ramp_limit,
    sweep_robustness_budget,
    sweep_pv_robustness,
)


FIGURES = Path(__file__).resolve().parents[1] / "docs" / "figures"


def _jan_inputs():
    return build_hourly_input(
        DATA_PROCESSED / "energy" / "windows" / "2025-01-01_30d_d168_h3_energy.csv",
        DATA_PROCESSED / "workload" / "generated_envelope_30d.csv",
        DATA_PROCESSED / "workload" / "workload_stats.json",
    )


def _feasible(rows):
    return [(x, rd * 100.0) for x, ok, rd in rows if ok]


def _infeasible_x(rows):
    return [x for x, ok, _rd in rows if not ok]


def main() -> None:
    inputs = _jan_inputs()
    FIGURES.mkdir(parents=True, exist_ok=True)

    # 图 1：算力侧/能源侧鲁棒预算的保守性—可靠性折中
    gamma_rows = sweep_robustness_budget(
        inputs,
        g_max_fraction=0.8,
        r_max_fraction=0.1,
        budgets=[0.0, 0.25, 0.5, 0.75, 1.0],
    )
    pv_gamma_rows = sweep_pv_robustness(
        inputs,
        g_max_fraction=0.8,
        r_max_fraction=0.1,
        budgets=[0.0, 0.25, 0.5, 0.75, 1.0],
    )
    plt.figure(figsize=(5, 3.5))
    g, rd = zip(*_feasible(gamma_rows))
    pg, prd = zip(*_feasible(pv_gamma_rows))
    plt.plot(g, rd, "o-", label="compute-side $\\Gamma$", color="#1f77b4")
    plt.plot(pg, prd, "s-", label="energy-side $\\Gamma_{pv}$", color="#d62728")
    plt.xlabel("Robustness budget $\\Gamma$")
    plt.ylabel("Total cost reduction (%)")
    plt.title("Conservatism-reliability tradeoff")
    plt.legend()
    plt.grid(alpha=0.3)
    plt.tight_layout()
    plt.savefig(FIGURES / "fig_gamma_ro.png", dpi=160)
    plt.close()

    # 图 2：BESS 功率与 PV 容量敏感性（双面板）
    bess_rows = sweep_bess_power(
        inputs,
        g_max_fraction=0.8,
        r_max_fraction=0.1,
        power_fractions=[0.0, 0.1, 0.25, 0.5, 0.75, 1.0],
    )
    pv_no_bess = sweep_pv_capacity(
        inputs,
        g_max_fraction=0.8,
        r_max_fraction=0.1,
        pv_fractions=[0.0, 0.5, 1.0, 1.5, 2.0],
        bess_power_fraction=0.0,
    )
    pv_bess = sweep_pv_capacity(
        inputs,
        g_max_fraction=0.8,
        r_max_fraction=0.1,
        pv_fractions=[0.0, 0.5, 1.0, 1.5, 2.0],
        bess_power_fraction=0.5,
    )
    fig, axes = plt.subplots(1, 2, figsize=(10, 3.8))
    bx, brd = zip(*_feasible([(b, ok, rd) for b, ok, rd, _ in bess_rows]))
    axes[0].plot(bx, brd, "o-", color="#2ca02c")
    axes[0].set_xlabel("BESS power / $P_{peak}$")
    axes[0].set_ylabel("Total cost reduction (%)")
    axes[0].set_title("BESS power sensitivity")
    axes[0].grid(alpha=0.3)
    px, pn = zip(*_feasible([(p, ok, rd) for p, ok, rd, _ in pv_no_bess]))
    axes[1].plot(px, pn, "o-", label="no BESS", color="#d62728")
    for p in _infeasible_x([(p, ok, rd) for p, ok, rd, _ in pv_no_bess]):
        axes[1].plot(p, 0, "x", color="#d62728")
    px2, pb = zip(*_feasible([(p, ok, rd) for p, ok, rd, _ in pv_bess]))
    axes[1].plot(px2, pb, "s-", label="BESS 0.5x", color="#1f77b4")
    for p in _infeasible_x([(p, ok, rd) for p, ok, rd, _ in pv_bess]):
        axes[1].plot(p, 0, "x", color="#1f77b4")
    axes[1].set_xlabel("PV capacity / $P_{must}$")
    axes[1].set_ylabel("Total cost reduction (%)")
    axes[1].set_title("PV capacity sensitivity")
    axes[1].legend()
    axes[1].grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(FIGURES / "fig_bess_pv.png", dpi=160)
    plt.close(fig)

    # 图 3：并网功率上限与爬坡上限敏感性（双面板）
    gmax_rows = sweep_grid_limit(
        inputs, g_max_fractions=[0.5, 0.6, 0.8, 1.0, 1.2]
    )
    rmax_rows = sweep_ramp_limit(
        inputs, g_max_fraction=1.0, r_max_fractions=[0.02, 0.05, 0.1, 0.5, 1.0]
    )
    fig, axes = plt.subplots(1, 2, figsize=(10, 3.8))
    gx, grd = zip(*_feasible(gmax_rows))
    axes[0].plot(gx, grd, "o-", color="#9467bd")
    for x in _infeasible_x(gmax_rows):
        axes[0].plot(x, 0, "x", color="#9467bd")
    axes[0].set_xlabel("$G_{max} / P_{peak}$")
    axes[0].set_ylabel("Total cost reduction (%)")
    axes[0].set_title("Grid connection limit")
    axes[0].grid(alpha=0.3)
    rx, rrd = zip(*_feasible(rmax_rows))
    axes[1].plot(rx, rrd, "o-", color="#8c564b")
    for x in _infeasible_x(rmax_rows):
        axes[1].plot(x, 0, "x", color="#8c564b")
    axes[1].set_xlabel("$R_{max} / P_{peak}$ per h")
    axes[1].set_ylabel("Total cost reduction (%)")
    axes[1].set_title("Ramp rate limit")
    axes[1].grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(FIGURES / "fig_grid_ramp.png", dpi=160)
    plt.close(fig)

    print("written figures to", FIGURES)


if __name__ == "__main__":
    main()
