from __future__ import annotations

import datetime as dt
import csv
import json
import math
import subprocess
import sys
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from typing import Callable
from unittest.mock import patch
from zoneinfo import ZoneInfo

try:
    from experiments.paper.ercot_2025_spot_gpu.energy import (
        build_study_window_rows,
        day_ahead_cutoff_utc,
        to_energy_interval,
    )
except ImportError:
    build_study_window_rows = None
    day_ahead_cutoff_utc = None
    to_energy_interval = None

try:
    from experiments.paper.ercot_2025_spot_gpu.energy import write_study_inputs
except ImportError:
    write_study_inputs = None

try:
    from experiments.paper.ercot_2025_spot_gpu.forecasting import (
        forecast_delivery_day,
        validate_ridge_2024,
    )
except ImportError:
    forecast_delivery_day = None
    validate_ridge_2024 = None

try:
    from experiments.paper.ercot_2025_spot_gpu.eia_history import (
        build_december_context,
        load_erco_history,
        load_houston_dam_prices,
    )
except ImportError:
    build_december_context = None
    load_erco_history = None
    load_houston_dam_prices = None


CENTRAL = ZoneInfo("America/Chicago")
_TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def _energy_row(timestamp: dt.datetime) -> dict[str, object]:
    local_end = timestamp.replace(tzinfo=dt.timezone.utc).astimezone(CENTRAL)
    local_interval_start = local_end - dt.timedelta(hours=1)
    return {
        "timestamp_utc": timestamp.strftime(_TIMESTAMP_FORMAT),
        "local_date": local_interval_start.date().isoformat(),
        "dam_lz_houston_usd_per_mwh": 30.0,
        "erco_solar_generation_mwh": 100.0,
        "erco_wind_generation_mwh": 200.0,
        "erco_consumed_co2_intensity_lbs_per_kwh": 0.5,
    }


def _hourly_rows(start: dt.datetime, hours: int) -> list[dict[str, object]]:
    return [_energy_row(start + dt.timedelta(hours=offset)) for offset in range(hours)]


def synthetic_annual_rows() -> list[dict[str, object]]:
    return _hourly_rows(dt.datetime(2025, 1, 1, 7), 8_760)


def synthetic_december_rows() -> list[dict[str, object]]:
    return _hourly_rows(dt.datetime(2024, 12, 1, 7), 744)


