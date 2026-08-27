from __future__ import annotations

import unittest

from alibaba2018_dro import scheduler
from alibaba2018_dro.config import (
    CARBON_BUDGET_REDUCTIONS,
    DEFAULT_CARBON_BUDGET_REDUCTION,
)
from alibaba2018_dro.inputs import HourlyInput
from alibaba2018_dro.scenarios import ScenarioRealization


def _input(
    hour: int,
    *,
    price: float,
    forecast_carbon: float,
    batch_baseline: float,
    batch_window: float,
    online_mw: float = 0.0,
    forecast_solar: float = 0.0,
    forecast_wind: float = 0.0,
    actual_solar: float = 0.0,
    actual_wind: float = 0.0,
    cumulative_arrived: float | None = None,
    cumulative_due: float | None = None,
    workload_mwh_per_core_hour: float = 0.0,
) -> HourlyInput:
    return HourlyInput(
        hour=hour,
        timestamp_utc=f"2025-01-01T{hour + 1:02d}:00:00Z",
        dam_lz_houston_usd_per_mwh=price,
        forecast_erco_solar_generation_mwh=forecast_solar,
        forecast_erco_wind_generation_mwh=forecast_wind,
        forecast_consumed_co2_lbs_per_kwh=forecast_carbon,
        actual_consumed_co2_lbs_per_kwh=forecast_carbon,
        online_mw=online_mw,
        base_mw=0.0,
        batch_baseline_mwh=batch_baseline,
        batch_window_mwh=batch_window,
        actual_erco_solar_generation_mwh=actual_solar,
        actual_erco_wind_generation_mwh=actual_wind,
        batch_cumulative_arrived_mwh=cumulative_arrived,
        batch_cumulative_due_mwh=cumulative_due,
        workload_mwh_per_core_hour=workload_mwh_per_core_hour,
    )


class MainlineConfigTests(unittest.TestCase):
    def test_all_methods_share_declared_carbon_budget_set(self) -> None:
        self.assertEqual(CARBON_BUDGET_REDUCTIONS, (0.000, 0.025, 0.050))
        self.assertEqual(DEFAULT_CARBON_BUDGET_REDUCTION, 0.025)


