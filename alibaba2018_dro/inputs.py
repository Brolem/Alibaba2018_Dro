"""Assemble a unified hourly scheduler input from energy windows and workload data."""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
TRACE_CORE_HOURS = 8 * 24


@dataclass(frozen=True)
class HourlyInput:
    """One aligned scheduler hour across energy and workload sources."""

    hour: int
    timestamp_utc: str
    dam_lz_houston_usd_per_mwh: float
    forecast_erco_solar_generation_mwh: float
    forecast_erco_wind_generation_mwh: float
    forecast_consumed_co2_lbs_per_kwh: float
    online_cores: float
    batch_baseline_cores: float
    batch_baseline_energy_core_hours: float
    batch_flexible_window_energy_core_hours: float


def _energy_core_rows(window_csv: Path, core_hours: int) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    with window_csv.open("r", encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            if row["period_role"] == "core":
                rows.append(row)
    if len(rows) < core_hours:
        raise ValueError(
            f"{window_csv.name} has {len(rows)} core hours, need {core_hours}"
        )
    return rows[:core_hours]


def _read_envelope(envelope_csv: Path) -> list[dict[str, str]]:
    with envelope_csv.open("r", encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def _online_cores(stats_json: Path) -> float:
    payload = json.loads(stats_json.read_text(encoding="utf-8"))
    return float(payload["online_static"]["online_reserved_cores"])


def build_hourly_input(
    window_csv: Path,
    envelope_csv: Path,
    stats_json: Path,
    *,
    core_days: int = 30,
) -> list[HourlyInput]:
    """Align one energy window's core days with the workload envelope.

    The 8-day trace envelope is rolled forward to fill the requested core
    horizon; this is a scenario extension, not additional measured workload.
    """

    core_hours = core_days * 24
    energy = _energy_core_rows(window_csv, core_hours)
    envelope = _read_envelope(envelope_csv)
    online_cores = _online_cores(stats_json)

    aligned: list[HourlyInput] = []
    for hour, energy_row in enumerate(energy):
        workload_row = envelope[hour % TRACE_CORE_HOURS]
        aligned.append(
            HourlyInput(
                hour=hour,
                timestamp_utc=energy_row["interval_end_utc"],
                dam_lz_houston_usd_per_mwh=float(
                    energy_row["dam_lz_houston_usd_per_mwh"] or 0.0
                ),
                forecast_erco_solar_generation_mwh=float(
                    energy_row["forecast_erco_solar_generation_mwh"] or 0.0
                ),
                forecast_erco_wind_generation_mwh=float(
                    energy_row["forecast_erco_wind_generation_mwh"] or 0.0
                ),
                forecast_consumed_co2_lbs_per_kwh=float(
                    energy_row["forecast_consumed_co2_lbs_per_kwh"] or 0.0
                ),
                online_cores=online_cores,
                batch_baseline_cores=float(workload_row["baseline_cores"] or 0.0),
                batch_baseline_energy_core_hours=float(
                    workload_row["baseline_energy_core_hours"] or 0.0
                ),
                batch_flexible_window_energy_core_hours=float(
                    workload_row["flexible_window_energy_core_hours"] or 0.0
                ),
            )
        )
    return aligned