def synthetic_history_rows() -> list[dict[str, object]]:
    start = dt.datetime(2024, 8, 1, 0)
    hours = int((dt.datetime(2025, 4, 2, 0) - start).total_seconds() // 3_600)
    rows: list[dict[str, object]] = []
    for offset in range(hours):
        timestamp = start + dt.timedelta(hours=offset)
        local_end = timestamp.replace(tzinfo=dt.timezone.utc).astimezone(CENTRAL)
        local_start = local_end - dt.timedelta(hours=1)
        hour = local_start.hour
        solar = (
            max(0.0, 600.0 * math.sin(math.pi * (hour - 6) / 14.0))
            if 6 <= hour < 20
            else 0.0
        )
        rows.append(
            {
                "timestamp_utc": timestamp.strftime(_TIMESTAMP_FORMAT),
                "local_date": local_start.date().isoformat(),
                "erco_solar_generation_mwh": solar,
                "erco_wind_generation_mwh": 1_500.0 + (offset % 17),
                "erco_consumed_co2_intensity_lbs_per_kwh": 0.4
                + 0.02 * ((offset // 24) % 5),
            }
        )
    return rows


def synthetic_causal_forecaster(
    _history: list[dict[str, object]],
    *,
    cutoff_utc: str,
    delivery_date: str,
    alphas: dict[str, float],
) -> list[dict[str, object]]:
    self_contained_alphas = dict(alphas)
    if not self_contained_alphas:
        raise ValueError("synthetic forecast requires validated Ridge alphas")
    start = dt.datetime.combine(
        dt.date.fromisoformat(delivery_date), dt.time(), tzinfo=CENTRAL
    ).astimezone(dt.timezone.utc)
    stop = dt.datetime.combine(
        dt.date.fromisoformat(delivery_date) + dt.timedelta(days=1),
        dt.time(),
        tzinfo=CENTRAL,
    ).astimezone(dt.timezone.utc)
    return [
        {
            "forecast_cutoff_utc": cutoff_utc,
            "forecast_target_end_utc": (start + dt.timedelta(hours=offset)).strftime(
                _TIMESTAMP_FORMAT
            ),
            "forecast_method": "direct_ridge_90d_v1",
            "forecast_erco_solar_generation_mwh": 100.0,
            "forecast_erco_wind_generation_mwh": 500.0,
            "forecast_consumed_co2_lbs_per_kwh": 0.5,
        }
        for offset in range(1, int((stop - start).total_seconds() // 3_600) + 1)
    ]


def _require_implementation(
    implementation: Callable[..., object] | None,
) -> Callable[..., object]:
    if implementation is None:
        raise AssertionError("paper input implementation is unavailable")
    return implementation


class PaperInputTests(unittest.TestCase):
    def test_loads_only_requested_year_houston_dam_prices(self) -> None:
        loader = _require_implementation(load_houston_dam_prices)
        source_rows = [
            {
                "Delivery Date": "12/01/2024",
                "Hour Ending": "01:00",
                "Repeated Hour Flag": "N",
                "Settlement Point": "LZ_HOUSTON",
                "Settlement Point Price": 32.5,
            },
            {
                "Delivery Date": "12/01/2024",
                "Hour Ending": "01:00",
                "Repeated Hour Flag": "N",
                "Settlement Point": "HB_HOUSTON",
                "Settlement Point Price": 31.0,
            },
        ]
        with (
            patch(
                "experiments.paper.ercot_2025_spot_gpu.eia_history._xlsx_payload_from_archive",
                return_value=BytesIO(b"xlsx"),
            ),
            patch(
                "experiments.paper.ercot_2025_spot_gpu.eia_history.iter_xlsx_rows",
                side_effect=[iter(())] * 11 + [iter(source_rows)],
            ),
        ):
            rows = loader(Path("unused.zip"), year=2024)

        self.assertEqual(
            rows,
            [
                {
                    "delivery_date": "2024-12-01",
                    "hour_ending": "01:00",
                    "repeated_hour_flag": "N",
                    "dam_lz_houston_usd_per_mwh": 32.5,
                }
            ],
        )

    def test_builds_complete_december_context_from_official_price_and_eia_rows(
        self,
    ) -> None:
        builder = _require_implementation(build_december_context)
        eia_rows = synthetic_december_rows()
        price_rows = [
            {
                "delivery_date": row["local_date"],
                "hour_ending": f"{index % 24 + 1:02d}:00",
                "repeated_hour_flag": "N",
                "dam_lz_houston_usd_per_mwh": 20.0 + index / 100.0,
            }
            for index, row in enumerate(eia_rows)
        ]

        rows = builder(price_rows, eia_rows, year=2024)

        self.assertEqual(len(rows), 744)
        self.assertEqual(rows[0]["timestamp_utc"], eia_rows[0]["timestamp_utc"])
        self.assertEqual(rows[0]["dam_lz_houston_usd_per_mwh"], 20.0)
        self.assertEqual(
            rows[-1]["erco_consumed_co2_intensity_lbs_per_kwh"],
            eia_rows[-1]["erco_consumed_co2_intensity_lbs_per_kwh"],
        )

    def test_eia_history_loader_keeps_all_erco_years_and_blank_values(self) -> None:
        loader = _require_implementation(load_erco_history)
        source_rows = [
            {
                "BA": "ERCO",
                "UTC time": 45658.2916666667,
                "Local date": 45658.0,
                "NG: SUN": 1.0,
                "NG: WND": "",
                "CO2 Emissions Intensity for Consumed Electricity": 0.7,
            },
            {
                "BA": "CISO",
                "UTC time": 45658.2916666667,
                "Local date": 45658.0,
                "NG: SUN": 2.0,
                "NG: WND": 3.0,
                "CO2 Emissions Intensity for Consumed Electricity": 0.6,
            },
        ]
        with patch(
            "experiments.paper.ercot_2025_spot_gpu.eia_history.iter_xlsx_rows",
            return_value=iter(source_rows),
        ):
            rows = loader(Path("unused.xlsx"))

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["timestamp_utc"], "2025-01-01T07:00:00Z")
        self.assertEqual(rows[0]["local_date"], "2025-01-01")
        self.assertEqual(rows[0]["erco_solar_generation_mwh"], 1.0)
        self.assertIsNone(rows[0]["erco_wind_generation_mwh"])
        self.assertEqual(rows[0]["erco_consumed_co2_intensity_lbs_per_kwh"], 0.7)

    def test_48_hour_protection_excludes_newer_observations(self) -> None:
        forecaster = _require_implementation(forecast_delivery_day)
        cutoff = "2025-04-01T00:00:00Z"
        history = synthetic_history_rows()
        original = forecaster(
            history,
            cutoff_utc=cutoff,
            delivery_date="2025-04-01",
        )
        protected_timestamp = "2025-03-30T01:00:00Z"
        modified = [dict(row) for row in history]
        next(
            row
            for row in modified
            if row["timestamp_utc"] == protected_timestamp
        )["erco_wind_generation_mwh"] = 999_999.0

        protected = forecaster(
            modified,
            cutoff_utc=cutoff,
            delivery_date="2025-04-01",
        )

        self.assertEqual(original, protected)

    def test_direct_predictions_are_nonnegative_and_night_solar_is_zero(self) -> None:
        forecaster = _require_implementation(forecast_delivery_day)

        forecasts = forecaster(
            synthetic_history_rows(),
            cutoff_utc="2025-04-01T00:00:00Z",
            delivery_date="2025-04-01",
        )

        self.assertEqual(len(forecasts), 24)
        self.assertTrue(
            all(
                row["forecast_erco_wind_generation_mwh"] >= 0.0
                and row["forecast_erco_solar_generation_mwh"] >= 0.0
                and row["forecast_consumed_co2_lbs_per_kwh"] >= 0.0
                for row in forecasts
            )
        )
        for row in forecasts:
            target_end = dt.datetime.strptime(
                str(row["forecast_target_end_utc"]), _TIMESTAMP_FORMAT
            ).replace(tzinfo=dt.timezone.utc)
            local_start_hour = (target_end.astimezone(CENTRAL) - dt.timedelta(hours=1)).hour
            if local_start_hour < 6 or local_start_hour >= 20:
                self.assertEqual(row["forecast_erco_solar_generation_mwh"], 0.0)

    def test_ridge_validation_uses_only_2024_and_reports_baseline_metrics(self) -> None:
        validator = _require_implementation(validate_ridge_2024)
        history = synthetic_history_rows()

        original = validator(history)
        modified = [dict(row) for row in history]
        for row in modified:
            if str(row["local_date"]).startswith("2025-"):
                row["erco_wind_generation_mwh"] = 999_999.0
        protected = validator(modified)

        self.assertEqual(original, protected)
        self.assertEqual(original["validation_year"], 2024)
        self.assertGreater(original["origin_count"], 0)
        for metrics in original["targets"].values():
            self.assertIn(metrics["selected_alpha"], (0.01, 0.1, 1.0, 10.0, 100.0))
            self.assertGreater(metrics["sample_count"], 0)
            self.assertGreaterEqual(metrics["ridge_mae"], 0.0)
            self.assertGreaterEqual(metrics["median_mae"], 0.0)

    def test_shared_annual_table_backfills_only_the_missing_december_renewables(
        self,
    ) -> None:
        project_root = Path(__file__).parents[3]
        annual_table = project_root / "data" / "energy" / "ercot_2025_houston_hourly.csv"
        with annual_table.open("r", encoding="utf-8", newline="") as input_file:
            rows = list(csv.DictReader(input_file))

        for column in (
            "erco_solar_generation_mwh",
            "erco_wind_generation_mwh",
        ):
            self.assertEqual(
                sum(not row[column] for row in rows),
                0,
                f"{column} must be complete after the ERCOT Fuel Mix backfill",
            )
        self.assertEqual(
            sum(
                not row["erco_consumed_co2_intensity_lbs_per_kwh"]
                for row in rows
            ),
            72,
        )
        first_backfilled_row = next(
            row for row in rows if row["timestamp_utc"] == "2025-12-04T07:00:00Z"
        )
        self.assertEqual(first_backfilled_row["erco_solar_generation_mwh"], "0.096472")
        self.assertEqual(first_backfilled_row["erco_wind_generation_mwh"], "21280.363466")

    def test_first_winter_hour_uses_the_preceding_18_central_cutoff(self) -> None:
        cutoff_builder = _require_implementation(day_ahead_cutoff_utc)

        cutoff = cutoff_builder("2025-01-01T07:00:00Z")

        self.assertEqual(cutoff, "2025-01-01T00:00:00Z")

    def test_end_timestamp_defines_the_preceding_one_hour_interval(self) -> None:
        interval_builder = _require_implementation(to_energy_interval)

        interval = interval_builder({"timestamp_utc": "2025-01-01T07:00:00Z"})

        self.assertEqual(interval.interval_start_utc, "2025-01-01T06:00:00Z")
        self.assertEqual(interval.interval_end_utc, "2025-01-01T07:00:00Z")

    def test_winter_window_has_exact_context_core_and_tail_sizes(self) -> None:
        window_builder = _require_implementation(build_study_window_rows)

        rows = window_builder(
            annual_2025=synthetic_annual_rows(),
            december_2024=synthetic_december_rows(),
            window_start=dt.date(2025, 1, 1),
        )

        self.assertEqual(len(rows), 1_062)
        self.assertEqual(
            sum(row["period_role"] == "context" for row in rows), 171
        )
        self.assertEqual(sum(row["period_role"] == "core" for row in rows), 720)
        self.assertEqual(
            sum(row["period_role"] == "settlement_tail" for row in rows), 171
        )

    def test_materializes_four_hashed_paper_inputs(self) -> None:
        writer = _require_implementation(write_study_inputs)
        with tempfile.TemporaryDirectory() as temporary_directory:
            output_directory = Path(temporary_directory)
            manifest = writer(
                annual_2025=synthetic_annual_rows(),
                december_2024=synthetic_december_rows(),
                eia_history=synthetic_history_rows(),
                output_directory=output_directory,
                source_hashes={"eia_930_erco": "A" * 64},
                forecast_provider=synthetic_causal_forecaster,
            )

            expected_files = {
                "2025-01-01_30d_d168_h3_energy.csv",
                "2025-04-01_30d_d168_h3_energy.csv",
                "2025-07-01_30d_d168_h3_energy.csv",
                "2025-10-01_30d_d168_h3_energy.csv",
            }
            self.assertEqual(set(manifest["outputs"]), expected_files)
            self.assertEqual(
                set(path.name for path in output_directory.glob("*.csv")),
                expected_files,
            )
            self.assertEqual(
                next(output_directory.glob("*.csv")).read_text(encoding="utf-8").splitlines()[0],
                "window_id,window_hour,period_role,interval_start_utc,interval_end_utc,local_date,dam_lz_houston_usd_per_mwh,erco_solar_generation_mwh,erco_wind_generation_mwh,erco_consumed_co2_intensity_lbs_per_kwh,forecast_cutoff_utc,forecast_method,forecast_erco_solar_generation_mwh,forecast_erco_wind_generation_mwh,forecast_consumed_co2_lbs_per_kwh",
            )
            first_window_path = (
                output_directory / "2025-01-01_30d_d168_h3_energy.csv"
            )
            with first_window_path.open(
                "r", encoding="utf-8", newline=""
            ) as input_file:
                first_window_rows = list(csv.DictReader(input_file))
            self.assertEqual(first_window_rows[0]["forecast_method"], "")
            self.assertEqual(first_window_rows[171]["forecast_method"], "direct_ridge_90d_v1")
            manifest_payload = json.loads(
                (output_directory / "inputs_manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(manifest_payload["sources"], manifest["sources"])
            self.assertEqual(manifest_payload["forecast"]["method"], "direct_ridge_90d_v1")
            self.assertEqual(manifest_payload["forecast"]["predicted_row_count"], 4 * 891)
            self.assertEqual(manifest_payload["forecast"]["validation_year"], 2024)
            self.assertEqual(
                set(manifest_payload["forecast"]["ridge_alphas"]),
                {
                    "erco_solar_generation_mwh",
                    "erco_wind_generation_mwh",
                    "erco_consumed_co2_intensity_lbs_per_kwh",
                },
            )
            self.assertIn(
                "median_mae",
                manifest_payload["forecast"]["validation"]["targets"]
                ["erco_wind_generation_mwh"],
            )
            self.assertNotIn(str(output_directory), json.dumps(manifest_payload))

    def test_preparation_script_exposes_explicit_raw_source_arguments(self) -> None:
        project_root = Path(__file__).parents[3]
        completed = subprocess.run(
            [
                sys.executable,
                str(project_root / "scripts" / "prepare_paper_ercot_2025_spot_gpu_inputs.py"),
                "--help",
            ],
            check=False,
            capture_output=True,
            encoding="utf-8",
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("--source", completed.stdout)
        self.assertIn("--eia-history", completed.stdout)
        self.assertIn("--ercot-2024-dam", completed.stdout)
        self.assertNotIn("--december-2024", completed.stdout)


if __name__ == "__main__":
    unittest.main()
