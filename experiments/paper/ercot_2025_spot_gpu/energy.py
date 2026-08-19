from __future__ import annotations

import csv
import datetime as dt
import hashlib
import json
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path
from zoneinfo import ZoneInfo

from .config import (
    COMPLETION_SLACK_HOURS,
    CONTEXT_HOURS,
    CORE_HOURS,
    ENERGY_INPUT_COLUMNS,
    FORECAST_HISTORY_DAYS,
    FORECAST_INFORMATION_PROTECTION_HOURS,
    FORECAST_METHOD,
    MAX_SPOT_DURATION_HOURS,
    PAPER_WINDOW_STARTS,
    TAIL_HOURS,
)
from .forecasting import forecast_delivery_day, validate_ridge_2024
from .types import EnergyInterval


_TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
_ACTUAL_COLUMNS = (
    "dam_lz_houston_usd_per_mwh",
    "erco_solar_generation_mwh",
    "erco_wind_generation_mwh",
    "erco_consumed_co2_intensity_lbs_per_kwh",
)
_FORECAST_COLUMNS = (
    "forecast_cutoff_utc",
    "forecast_method",
    "forecast_erco_solar_generation_mwh",
    "forecast_erco_wind_generation_mwh",
    "forecast_consumed_co2_lbs_per_kwh",
)
_CENTRAL = ZoneInfo("America/Chicago")
ForecastProvider = Callable[..., Sequence[Mapping[str, object]]]


def _timestamp(value: object, *, label: str) -> dt.datetime:
    try:
        return dt.datetime.strptime(str(value), _TIMESTAMP_FORMAT)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{label} must use {_TIMESTAMP_FORMAT}") from error


def _timestamp_text(value: dt.datetime) -> str:
    return value.strftime(_TIMESTAMP_FORMAT)


def to_energy_interval(row: Mapping[str, object]) -> EnergyInterval:
    """Convert an hourly end timestamp to its preceding one-hour interval."""

    end = _timestamp(row.get("timestamp_utc"), label="timestamp_utc")
    return EnergyInterval(
        interval_start_utc=_timestamp_text(end - dt.timedelta(hours=1)),
        interval_end_utc=_timestamp_text(end),
    )


def day_ahead_cutoff_utc(forecast_target_end_utc: str) -> str:
    """Return the pre-registered 18:00 Central cutoff before delivery day."""

    target_utc = _timestamp(
        forecast_target_end_utc, label="forecast_target_end_utc"
    ).replace(tzinfo=dt.timezone.utc)
    target_local_end = target_utc.astimezone(_CENTRAL)
    delivery_date = (target_local_end - dt.timedelta(hours=1)).date()
    cutoff_local = dt.datetime.combine(
        delivery_date - dt.timedelta(days=1),
        dt.time(18, tzinfo=_CENTRAL),
    )
    return _timestamp_text(cutoff_local.astimezone(dt.timezone.utc).replace(tzinfo=None))


def _ordered_rows(rows: Iterable[Mapping[str, object]]) -> list[Mapping[str, object]]:
    ordered = sorted(
        rows, key=lambda row: _timestamp(row.get("timestamp_utc"), label="timestamp_utc")
    )
    timestamps = [
        _timestamp(row.get("timestamp_utc"), label="timestamp_utc")
        for row in ordered
    ]
    if len(timestamps) != len(set(timestamps)):
        raise ValueError("energy timestamp_utc values must be unique")
    for previous, current in zip(timestamps, timestamps[1:], strict=False):
        if current != previous + dt.timedelta(hours=1):
            raise ValueError("energy timestamp_utc values must be consecutive")
    return ordered


def _forecast_by_target(
    forecast_rows: Iterable[Mapping[str, object]],
) -> dict[str, Mapping[str, object]]:
    selected: dict[str, Mapping[str, object]] = {}
    for row in forecast_rows:
        target = _timestamp(
            row.get("forecast_target_end_utc"), label="forecast_target_end_utc"
        )
        cutoff = _timestamp(row.get("forecast_cutoff_utc"), label="forecast_cutoff_utc")
        expected_cutoff = day_ahead_cutoff_utc(_timestamp_text(target))
        if _timestamp_text(cutoff) != expected_cutoff:
            raise ValueError(
                "forecast cutoff does not match the fixed day-ahead rule: "
                f"{_timestamp_text(cutoff)} != {expected_cutoff}"
            )
        if row.get("forecast_method") != FORECAST_METHOD:
            raise ValueError("forecast method does not match the registered causal model")
        target_text = _timestamp_text(target)
        if target_text in selected:
            raise ValueError(f"duplicate forecast target: {target_text}")
        selected[target_text] = row
    return selected