@unittest.skipIf(scheduler.Model is None, "PySCIPOpt is only available in scip_env")
class MainlineSchedulerTests(unittest.TestCase):
    def test_saa_workload_chance_constraint_uses_joint_scenario_envelope(self) -> None:
        inputs = [
            _input(
                0,
                price=1.0,
                forecast_carbon=0.0,
                batch_baseline=1.0,
                batch_window=1.0,
                workload_mwh_per_core_hour=1.0,
            ),
            _input(
                1,
                price=10.0,
                forecast_carbon=0.0,
                batch_baseline=0.0,
                batch_window=1.0,
                workload_mwh_per_core_hour=1.0,
            ),
        ]
        scenario = ScenarioRealization(
            scenario_id=0,
            workload_source_days_one_based=(2,),
            energy_delivery_dates=("2024-01-01",),
            cumulative_arrived_core_hours=(0.0, 1.0),
            cumulative_due_core_hours=(0.0, 1.0),
            residual_solar_mwh=(0.0, 0.0),
            residual_wind_mwh=(0.0, 0.0),
            residual_carbon_lbs_per_kwh=(0.0, 0.0),
        )
        result = scheduler.solve_saa_wind_solar_storage(
            inputs,
            [scenario],
            g_max_mw=2.0,
            r_max_mw=2.0,
            p_grid_initial_mw=0.0,
            bess_power_mw=0.0,
            bess_energy_mwh=0.0,
            pv_capacity_mw=0.0,
            wind_capacity_mw=0.0,
            carbon_budget_reduction=0.0,
            beta_workload=0.0,
            beta_carbon=0.0,
            beta_grid=0.0,
            beta_ramp=0.0,
        )

        self.assertTrue(result.feasible)
        # 名义参考追随低价时段；场景追索再把未到达工作移到第 2 小时。
        self.assertAlmostEqual(result.plan.batch[0], 1.0, places=6)
        self.assertAlmostEqual(result.plan.batch[1], 0.0, places=6)
        self.assertEqual(result.workload_violation_rate, 0.0)
        self.assertAlmostEqual(result.mean_batch_adjustment_mwh, 2.0, places=6)
        replay = scheduler.replay_joint_scenario_with_batch_recourse(
            inputs,
            result.plan,
            scenario,
            pv_capacity_mw=0.0,
            wind_capacity_mw=0.0,
            g_max_mw=2.0,
            r_max_mw=2.0,
            p_grid_initial_mw=0.0,
        )
        self.assertAlmostEqual(replay.batch[0], 0.0, places=6)
        self.assertAlmostEqual(replay.batch[1], 1.0, places=6)
        self.assertAlmostEqual(replay.batch_adjustment_mwh, 2.0, places=6)
        self.assertFalse(replay.workload_violation)
        self.assertFalse(replay.carbon_violation)
        self.assertFalse(replay.grid_limit_violation)
        self.assertFalse(replay.ramp_violation)

        decomposed = scheduler.solve_decomposed_saa_wind_solar_storage(
            inputs,
            [scenario],
            g_max_mw=2.0,
            r_max_mw=2.0,
            p_grid_initial_mw=0.0,
            bess_power_mw=0.0,
            bess_energy_mwh=0.0,
            pv_capacity_mw=0.0,
            wind_capacity_mw=0.0,
            carbon_budget_reduction=0.0,
            beta_workload=0.0,
            beta_carbon=0.0,
            beta_grid=0.0,
            beta_ramp=0.0,
            max_iterations=2,
            replay_workers=2,
        )
        self.assertTrue(decomposed.feasible)
        self.assertEqual(decomposed.solver_status, "optimal_decomposed")
        self.assertEqual(decomposed.decomposition_iterations, 1)
        self.assertEqual(decomposed.active_scenario_count, 1)
        self.assertEqual(decomposed.scenario_count, 1)

    def test_cumulative_envelope_prevents_early_and_late_batch_execution(self) -> None:
        inputs = [
            _input(
                0,
                price=100.0,
                forecast_carbon=0.0,
                batch_baseline=0.0,
                batch_window=0.0,
                cumulative_arrived=0.0,
                cumulative_due=0.0,
            ),
            _input(
                1,
                price=50.0,
                forecast_carbon=0.0,
                batch_baseline=1.0,
                batch_window=1.0,
                cumulative_arrived=1.0,
                cumulative_due=0.0,
            ),
            _input(
                2,
                price=10.0,
                forecast_carbon=0.0,
                batch_baseline=0.0,
                batch_window=1.0,
                cumulative_arrived=1.0,
                cumulative_due=1.0,
            ),
        ]
        result = scheduler.solve_wind_solar_storage(
            inputs,
            g_max_mw=2.0,
            r_max_mw=2.0,
            p_grid_initial_mw=0.0,
            bess_power_mw=0.0,
            bess_energy_mwh=0.0,
            pv_capacity_mw=0.0,
            wind_capacity_mw=0.0,
            carbon_budget_reduction=0.0,
        )

        self.assertTrue(result.feasible)
        self.assertAlmostEqual(result.batch[0], 0.0, places=6)
        self.assertAlmostEqual(result.batch[1], 0.0, places=6)
        self.assertAlmostEqual(result.batch[2], 1.0, places=6)

    def test_carbon_budget_moves_flexible_batch_to_low_carbon_hour(self) -> None:
        inputs = [
            _input(
                0,
                price=10.0,
                forecast_carbon=1.0,
                batch_baseline=1.0,
                batch_window=1.0,
            ),
            _input(
                1,
                price=20.0,
                forecast_carbon=0.0,
                batch_baseline=0.0,
                batch_window=1.0,
            ),
        ]
        result = scheduler.solve_wind_solar_storage(
            inputs,
            g_max_mw=2.0,
            r_max_mw=2.0,
            p_grid_initial_mw=0.0,
            bess_power_mw=0.0,
            bess_energy_mwh=0.0,
            pv_capacity_mw=0.0,
            wind_capacity_mw=0.0,
            carbon_budget_reduction=0.5,
        )

        self.assertTrue(result.feasible)
        # 基准碳排放的一半正好允许 0.5 MWh 留在高碳小时。
        self.assertAlmostEqual(result.batch[0], 0.5, places=6)
        self.assertAlmostEqual(result.batch[1], 0.5, places=6)
        self.assertLessEqual(
            result.forecast_carbon_kg,
            result.carbon_budget_kg + 1e-6,
        )

    def test_actual_replay_uses_actual_wind_and_solar_and_reports_violation(self) -> None:
        inputs = [
            _input(
                0,
                price=10.0,
                forecast_carbon=0.5,
                batch_baseline=0.0,
                batch_window=0.0,
                online_mw=1.0,
                forecast_solar=1.0,
                forecast_wind=1.0,
            )
        ]
        plan = scheduler.solve_wind_solar_storage(
            inputs,
            g_max_mw=2.0,
            r_max_mw=2.0,
            p_grid_initial_mw=1.0,
            bess_power_mw=0.0,
            bess_energy_mwh=0.0,
            pv_capacity_mw=0.6,
            wind_capacity_mw=0.4,
            carbon_budget_reduction=0.0,
            solar_reference_mwh=1.0,
            wind_reference_mwh=1.0,
        )
        replay = scheduler.replay_actual_wind_solar(
            inputs,
            plan,
            pv_capacity_mw=0.6,
            wind_capacity_mw=0.4,
            solar_reference_mwh=1.0,
            wind_reference_mwh=1.0,
            g_max_mw=2.0,
            r_max_mw=2.0,
            p_grid_initial_mw=1.0,
        )

        self.assertAlmostEqual(plan.grid[0], 0.0, places=6)
        self.assertAlmostEqual(replay.grid[0], 1.0, places=6)
        self.assertGreater(replay.carbon_budget_violation_kg, 0.0)

    def test_bess_uses_total_throughput_cost_without_simultaneous_operation(self) -> None:
        inputs = [
            _input(
                0,
                price=10.0,
                forecast_carbon=0.0,
                batch_baseline=0.0,
                batch_window=0.0,
                online_mw=1.0,
            ),
            _input(
                1,
                price=100.0,
                forecast_carbon=0.0,
                batch_baseline=0.0,
                batch_window=0.0,
                online_mw=1.0,
            ),
        ]
        result = scheduler.solve_wind_solar_storage(
            inputs,
            g_max_mw=3.0,
            r_max_mw=3.0,
            p_grid_initial_mw=1.0,
            bess_power_mw=1.0,
            bess_energy_mwh=2.0,
            bess_efficiency=1.0,
            soc_min=0.0,
            soc_max=1.0,
            soc_initial=0.5,
            pv_capacity_mw=0.0,
            wind_capacity_mw=0.0,
            carbon_budget_reduction=0.0,
            bess_degradation_cost_usd_per_mwh_throughput=20.0,
        )

        self.assertAlmostEqual(result.bess_degradation_cost, 40.0, places=6)
        self.assertAlmostEqual(result.operating_cost, 60.0, places=6)
        self.assertTrue(
            all(
                charge * discharge <= 1e-9
                for charge, discharge in zip(
                    result.bess_charge, result.bess_discharge
                )
            )
        )


if __name__ == "__main__":
    unittest.main()
