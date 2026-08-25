"""生成 2024 逐日风、光、碳联合预测残差及可审计清单。"""

from __future__ import annotations

import csv
import datetime as dt
import hashlib
import json
import math
from collections import Counter, defaultdict
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from zoneinfo import ZoneInfo

from .config import (
    ENERGY_RESIDUAL_YEAR,
    ENERGY_SEASONAL_CV_FOLDS,
    FORECAST_METHOD,
)
from .forecasting import (
    FORECAST_COLUMNS,
    TARGET_COLUMNS,
    forecast_delivery_dates,
    validate_ridge_selection,
)

CENTRAL = ZoneInfo("America/Chicago")
TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
OUTPUT_FILENAME = "joint_residuals_2024.csv"
MANIFEST_FILENAME = "residuals_manifest.json"
RESIDUAL_COLUMNS = tuple(column.replace("erco_", "residual_erco_") for column in TARGET_COLUMNS)
CSV_COLUMNS = (
    "delivery_date",
    "cv_fold",
    "hour_index",
    "forecast_cutoff_utc",
    "interval_end_utc",
    "forecast_method",
    *TARGET_COLUMNS,
    *FORECAST_COLUMNS,
    *RESIDUAL_COLUMNS,
    "complete_row",
    "usable_24h_block",
)
ForecastProvider = Callable[..., list[dict[str, object]]]


def dates_for_year(year: int) -> tuple[dt.date, ...]:
    """返回指定公历年的全部日期。"""

    start = dt.date(year, 1, 1)
    stop = dt.date(year + 1, 1, 1)
    return tuple(start + dt.timedelta(days=offset) for offset in range((stop - start).days))