def _window_id(window_start: dt.date) -> str:
    return (
        f"{window_start.isoformat()}_30d_d{MAX_SPOT_DURATION_HOURS}"
        f"_h{COMPLETION_SLACK_HOURS}"
    )


def _output_row(
    source_row: Mapping[str, object],
    *,
    window_id: str,
    window_hour: int,
    period_role: str,
    forecast: Mapping[str, object] | None,
) -> dict[str, object]:
    interval = to_energy_interval(source_row)
    result: dict[str, object] = {
        "window_id": window_id,
        "window_hour": window_hour,
        "period_role": period_role,
        "interval_start_utc": interval.interval_start_utc,
        "interval_end_utc": interval.interval_end_utc,
        "local_date": source_row.get("local_date", ""),
    }
    for column in _ACTUAL_COLUMNS:
        result[column] = source_row.get(column, "")
    for column in _FORECAST_COLUMNS:
        result[column] = "" if forecast is None else forecast.get(column, "")
    return result


def build_study_window_rows(
    *,
    annual_2025: Sequence[Mapping[str, object]],
    december_2024: Sequence[Mapping[str, object]],
    window_start: dt.date,
    forecast_rows: Iterable[Mapping[str, object]] = (),
    require_forecast_coverage: bool = False,
) -> list[dict[str, object]]:
    """Build one context/core/tail input without shifting ERCOT timestamps."""

    annual = _ordered_rows(annual_2025)
    combined = _ordered_rows([*december_2024, *annual])
    window_end = window_start + dt.timedelta(days=30)
    core = [
        row
        for row in annual
        if window_start <= dt.date.fromisoformat(str(row["local_date"])) < window_end
    ]
    if len(core) != CORE_HOURS:
        raise ValueError(
            f"30-day core from {window_start.isoformat()} must contain "
            f"{CORE_HOURS} hours, found {len(core)}"
        )

    core_start = _timestamp(core[0].get("timestamp_utc"), label="timestamp_utc")
    core_end = _timestamp(core[-1].get("timestamp_utc"), label="timestamp_utc")
    combined_by_timestamp = {
        _timestamp(row.get("timestamp_utc"), label="timestamp_utc"): index
        for index, row in enumerate(combined)
    }
    try:
        start_index = combined_by_timestamp[core_start]
        end_index = combined_by_timestamp[core_end]
    except KeyError as error:
        raise ValueError("core timestamps are absent from the combined energy source") from error

    context = combined[start_index - CONTEXT_HOURS : start_index]
    tail = combined[end_index + 1 : end_index + 1 + TAIL_HOURS]
    if len(context) != CONTEXT_HOURS or len(tail) != TAIL_HOURS:
        raise ValueError(
            "energy source does not contain the required "
            f"{CONTEXT_HOURS}-hour context and {TAIL_HOURS}-hour settlement tail"
        )

    selected_rows = [*context, *core, *tail]
    selected_timestamps = [
        _timestamp(row.get("timestamp_utc"), label="timestamp_utc")
        for row in selected_rows
    ]
    for previous, current in zip(selected_timestamps, selected_timestamps[1:], strict=False):
        if current != previous + dt.timedelta(hours=1):
            raise ValueError("study input timestamp_utc values must be consecutive")

    selected_forecasts = _forecast_by_target(forecast_rows)
    result: list[dict[str, object]] = []
    for window_hour, source_row in enumerate(selected_rows):
        if window_hour < CONTEXT_HOURS:
            period_role = "context"
        elif window_hour < CONTEXT_HOURS + CORE_HOURS:
            period_role = "core"
        else:
            period_role = "settlement_tail"
        interval = to_energy_interval(source_row)
        forecast = None if period_role == "context" else selected_forecasts.get(interval.interval_end_utc)
        if require_forecast_coverage and period_role != "context":
            if forecast is None:
                raise ValueError(
                    "forecast coverage is missing for " f"{interval.interval_end_utc}"
                )
            for column in _FORECAST_COLUMNS:
                if forecast.get(column) in (None, ""):
                    raise ValueError(
                        "forecast value is missing for "
                        f"{interval.interval_end_utc}: {column}"
                    )
        result.append(
            _output_row(
                source_row,
                window_id=_window_id(window_start),
                window_hour=window_hour,
                period_role=period_role,
                forecast=forecast,
            )
        )
    if tuple(result[0]) != ENERGY_INPUT_COLUMNS:
        raise AssertionError("paper input rows do not match the formal schema")
    return result


