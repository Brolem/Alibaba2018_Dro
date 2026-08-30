from __future__ import annotations

import unittest

from scripts.run_uncertainty_methods import (
    HELD_OUT_FOLDS,
    select_saa_sample_size,
    summarize_saa_runs,
    wilson_upper_bound,
)


def _run_row(
    sample_size: int,
    fold: str,
    window: int,
    *,
    workload_violation: bool = False,
    nominal_cost: float = 100.0,
) -> dict[str, str]:
    return {
        "sample_size": str(sample_size),
        "held_out_fold": fold,
        "validation_window_id": str(window),
        "feasible": "True",
        "solver_runtime_seconds": "1.0",
        "nominal_operating_cost_usd": str(nominal_cost),
        "actual_operating_cost_usd": str(nominal_cost + 1.0),
        "workload_violation": str(workload_violation),
        "carbon_violation": "False",
        "grid_limit_violation": "False",
        "ramp_violation": "False",
    }


class SaaCalibrationTests(unittest.TestCase):
    def test_one_sided_wilson_gate_distinguishes_zero_and_one_of_36(self) -> None:
        self.assertLess(wilson_upper_bound(0, 36), 0.10)
        self.assertGreater(wilson_upper_bound(1, 36), 0.10)

    def test_adaptive_selection_uses_smallest_reliable_sample_size(self) -> None:
        rows: list[dict[str, str]] = []
        for sample_size, cost in ((20, 150.0), (50, 100.0), (100, 101.0), (200, 102.0)):
            for fold in HELD_OUT_FOLDS:
                for window in range(12):
                    rows.append(
                        _run_row(sample_size, fold, window, nominal_cost=cost)
                    )
        summaries = summarize_saa_runs(rows)
        selection = select_saa_sample_size(summaries)

        self.assertEqual(len(summaries), 4)
        self.assertTrue(all(item["meets_90pct_target"] for item in summaries))
        self.assertEqual(selection["selected_sample_size"], 20)
        self.assertTrue(selection["target_achieved"])
        self.assertIn("smallest tested N", selection["selection_rule"])

    def test_failed_reliability_gate_uses_minimum_maximum_upper_bound(self) -> None:
        rows: list[dict[str, str]] = []
        for sample_size in (20, 50, 100, 200):
            for fold in HELD_OUT_FOLDS:
                for window in range(12):
                    rows.append(
                        _run_row(
                            sample_size,
                            fold,
                            window,
                            workload_violation=window < (2 if sample_size == 20 else 1),
                            nominal_cost=float(sample_size),
                        )
                    )
        summaries = summarize_saa_runs(rows)
        selection = select_saa_sample_size(summaries)

        self.assertFalse(selection["target_achieved"])
        self.assertEqual(selection["selected_sample_size"], 50)


if __name__ == "__main__":
    unittest.main()
