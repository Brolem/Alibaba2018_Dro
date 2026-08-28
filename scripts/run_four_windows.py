"""运行成本—运行可靠性主线的四个 2025 能源窗口。"""

from __future__ import annotations

import csv
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from alibaba2018_dro.config import (
    PV_CAPACITY_FRACTION_OF_MUST_LOAD,
    WIND_CAPACITY_FRACTION_OF_MUST_LOAD,
)
from alibaba2018_dro.inputs import DATA_PROCESSED, DATA_RESULTS, build_hourly_input
from alibaba2018_dro.scheduler import (
    _peak_load,
    replay_actual_wind_solar,
    solve_wind_solar_storage,
)


WINDOWS = ["2025-01-01", "2025-04-01", "2025-07-01", "2025-10-01"]


def main() -> None:
    envelope = DATA_PROCESSED / "workload" / "nominal_workload_30d.csv"
    stats = DATA_PROCESSED / "workload" / "workload_stats.json"
    rows: list[dict[str, float | int | str | None]] = []

    for window_start in WINDOWS:
        window = (
            DATA_PROCESSED
            / "energy"
            / "windows"
            / f"{window_start}_30d_d168_h3_energy.csv"
        )
        inputs = build_hourly_input(window, envelope, stats)
        p_must = inputs[0].online_mw + inputs[0].base_mw
        p_peak = _peak_load(inputs)
        g_max_mw = p_peak
        r_max_mw = 0.1 * p_peak
        bess_power_mw = 0.5 * p_peak
        bess_energy_mwh = 2.0 * bess_power_mw
        pv_capacity_mw = PV_CAPACITY_FRACTION_OF_MUST_LOAD * p_must
        wind_capacity_mw = WIND_CAPACITY_FRACTION_OF_MUST_LOAD * p_must

        common = {
            "window": window_start,
            "effective_capacity_cores": round(
                inputs[0].effective_capacity_cores or 0.0, 2
            ),
            "workload_scale": round(inputs[0].workload_scale, 8),
            "pv_capacity_mw": round(pv_capacity_mw, 4),
            "wind_capacity_mw": round(wind_capacity_mw, 4),
        }
        plan = solve_wind_solar_storage(
            inputs,
            g_max_mw=g_max_mw,
            r_max_mw=r_max_mw,
            p_grid_initial_mw=p_must,
            bess_power_mw=bess_power_mw,
            bess_energy_mwh=bess_energy_mwh,
            pv_capacity_mw=pv_capacity_mw,
            wind_capacity_mw=wind_capacity_mw,
        )
        if not plan.feasible:
            rows.append(
                {
                    **common,
                    "feasible": False,
                    "grid_cost_usd": None,
                    "bess_degradation_cost_usd": None,
                    "operating_cost_usd": None,
                    "cost_reduction": None,
                    "forecast_carbon_kg": None,
                    "actual_grid_cost_usd": None,
                    "actual_operating_cost_usd": None,
                    "actual_carbon_kg": None,
                    "forecast_curtailment_mwh": None,
                    "actual_curtailment_mwh": None,
                    "actual_grid_limit_violation_hours": None,
                    "actual_ramp_violation_hours": None,
                }
            )
            print(f"{window_start}: infeasible")
            continue
        replay = replay_actual_wind_solar(
            inputs,
            plan,
            pv_capacity_mw=pv_capacity_mw,
            wind_capacity_mw=wind_capacity_mw,
            g_max_mw=g_max_mw,
            r_max_mw=r_max_mw,
            p_grid_initial_mw=p_must,
        )
        rows.append(
            {
                **common,
                "feasible": True,
                "grid_cost_usd": round(plan.grid_cost, 2),
                "bess_degradation_cost_usd": round(
                    plan.bess_degradation_cost, 2
                ),
                "operating_cost_usd": round(plan.operating_cost, 2),
                "cost_reduction": round(plan.cost_reduction, 4),
                "forecast_carbon_kg": round(plan.forecast_carbon_kg, 2),
                "actual_grid_cost_usd": round(replay.grid_cost, 2),
                "actual_operating_cost_usd": round(replay.operating_cost, 2),
                "actual_carbon_kg": round(replay.carbon_kg, 2),
                "forecast_curtailment_mwh": round(
                    max(0.0, sum(plan.curtailment)), 2
                ),
                "actual_curtailment_mwh": round(
                    max(0.0, sum(replay.curtailment)), 2
                ),
                "actual_grid_limit_violation_hours": replay.grid_limit_violation_hours,
                "actual_ramp_violation_hours": replay.ramp_violation_hours,
            }
        )
        print(
            f"{window_start}: operating={plan.operating_cost:.2f}, "
            f"forecast_carbon={plan.forecast_carbon_kg:.2f} kg, "
            f"actual_carbon={replay.carbon_kg:.2f} kg"
        )

    if not rows:
        raise RuntimeError("all mainline scenarios were infeasible")
    out = DATA_RESULTS / "four_windows_mainline_summary.csv"
    with out.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    print("written:", out)


if __name__ == "__main__":
    main()