def residual_cv_fold(delivery_date: dt.date) -> str:
    """按月份返回覆盖四季的统一交叉验证折。"""

    if delivery_date.year != ENERGY_RESIDUAL_YEAR:
        raise ValueError(
            f"delivery date must be in residual year {ENERGY_RESIDUAL_YEAR}"
        )
    for fold, months in ENERGY_SEASONAL_CV_FOLDS.items():
        if delivery_date.month in months:
            return fold
    raise ValueError(f"no seasonal CV fold registered for month {delivery_date.month}")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as input_file:
        for block in iter(lambda: input_file.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def _validate_hash(value: str) -> str:
    digest = value.upper()
    if len(digest) != 64 or any(character not in "0123456789ABCDEF" for character in digest):
        raise ValueError("source_sha256 must be a SHA-256 digest")
    return digest


def _target_delivery_date(timestamp_text: str) -> dt.date:
    endpoint = dt.datetime.strptime(timestamp_text, TIMESTAMP_FORMAT).replace(
        tzinfo=dt.timezone.utc
    )
    return (endpoint.astimezone(CENTRAL) - dt.timedelta(hours=1)).date()


def build_joint_residual_rows(
    history: Sequence[Mapping[str, object]],
    *,
    delivery_dates: Sequence[dt.date],
    alphas: Mapping[str, float],
    forecast_provider: ForecastProvider = forecast_delivery_dates,
) -> list[dict[str, object]]:
    """对齐逐日预测与实际值，并保留缺失和 DST 日的审计标记。"""

    history_by_timestamp = {str(row["timestamp_utc"]): row for row in history}
    if len(history_by_timestamp) != len(history):
        raise ValueError("history timestamp_utc values must be unique")
    forecasts = forecast_provider(
        history,
        delivery_dates=delivery_dates,
        alphas=alphas,
        skip_unavailable=True,
    )
    rows: list[dict[str, object]] = []
    hour_counts: Counter[str] = Counter()
    for forecast in forecasts:
        timestamp = str(forecast["forecast_target_end_utc"])
        actual = history_by_timestamp.get(timestamp, {})
        delivery_date = _target_delivery_date(timestamp)
        date_text = delivery_date.isoformat()
        hour_index = hour_counts[date_text]
        hour_counts[date_text] += 1
        row: dict[str, object] = {
            "delivery_date": date_text,
            "cv_fold": residual_cv_fold(delivery_date),
            "hour_index": hour_index,
            "forecast_cutoff_utc": forecast["forecast_cutoff_utc"],
            "interval_end_utc": timestamp,
            "forecast_method": forecast["forecast_method"],
        }
        complete = True
        for actual_column, forecast_column, residual_column in zip(
            TARGET_COLUMNS,
            FORECAST_COLUMNS,
            RESIDUAL_COLUMNS,
            strict=True,
        ):
            actual_value = actual.get(actual_column)
            forecast_value = forecast.get(forecast_column)
            row[actual_column] = actual_value
            row[forecast_column] = forecast_value
            if actual_value in (None, "") or forecast_value in (None, ""):
                complete = False
                row[residual_column] = None
            else:
                row[residual_column] = float(actual_value) - float(forecast_value)
        row["complete_row"] = complete
        row["usable_24h_block"] = False
        rows.append(row)

    complete_by_date: dict[str, bool] = defaultdict(lambda: True)
    for row in rows:
        complete_by_date[str(row["delivery_date"])] &= bool(row["complete_row"])
    for row in rows:
        date_text = str(row["delivery_date"])
        row["usable_24h_block"] = hour_counts[date_text] == 24 and complete_by_date[date_text]
    return rows


def _fold_metrics(rows: Sequence[Mapping[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for fold in ENERGY_SEASONAL_CV_FOLDS:
        fold_rows = [row for row in rows if row["cv_fold"] == fold]
        usable_dates = {
            str(row["delivery_date"])
            for row in fold_rows
            if bool(row["usable_24h_block"])
        }
        targets: dict[str, object] = {}
        for actual_column, residual_column in zip(
            TARGET_COLUMNS, RESIDUAL_COLUMNS, strict=True
        ):
            usable = [
                row for row in fold_rows if row[residual_column] not in (None, "")
            ]
            absolute_errors = [abs(float(row[residual_column])) for row in usable]
            magnitudes = [abs(float(row[actual_column])) for row in usable]
            mae = sum(absolute_errors) / len(absolute_errors) if absolute_errors else 0.0
            magnitude = sum(magnitudes) / len(magnitudes) if magnitudes else 0.0
            targets[actual_column] = {
                "sample_count": len(usable),
                "mae": mae,
                "nmae": mae / magnitude if magnitude > 0.0 else 0.0,
            }
        result[fold] = {
            "months": list(ENERGY_SEASONAL_CV_FOLDS[fold]),
            "row_count": len(fold_rows),
            "usable_24h_block_count": len(usable_dates),
            "targets": targets,
        }
    return result


def write_joint_residuals(
    *,
    history: Sequence[Mapping[str, object]],
    output_directory: Path,
    source_sha256: str,
    ridge_selection: Mapping[str, object] | None = None,
    delivery_dates: Sequence[dt.date] | None = None,
    forecast_provider: ForecastProvider = forecast_delivery_dates,
) -> dict[str, object]:
    """统一生成全年逐小时预测、联合残差 CSV 和清单。"""

    selection = dict(ridge_selection or validate_ridge_selection(history))
    alphas = {
        column: float(metrics["selected_alpha"])
        for column, metrics in selection["targets"].items()
    }
    dates = tuple(delivery_dates or dates_for_year(ENERGY_RESIDUAL_YEAR))
    if any(date.year != ENERGY_RESIDUAL_YEAR for date in dates):
        raise ValueError(
            f"joint residual delivery_dates must all be in {ENERGY_RESIDUAL_YEAR}"
        )
    rows = build_joint_residual_rows(
        history,
        delivery_dates=dates,
        alphas=alphas,
        forecast_provider=forecast_provider,
    )
    if not rows:
        raise ValueError("joint residual generation produced no rows")

    output_directory.mkdir(parents=True, exist_ok=True)
    output_path = output_directory / OUTPUT_FILENAME
    with output_path.open("w", encoding="utf-8", newline="") as output_file:
        writer = csv.DictWriter(output_file, fieldnames=CSV_COLUMNS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)

    counts_by_date = Counter(str(row["delivery_date"]) for row in rows)
    complete_by_date: dict[str, bool] = defaultdict(lambda: True)
    for row in rows:
        complete_by_date[str(row["delivery_date"])] &= bool(row["complete_row"])
    requested_date_texts = {date.isoformat() for date in dates}
    missing_forecast_dates = requested_date_texts.difference(counts_by_date)
    excluded_dates = [
        {
            "delivery_date": date_text,
            "hour_count": counts_by_date.get(date_text, 0),
            "reason": (
                "forecast_unavailable"
                if date_text in missing_forecast_dates
                else "dst_hour_count"
                if counts_by_date[date_text] != 24
                else "missing_actual"
            ),
        }
        for date_text in sorted(requested_date_texts)
        if date_text in missing_forecast_dates
        or counts_by_date[date_text] != 24
        or not complete_by_date[date_text]
    ]
    manifest = {
        "schema_version": 2,
        "year": ENERGY_RESIDUAL_YEAR,
        "forecast_method": FORECAST_METHOD,
        "ridge_alphas": alphas,
        "ridge_selection": selection,
        "seasonal_cv": {
            "protocol": "hold_out_one_fold_fit_on_other_two",
            "shared_by": ["SAA", "RO", "DRO"],
            "folds": {
                fold: list(months)
                for fold, months in ENERGY_SEASONAL_CV_FOLDS.items()
            },
        },
        "row_count": len(rows),
        "complete_row_count": sum(bool(row["complete_row"]) for row in rows),
        "usable_24h_block_count": len(
            {
                str(row["delivery_date"])
                for row in rows
                if bool(row["usable_24h_block"])
            }
        ),
        "excluded_dates": excluded_dates,
        "metrics_by_fold": _fold_metrics(rows),
        "source": {"eia_930_erco": _validate_hash(source_sha256)},
        "output": {OUTPUT_FILENAME: _sha256(output_path)},
    }
    manifest_path = output_directory / MANIFEST_FILENAME
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    if not all(math.isfinite(float(alpha)) for alpha in alphas.values()):
        raise ValueError("Ridge alpha values must be finite")
    return manifest