def sha256_file(path: Path) -> str:
    """Return the uppercase SHA-256 digest for one materialized input."""

    digest = hashlib.sha256()
    with path.open("rb") as source_file:
        for block in iter(lambda: source_file.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def _write_csv(path: Path, rows: Sequence[Mapping[str, object]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as output_file:
        writer = csv.DictWriter(
            output_file,
            fieldnames=ENERGY_INPUT_COLUMNS,
            lineterminator="\n",
        )
        writer.writeheader()
        writer.writerows(rows)


def _validate_source_hashes(source_hashes: Mapping[str, str]) -> dict[str, str]:
    validated: dict[str, str] = {}
    for source_id, digest in source_hashes.items():
        if not source_id or "/" in source_id or "\\" in source_id:
            raise ValueError("source hashes must use stable source identifiers")
        if len(digest) != 64 or any(
            character not in "0123456789abcdefABCDEF" for character in digest
        ):
            raise ValueError(f"source hash for {source_id} must be a SHA-256 digest")
        validated[source_id] = digest.upper()
    return dict(sorted(validated.items()))


def _window_forecasts(
    *,
    rows: Sequence[Mapping[str, object]],
    eia_history: Sequence[Mapping[str, object]],
    forecast_provider: ForecastProvider,
    ridge_alphas: Mapping[str, float],
) -> list[Mapping[str, object]]:
    delivery_dates = sorted(
        {
            str(row["local_date"])
            for row in rows
            if row["period_role"] != "context"
        }
    )
    forecasts: list[Mapping[str, object]] = []
    for delivery_date in delivery_dates:
        representative = next(
            row
            for row in rows
            if row["period_role"] != "context"
            and row["local_date"] == delivery_date
        )
        forecasts.extend(
            forecast_provider(
                eia_history,
                cutoff_utc=day_ahead_cutoff_utc(str(representative["interval_end_utc"])),
                delivery_date=delivery_date,
                alphas=ridge_alphas,
            )
        )
    return forecasts


def write_study_inputs(
    *,
    annual_2025: Sequence[Mapping[str, object]],
    december_2024: Sequence[Mapping[str, object]],
    eia_history: Sequence[Mapping[str, object]],
    output_directory: Path,
    source_hashes: Mapping[str, str],
    forecast_provider: ForecastProvider = forecast_delivery_day,
) -> dict[str, object]:
    """Write four fixed inputs with causal forecasts for core and tail only."""

    validation = validate_ridge_2024(eia_history)
    ridge_alphas = {
        column: float(metrics["selected_alpha"])
        for column, metrics in validation["targets"].items()
    }
    materialized_rows: dict[str, list[dict[str, object]]] = {}
    for window_start in PAPER_WINDOW_STARTS:
        filename = (
            f"{window_start.isoformat()}_30d_d{MAX_SPOT_DURATION_HOURS}"
            f"_h{COMPLETION_SLACK_HOURS}_energy.csv"
        )
        unforecasted_rows = build_study_window_rows(
            annual_2025=annual_2025,
            december_2024=december_2024,
            window_start=window_start,
        )
        materialized_rows[filename] = build_study_window_rows(
            annual_2025=annual_2025,
            december_2024=december_2024,
            window_start=window_start,
            forecast_rows=_window_forecasts(
                rows=unforecasted_rows,
                eia_history=eia_history,
                forecast_provider=forecast_provider,
                ridge_alphas=ridge_alphas,
            ),
            require_forecast_coverage=True,
        )

    output_directory.mkdir(parents=True, exist_ok=True)
    outputs: dict[str, str] = {}
    missing_value_counts = {column: 0 for column in ENERGY_INPUT_COLUMNS[6:]}
    predicted_row_count = 0
    for filename, rows in materialized_rows.items():
        output_path = output_directory / filename
        _write_csv(output_path, rows)
        outputs[filename] = sha256_file(output_path)
        for row in rows:
            if row["period_role"] != "context":
                predicted_row_count += 1
            for column in missing_value_counts:
                missing_value_counts[column] += row[column] in (None, "")

    manifest = {
        "schema_version": 2,
        "window_starts": [window_start.isoformat() for window_start in PAPER_WINDOW_STARTS],
        "window_hours": {
            "context": CONTEXT_HOURS,
            "core": CORE_HOURS,
            "settlement_tail": TAIL_HOURS,
        },
        "max_spot_duration_hours": MAX_SPOT_DURATION_HOURS,
        "completion_slack_hours": COMPLETION_SLACK_HOURS,
        "forecast": {
            "method": FORECAST_METHOD,
            "history_days": FORECAST_HISTORY_DAYS,
            "information_protection_hours": FORECAST_INFORMATION_PROTECTION_HOURS,
            "baseline_method": "same_hour_median_28d_v1",
            "predicted_row_count": predicted_row_count,
            "validation_year": validation["validation_year"],
            "ridge_alphas": ridge_alphas,
            "validation": validation,
        },
        "sources": _validate_source_hashes(source_hashes),
        "outputs": dict(sorted(outputs.items())),
        "missing_value_counts": missing_value_counts,
    }
    manifest_path = output_directory / "inputs_manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return manifest
