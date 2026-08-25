from __future__ import annotations

import csv
import datetime as dt
import tempfile
import unittest
from pathlib import Path
from zoneinfo import ZoneInfo

from alibaba2018_dro.residuals import write_joint_residuals


CENTRAL = ZoneInfo("America/Chicago")
TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
ALPHAS = {
    "erco_solar_generation_mwh": 1.0,
    "erco_wind_generation_mwh": 1.0,
    "erco_consumed_co2_intensity_lbs_per_kwh": 1.0,
}


def _endpoints(delivery_date: dt.date) -> list[dt.datetime]:
    start = dt.datetime.combine(delivery_date, dt.time(), tzinfo=CENTRAL).astimezone(
        dt.timezone.utc
    )
    stop = dt.datetime.combine(
        delivery_date + dt.timedelta(days=1), dt.time(), tzinfo=CENTRAL
    ).astimezone(dt.timezone.utc)
    return [
        start + dt.timedelta(hours=offset)
        for offset in range(1, int((stop - start).total_seconds() // 3600) + 1)
    ]


def _history(dates: list[dt.date]) -> list[dict[str, object]]:
    return [
        {
            "timestamp_utc": endpoint.strftime(TIMESTAMP_FORMAT),
            "local_date": delivery_date.isoformat(),
            "erco_solar_generation_mwh": 100.0,
            "erco_wind_generation_mwh": 200.0,
            "erco_consumed_co2_intensity_lbs_per_kwh": 0.5,
        }
        for delivery_date in dates
        for endpoint in _endpoints(delivery_date)
    ]


def _forecasts(
    _history_rows: list[dict[str, object]],
    *,
    delivery_dates: list[dt.date] | tuple[dt.date, ...],
    alphas: dict[str, float],
    skip_unavailable: bool = False,
) -> list[dict[str, object]]:
    del skip_unavailable
    if dict(alphas) != ALPHAS:
        raise AssertionError("unexpected Ridge alphas")
    return [
        {
            "forecast_cutoff_utc": (
                dt.datetime.combine(
                    delivery_date - dt.timedelta(days=1),
                    dt.time(18),
                    tzinfo=CENTRAL,
                )
                .astimezone(dt.timezone.utc)
                .strftime(TIMESTAMP_FORMAT)
            ),
            "forecast_target_end_utc": endpoint.strftime(TIMESTAMP_FORMAT),
            "forecast_method": "direct_ridge_90d_v1",
            "forecast_erco_solar_generation_mwh": 90.0,
            "forecast_erco_wind_generation_mwh": 180.0,
            "forecast_consumed_co2_lbs_per_kwh": 0.4,
        }
        for delivery_date in delivery_dates
        for endpoint in _endpoints(delivery_date)
    ]


def _forecasts_with_missing_day(
    history_rows: list[dict[str, object]],
    *,
    delivery_dates: list[dt.date] | tuple[dt.date, ...],
    alphas: dict[str, float],
    skip_unavailable: bool = False,
) -> list[dict[str, object]]:
    del skip_unavailable
    return _forecasts(
        history_rows,
        delivery_dates=delivery_dates[:1],
        alphas=alphas,
    )


def _validation() -> dict[str, object]:
    return {
        "selection_year": 2023,
        "purpose": "ridge_alpha_selection",
        "origin_dates": ["2023-01-01"],
        "origin_count": 1,
        "targets": {
            column: {
                "selected_alpha": alpha,
                "sample_count": 24,
                "ridge_mae": 0.0,
                "ridge_nmae": 0.0,
                "median_mae": 0.0,
                "median_nmae": 0.0,
            }
            for column, alpha in ALPHAS.items()
        },
    }


class EnergyResidualTests(unittest.TestCase):
    def test_registers_a_forecast_unavailable_day_without_imputation(self) -> None:
        dates = [dt.date(2024, 3, 30), dt.date(2024, 3, 31)]
        with tempfile.TemporaryDirectory() as temporary_directory:
            manifest = write_joint_residuals(
                history=_history(dates),
                output_directory=Path(temporary_directory),
                source_sha256="A" * 64,
                ridge_selection=_validation(),
                delivery_dates=dates,
                forecast_provider=_forecasts_with_missing_day,
            )

        self.assertEqual(manifest["usable_24h_block_count"], 1)
        self.assertEqual(
            manifest["excluded_dates"],
            [
                {
                    "delivery_date": "2024-03-31",
                    "hour_count": 0,
                    "reason": "forecast_unavailable",
                }
            ],
        )

    def test_writes_seasonal_folds_and_excludes_dst_day_from_24h_blocks(self) -> None:
        dates = [
            dt.date(2024, 1, 2),
            dt.date(2024, 2, 2),
            dt.date(2024, 3, 2),
            dt.date(2024, 11, 3),
        ]
        with tempfile.TemporaryDirectory() as temporary_directory:
            output_directory = Path(temporary_directory)
            manifest = write_joint_residuals(
                history=_history(dates),
                output_directory=output_directory,
                source_sha256="A" * 64,
                ridge_selection=_validation(),
                delivery_dates=dates,
                forecast_provider=_forecasts,
            )
            with (output_directory / "joint_residuals_2024.csv").open(
                "r", encoding="utf-8", newline=""
            ) as input_file:
                rows = list(csv.DictReader(input_file))

        self.assertEqual(len(rows), 97)
        self.assertEqual(manifest["usable_24h_block_count"], 3)
        self.assertEqual(
            {row["cv_fold"] for row in rows},
            {"fold_1", "fold_2", "fold_3"},
        )
        self.assertEqual(
            manifest["seasonal_cv"]["folds"],
            {"fold_1": [1, 4, 7, 10], "fold_2": [2, 5, 8, 11], "fold_3": [3, 6, 9, 12]},
        )
        dst_rows = [row for row in rows if row["delivery_date"] == "2024-11-03"]
        self.assertEqual(len(dst_rows), 25)
        self.assertTrue(all(row["usable_24h_block"] == "False" for row in dst_rows))
        self.assertAlmostEqual(float(rows[0]["residual_erco_wind_generation_mwh"]), 20.0)


if __name__ == "__main__":
    unittest.main()
