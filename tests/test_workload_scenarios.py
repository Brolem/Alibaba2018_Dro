from __future__ import annotations

import unittest

import numpy as np

from scripts.generate_workload import generate_nominal_scenario, generate_scenarios


class WorkloadScenarioTests(unittest.TestCase):
    def test_nominal_scenario_balances_retained_source_days(self) -> None:
        daily = np.arange(1.0, 7.0 * 24.0 + 1.0).reshape(7, 24)

        scenario, target_total = generate_nominal_scenario(
            daily,
            days=30,
            seed=0,
            block_days=2,
            flex_window_hours=6,
        )

        counts = np.bincount(scenario.source_days, minlength=7)
        self.assertEqual(len(scenario.arrival_work), 720)
        self.assertEqual(sorted(counts.tolist()), [4, 4, 4, 4, 4, 5, 5])
        self.assertAlmostEqual(float(scenario.arrival_work.sum()), target_total)
        self.assertAlmostEqual(target_total, float(daily.mean(axis=0).sum()) * 30)
        for first, second in zip(
            scenario.source_days[0::2], scenario.source_days[1::2]
        ):
            self.assertEqual(second, (first + 1) % 7)

    def test_block_scenarios_conserve_work_and_build_valid_envelopes(self) -> None:
        daily = np.arange(1.0, 8.0 * 24.0 + 1.0).reshape(8, 24)

        scenarios, target_total = generate_scenarios(
            daily,
            days=30,
            scenario_count=3,
            seed=7,
            block_days=2,
            flex_window_hours=6,
        )

        self.assertEqual(len(scenarios), 3)
        for scenario in scenarios:
            self.assertEqual(len(scenario.arrival_work), 720)
            self.assertAlmostEqual(float(scenario.arrival_work.sum()), target_total)
            self.assertAlmostEqual(float(scenario.cumulative_arrived[-1]), target_total)
            self.assertAlmostEqual(float(scenario.cumulative_due[-1]), target_total)
            self.assertTrue(
                np.all(scenario.cumulative_due <= scenario.cumulative_arrived + 1e-6)
            )
            for first, second in zip(
                scenario.source_days[0::2], scenario.source_days[1::2]
            ):
                self.assertEqual(second, (first + 1) % 8)

    def test_flexibility_window_is_independent_of_work_amount(self) -> None:
        daily = np.zeros((8, 24), dtype=np.float64)
        daily[:, 0] = 10.0

        scenarios, _ = generate_scenarios(
            daily,
            days=1,
            scenario_count=1,
            seed=0,
            block_days=2,
            flex_window_hours=6,
        )
        scenario = scenarios[0]

        self.assertEqual(float(scenario.cumulative_due[5]), 0.0)
        self.assertEqual(float(scenario.cumulative_due[6]), 10.0)
        self.assertEqual(float(scenario.active_window_work[0]), 10.0)
        self.assertEqual(float(scenario.active_window_work[6]), 10.0)
        self.assertEqual(float(scenario.active_window_work[7]), 0.0)


if __name__ == "__main__":
    unittest.main()
