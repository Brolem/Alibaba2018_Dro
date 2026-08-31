from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path

from alibaba2018_dro.residuals import CSV_COLUMNS
from alibaba2018_dro.scenarios import (
    CALIBRATION_BLOCKS_FILENAME,
    load_calibration_energy_rows,
    load_hourly_downward_residual_quantiles,
    load_saa_scenarios,
    write_calibration_day_blocks,
    write_saa_scenario_manifest,
)


def _residual_row(date_text: str, fold: str, hour: int) -> dict[str, object]:
    row: dict[str, object] = {column: "0" for column in CSV_COLUMNS}
    row.update(
        {
            "delivery_date": date_text,
            "cv_fold": fold,
            "hour_index": hour,
            "complete_row": "True",
            "usable_24h_block": "True",
            "residual_erco_solar_generation_mwh": float(hour),
            "residual_erco_wind_generation_mwh": float(-hour),
            "residual_erco_consumed_co2_intensity_lbs_per_kwh": 0.1,
        }
    )
    return row


class ScenarioManifestTests(unittest.TestCase):
    def test_downward_quantiles_exclude_held_out_fold(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            calibration_csv = Path(temporary_directory) / "calibration.csv"
            rows: list[dict[str, object]] = []
            for hour in range(24):
                training = _residual_row("2024-01-01", "fold_1", hour)
                training["residual_erco_solar_generation_mwh"] = -10.0
                training["residual_erco_wind_generation_mwh"] = -20.0
                held_out = _residual_row("2024-02-01", "fold_2", hour)
                held_out["residual_erco_solar_generation_mwh"] = -1000.0
                held_out["residual_erco_wind_generation_mwh"] = -2000.0
                rows.extend((training, held_out))
            with calibration_csv.open("w", newline="", encoding="utf-8") as output_file:
                writer = csv.DictWriter(output_file, fieldnames=CSV_COLUMNS)
                writer.writeheader()
                writer.writerows(rows)

            solar, wind = load_hourly_downward_residual_quantiles(
                calibration_csv, held_out_fold="fold_2"
            )

            self.assertEqual(solar, (10.0,) * 24)
            self.assertEqual(wind, (20.0,) * 24)

    def test_calibration_blocks_and_manifest_are_reconstructible(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            residual_csv = root / "joint_residuals_2024.csv"
            dates_and_folds = (
                ("2024-01-01", "fold_1"),
                ("2024-02-01", "fold_2"),
                ("2024-03-01", "fold_3"),
            )
            with residual_csv.open("w", encoding="utf-8", newline="") as output_file:
                writer = csv.DictWriter(output_file, fieldnames=CSV_COLUMNS)
                writer.writeheader()
                for date_text, fold in dates_and_folds:
                    writer.writerows(
                        _residual_row(date_text, fold, hour) for hour in range(24)
                    )
            price_source = root / "ercot_prices.zip"
            price_source.write_bytes(b"test archive provenance")
            price_rows = [
                {
                    "delivery_date": date_text,
                    "hour_ending": f"{hour + 1:02d}:00",
                    "dam_lz_houston_usd_per_mwh": 20.0 + hour,
                }
                for date_text, _ in dates_and_folds
                for hour in range(24)
            ]
            output_directory = root / "scenarios"
            block_manifest = write_calibration_day_blocks(
                residual_csv=residual_csv,
                dam_price_rows=price_rows,
                dam_price_source=price_source,
                output_directory=output_directory,
            )
            self.assertEqual(block_manifest["usable_day_block_count"], 3)
            self.assertEqual(
                block_manifest["usable_day_block_count_by_fold"],
                {"fold_1": 1, "fold_2": 1, "fold_3": 1},
            )
            with (output_directory / CALIBRATION_BLOCKS_FILENAME).open(
                "r", encoding="utf-8", newline=""
            ) as input_file:
                calibration_rows = list(csv.DictReader(input_file))
            self.assertEqual(len(calibration_rows), 72)
            self.assertEqual(
                float(calibration_rows[0]["dam_lz_houston_usd_per_mwh"]), 20.0
            )

            workload_csv = root / "aggregate_workload_8d.csv"
            with workload_csv.open("w", encoding="utf-8", newline="") as output_file:
                writer = csv.DictWriter(
                    output_file,
                    fieldnames=(
                        "trace_hour",
                        "day",
                        "hour_of_day",
                        "arrival_work_core_hours",
                    ),
                )
                writer.writeheader()
                for day in range(1, 9):
                    writer.writerows(
                        {
                            "trace_hour": (day - 1) * 24 + hour,
                            "day": day,
                            "hour_of_day": hour,
                            "arrival_work_core_hours": float(day),
                        }
                        for hour in range(24)
                    )
            workload_manifest = root / "nominal_workload_manifest.json"
            workload_manifest.write_text(
                json.dumps(
                    {
                        "parameters": {
                            "days": 30,
                            "block_days": 2,
                            "flex_window_hours": 6,
                            "target_total_work_core_hours": 3600.0,
                        },
                        "source": {"included_source_days_one_based": list(range(2, 9))},
                    }
                ),
                encoding="utf-8",
            )
            manifest_path = output_directory / "saa_scenarios_manifest.json"
            payload = write_saa_scenario_manifest(
                calibration_csv=output_directory / CALIBRATION_BLOCKS_FILENAME,
                workload_csv=workload_csv,
                workload_manifest=workload_manifest,
                output_path=manifest_path,
            )
            self.assertEqual(
                payload["shared_protocol"]["nested_saa_sample_sizes"],
                [20, 50, 100, 200],
            )
            fold_1_train = payload["training_by_held_out_fold"]["fold_1"]
            self.assertEqual(len(fold_1_train["scenarios"]), 200)
            self.assertTrue(
                all(
                    date != "2024-01-01"
                    for spec in fold_1_train["scenarios"]
                    for date in spec["energy_delivery_dates"]
                )
            )
            fold_1_validation = payload["validation_by_held_out_fold"]["fold_1"]
            self.assertEqual(len(fold_1_validation["pseudo_windows"]), 12)
            self.assertTrue(
                all(
                    date == "2024-01-01"
                    for spec in fold_1_validation["pseudo_windows"]
                    for date in spec["energy_delivery_dates"]
                )
            )
            self.assertEqual(len(payload["workload_replay_2025"]["scenarios"]), 100)

            scenarios = load_saa_scenarios(
                manifest_path=manifest_path,
                calibration_csv=output_directory / CALIBRATION_BLOCKS_FILENAME,
                workload_csv=workload_csv,
                scenario_count=20,
            )
            self.assertEqual([scenario.scenario_id for scenario in scenarios], list(range(20)))
            self.assertEqual(len(scenarios[0].cumulative_arrived_core_hours), 720)
            self.assertAlmostEqual(
                scenarios[0].cumulative_arrived_core_hours[-1], 3600.0
            )
            self.assertEqual(len(scenarios[0].residual_solar_mwh), 720)
            energy_rows = load_calibration_energy_rows(
                output_directory / CALIBRATION_BLOCKS_FILENAME,
                ("2024-01-01", "2024-02-01"),
            )
            self.assertEqual(len(energy_rows), 48)
            self.assertEqual(energy_rows[0]["delivery_date"], "2024-01-01")
            self.assertEqual(energy_rows[24]["delivery_date"], "2024-02-01")


if __name__ == "__main__":
    unittest.main()
