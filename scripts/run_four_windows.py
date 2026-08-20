"""跑四个 2025 窗口，输出成本/碳/尖峰/爬坡/PV 自用率指标。"""

from __future__ import annotations

import csv
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from alibaba2018_dro.inputs import DATA, build_hourly_input
from alibaba2018_dro.scheduler import (
    _peak_load,
    _pv_profile,
    solve_lexicographic,
)


WINDOWS = ["2025-01-01", "2025-04-01", "2025-07-01", "2025-10-01"]
LBS_PER_KG = 0.45359237


def _metrics(inputs, grid):
    """返回 (成本 USD, 碳 kgCO2, 尖峰 MW, 爬坡 MW/h)。碳用实际碳强度。"""
    cost = sum(item.dam_lz_houston_usd_per_mwh * grid[i] for i, item in enumerate(inputs))
    carbon = sum(
        grid[i] * 1000.0 * item.actual_consumed_co2_lbs_per_kwh * LBS_PER_KG
        for i, item in enumerate(inputs)
    )
    peak = max(grid)
    ramp = max(abs(grid[i] - grid[i - 1]) for i in range(1, len(grid)))
    return cost, carbon, peak, ramp


def _pv_self_use(inputs, result, pv_capacity_mw):
    """PV 自用率 = 就地消纳的 PV / 名义 PV 出力。"""
    p_must = inputs[0].online_mw + inputs[0].base_mw
    pv_nom = _pv_profile(inputs, pv_capacity_mw)
    used = 0.0
    for t, item in enumerate(inputs):
        load = (
            p_must
            + result.batch[t]
            + result.bess_charge[t]
            - result.bess_discharge[t]
        )
        used += min(pv_nom[t], max(0.0, load))
    total = sum(pv_nom)
    return used / total if total > 0 else 0.0


def main() -> None:
    envelope = DATA / "workload" / "generated_envelope_30d.csv"
    stats = DATA / "workload" / "workload_stats.json"
    rows: list[dict] = []

    for window_start in WINDOWS:
        window = (
            DATA / "energy" / "windows" / f"{window_start}_30d_d168_h3_energy.csv"
        )
        inputs = build_hourly_input(window, envelope, stats)
        p_must = inputs[0].online_mw + inputs[0].base_mw
        p_peak = _peak_load(inputs)
        g_max_mw = 1.0 * p_peak
        r_max_mw = 0.1 * p_peak
        bess_power_mw = 0.5 * p_peak
        bess_energy_mwh = 2.0 * bess_power_mw
        pv_capacity_mw = 1.0 * p_must
        p_grid_initial = p_must + inputs[0].batch_baseline_mwh

        base_grid = [p_must + item.batch_baseline_mwh for item in inputs]
        base = _metrics(inputs, base_grid)

        for label, gamma in (("optimal_G0", 0.0), ("robust_G1", 1.0)):
            result = solve_lexicographic(
                inputs,
                g_max_mw=g_max_mw,
                r_max_mw=r_max_mw,
                p_grid_initial_mw=p_grid_initial,
                bess_power_mw=bess_power_mw,
                bess_energy_mwh=bess_energy_mwh,
                pv_capacity_mw=pv_capacity_mw,
                robustness_budget=gamma,
                pv_robustness_budget=gamma,
            )
            m = _metrics(inputs, result.grid)
            self_use = _pv_self_use(inputs, result, pv_capacity_mw)
            rows.append(
                {
                    "window": window_start,
                    "scenario": label,
                    "cost_usd": round(m[0], 2),
                    "carbon_kg": round(m[1], 2),
                    "peak_mw": round(m[2], 3),
                    "ramp_mw": round(m[3], 3),
                    "pv_self_use": round(self_use, 4),
                    "cost_reduction": round((base[0] - m[0]) / base[0], 4),
                    "carbon_reduction": round((base[1] - m[1]) / base[1], 4),
                    "peak_reduction": round((base[2] - m[2]) / base[2], 4),
                    "ramp_reduction": round((base[3] - m[3]) / base[3], 4),
                }
            )
            print(
                f"{window_start} {label}: cost={m[0]:.2f} ({(base[0]-m[0])/base[0]*100:.2f}%), "
                f"carbon={m[1]:.2f} ({(base[1]-m[1])/base[1]*100:.2f}%), "
                f"peak={m[2]:.3f}, ramp={m[3]:.3f}, pv_self_use={self_use:.4f}"
            )

    out = DATA / "workload" / "four_windows_summary.csv"
    with out.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    print("written:", out)


if __name__ == "__main__":
    main()
