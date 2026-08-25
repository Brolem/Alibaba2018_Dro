"""无泄漏、由研究者自行构造的 ERCO 信号预测器。"""

from __future__ import annotations

import datetime as dt
import math
from bisect import bisect_right
from collections.abc import Mapping, Sequence
from functools import lru_cache
from zoneinfo import ZoneInfo

import numpy as np

from .config import (
    FORECAST_BASELINE_DAYS,
    FORECAST_HISTORY_DAYS,
    FORECAST_INFORMATION_PROTECTION_HOURS,
    FORECAST_METHOD,
    RIDGE_SELECTION_END,
    RIDGE_SELECTION_START,
    RIDGE_SELECTION_YEAR,
    RIDGE_ALPHAS,
)

TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
CENTRAL = ZoneInfo("America/Chicago")
TARGET_COLUMNS = (
    "erco_solar_generation_mwh",
    "erco_wind_generation_mwh",
    "erco_consumed_co2_intensity_lbs_per_kwh",
)
FORECAST_COLUMNS = (
    "forecast_erco_solar_generation_mwh",
    "forecast_erco_wind_generation_mwh",
    "forecast_consumed_co2_lbs_per_kwh",
)
TARGET_TO_FORECAST = dict(zip(TARGET_COLUMNS, FORECAST_COLUMNS, strict=True))
HourIndex = dict[tuple[str, int], tuple[list[dt.datetime], list[float]]]


class ForecastUnavailableError(ValueError):
    """表示源数据不足以为某一交割日生成完整预测。"""


def _timestamp(value: object, *, label: str) -> dt.datetime:
    """解析标准 UTC 时间戳。"""
    try:
        return dt.datetime.strptime(str(value), TIMESTAMP_FORMAT).replace(
            tzinfo=dt.timezone.utc
        )
    except (TypeError, ValueError) as error:
        raise ValueError(f"{label} must use {TIMESTAMP_FORMAT}") from error


def _timestamp_text(value: dt.datetime) -> str:
    """把 datetime 格式化为 UTC 时间戳文本。"""
    return value.astimezone(dt.timezone.utc).strftime(TIMESTAMP_FORMAT)


def _number(value: object, *, label: str) -> float:
    """把值解析为有限数值。"""
    try:
        number = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{label} must be numeric") from error
    if not math.isfinite(number):
        raise ValueError(f"{label} must be finite")
    return number


def _history_by_timestamp(
    history: Sequence[Mapping[str, object]],
) -> dict[dt.datetime, dict[str, float]]:
    """把历史序列按时间戳索引，并校验唯一性。"""
    result: dict[dt.datetime, dict[str, float]] = {}
    for row in history:
        timestamp = _timestamp(row.get("timestamp_utc"), label="timestamp_utc")
        if timestamp in result:
            raise ValueError("history timestamp_utc values must be unique")
        values: dict[str, float] = {}
        for column in TARGET_COLUMNS:
            value = row.get(column)
            if value not in (None, ""):
                values[column] = _number(value, label=column)
        result[timestamp] = values
    if not result:
        raise ValueError("forecast history is empty")
    return result


@lru_cache(maxsize=None)
def _interval_endpoints(delivery_date: dt.date) -> tuple[dt.datetime, ...]:
    """返回某个交割日所有小时区间的结束 UTC 时刻。"""
    local_start = dt.datetime.combine(delivery_date, dt.time(), tzinfo=CENTRAL)
    local_stop = dt.datetime.combine(
        delivery_date + dt.timedelta(days=1), dt.time(), tzinfo=CENTRAL
    )
    start_utc = local_start.astimezone(dt.timezone.utc)
    stop_utc = local_stop.astimezone(dt.timezone.utc)
    hours = int((stop_utc - start_utc).total_seconds() // 3_600)
    return tuple(
        start_utc + dt.timedelta(hours=index) for index in range(1, hours + 1)
    )


@lru_cache(maxsize=None)
def _target_for_hour(delivery_date: dt.date, local_start_hour: int) -> dt.datetime | None:
    """返回某个当地起始小时对应的目标结束时刻。"""
    for endpoint in _interval_endpoints(delivery_date):
        if (endpoint.astimezone(CENTRAL) - dt.timedelta(hours=1)).hour == local_start_hour:
            return endpoint
    return None


@lru_cache(maxsize=None)
def _cutoff_for_delivery_date(delivery_date: dt.date) -> dt.datetime:
    """返回某个交割日对应的日前截止时刻（前一天 18:00 Central）。"""
    cutoff_local = dt.datetime.combine(
        delivery_date - dt.timedelta(days=1),
        dt.time(18),
        tzinfo=CENTRAL,
    )
    return cutoff_local.astimezone(dt.timezone.utc)


def _same_hour_observations(
    hour_index: HourIndex,
    *,
    known_end: dt.datetime,
    local_start_hour: int,
    column: str,
) -> list[float]:
    """返回截止时刻之前、指定当地起始小时的同小时历史观测。"""
    timestamps, values = hour_index.get((column, local_start_hour), ([], []))
    return values[:bisect_right(timestamps, known_end)]


def _build_hour_index(
    values_by_timestamp: Mapping[dt.datetime, Mapping[str, float]],
) -> HourIndex:
    """按 (信号列, 当地起始小时) 建索引，便于快速取同小时历史。"""
    index: HourIndex = {}
    for timestamp in sorted(values_by_timestamp):
        local_start_hour = (timestamp.astimezone(CENTRAL) - dt.timedelta(hours=1)).hour
        for column, value in values_by_timestamp[timestamp].items():
            timestamps, values = index.setdefault((column, local_start_hour), ([], []))
            timestamps.append(timestamp)
            values.append(value)
    return index


def _feature_vector(
    values_by_timestamp: Mapping[dt.datetime, Mapping[str, float]],
    hour_index: HourIndex,
    *,
    known_end: dt.datetime,
    target_end: dt.datetime,
    column: str,
) -> np.ndarray | None:
    """为某个目标小时构造特征向量；历史不足时返回 None。"""
    recent: list[float] = []
    for offset in range(23, -1, -1):
        value = values_by_timestamp.get(known_end - dt.timedelta(hours=offset), {}).get(
            column
        )
        if value is None:
            return None
        recent.append(value)

    local_start = target_end.astimezone(CENTRAL) - dt.timedelta(hours=1)
    same_hour = _same_hour_observations(
        hour_index,
        known_end=known_end,
        local_start_hour=local_start.hour,
        column=column,
    )
    if len(same_hour) < FORECAST_BASELINE_DAYS:
        return None
    day_of_year = local_start.timetuple().tm_yday
    return np.asarray(
        [
            *recent,
            same_hour[-1],
            same_hour[-7],
            float(np.mean(same_hour[-7:])),
            float(np.mean(same_hour[-FORECAST_BASELINE_DAYS:])),
            float(local_start.weekday()),
            math.sin(2.0 * math.pi * day_of_year / 365.25),
            math.cos(2.0 * math.pi * day_of_year / 365.25),
        ],
        dtype=float,
    )


def _training_data(
    values_by_timestamp: Mapping[dt.datetime, Mapping[str, float]],
    hour_index: HourIndex,
    *,
    current_known_end: dt.datetime,
    delivery_date: dt.date,
    target_end: dt.datetime,
    column: str,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """构造训练特征、训练目标与待预测特征。"""
    local_start_hour = (target_end.astimezone(CENTRAL) - dt.timedelta(hours=1)).hour
    rows: list[np.ndarray] = []
    targets: list[float] = []
    candidate_date = delivery_date - dt.timedelta(days=1)
    earliest_timestamp = min(values_by_timestamp)
    while len(rows) < FORECAST_HISTORY_DAYS:
        candidate_target = _target_for_hour(candidate_date, local_start_hour)
        if candidate_target is not None and candidate_target < earliest_timestamp:
            break
        if candidate_target is not None and candidate_target <= current_known_end:
            target_value = values_by_timestamp.get(candidate_target, {}).get(column)
            pseudo_known_end = _cutoff_for_delivery_date(
                candidate_date
            ) - dt.timedelta(hours=FORECAST_INFORMATION_PROTECTION_HOURS)
            if target_value is not None:
                feature = _feature_vector(
                    values_by_timestamp,
                    hour_index,
                    known_end=pseudo_known_end,
                    target_end=candidate_target,
                    column=column,
                )
                if feature is not None:
                    rows.append(feature)
                    targets.append(target_value)
        candidate_date -= dt.timedelta(days=1)
    if len(rows) < FORECAST_BASELINE_DAYS:
        raise ForecastUnavailableError(
            f"insufficient lookback history for {column}: {len(rows)} daily samples"
        )
    prediction_feature = _feature_vector(
        values_by_timestamp,
        hour_index,
        known_end=current_known_end,
        target_end=target_end,
        column=column,
    )
    if prediction_feature is None:
        raise ForecastUnavailableError(
            f"insufficient prediction features for {column}"
        )
    return np.vstack(rows), np.asarray(targets, dtype=float), prediction_feature


def _ridge_prediction(
    features: np.ndarray,
    targets: np.ndarray,
    prediction_feature: np.ndarray,
    *,
    alpha: float,
) -> float:
    """用带截距的标准 Ridge 回归做一次点预测。"""
    mean = features.mean(axis=0)
    scale = features.std(axis=0)
    scale[scale == 0.0] = 1.0
    design = (features - mean) / scale
    prediction = (prediction_feature - mean) / scale
    design = np.column_stack((np.ones(len(design)), design))
    prediction = np.concatenate(([1.0], prediction))
    penalty = np.diag([0.0, *([alpha] * (design.shape[1] - 1))])
    try:
        coefficients = np.linalg.solve(design.T @ design + penalty, design.T @ targets)
    except np.linalg.LinAlgError:
        coefficients = np.linalg.pinv(design.T @ design + penalty) @ (design.T @ targets)
    return float(prediction @ coefficients)


def _is_local_night(target_end: dt.datetime) -> bool:
    """判断目标小时是否处于当地夜间（用于夜间光伏强制置零）。"""
    local_start_hour = (target_end.astimezone(CENTRAL) - dt.timedelta(hours=1)).hour
    return local_start_hour < 6 or local_start_hour >= 20


def _forecast_rows(
    values_by_timestamp: Mapping[dt.datetime, Mapping[str, float]],
    hour_index: HourIndex,
    *,
    cutoff: dt.datetime,
    delivery_date: dt.date,
    alphas: Mapping[str, float],
) -> list[dict[str, object]]:
    """为一个交割日逐小时生成预测行。"""
    known_end = cutoff - dt.timedelta(hours=FORECAST_INFORMATION_PROTECTION_HOURS)
    rows: list[dict[str, object]] = []
    for target_end in _interval_endpoints(delivery_date):
        row: dict[str, object] = {
            "forecast_cutoff_utc": _timestamp_text(cutoff),
            "forecast_target_end_utc": _timestamp_text(target_end),
            "forecast_method": FORECAST_METHOD,
        }
        for column, forecast_column in TARGET_TO_FORECAST.items():
            features, targets, prediction_feature = _training_data(
                values_by_timestamp,
                hour_index,
                current_known_end=known_end,
                delivery_date=delivery_date,
                target_end=target_end,
                column=column,
            )
            value = max(
                0.0,
                _ridge_prediction(
                    features,
                    targets,
                    prediction_feature,
                    alpha=float(alphas[column]),
                ),
            )
            if column == "erco_solar_generation_mwh" and _is_local_night(target_end):
                value = 0.0
            row[forecast_column] = value
        rows.append(row)
    return rows


def select_ridge_alpha(
    history: Sequence[Mapping[str, object]],
) -> dict[str, float]:
    """仅用 2023 年逐日滚动起点，为每个信号选择 alpha。"""

    validation = validate_ridge_selection(history)
    return {
        column: float(metrics["selected_alpha"])
        for column, metrics in validation["targets"].items()
    }


def validate_ridge_selection(
    history: Sequence[Mapping[str, object]],
    *,
    origin_dates: Sequence[dt.date] | None = None,
) -> dict[str, object]:
    """在登记的 2023 年逐日滚动起点上评估 Ridge 与中位数基线。"""

    values_by_timestamp = _history_by_timestamp(history)
    hour_index = _build_hour_index(values_by_timestamp)
    origins = list(origin_dates or ridge_selection_dates())
    if not origins:
        raise ValueError("Ridge validation origin_dates must not be empty")
    if any(date.year != RIDGE_SELECTION_YEAR for date in origins):
        raise ValueError(
            f"Ridge validation origin_dates must all be in {RIDGE_SELECTION_YEAR}"
        )
    errors = {
        column: {alpha: [] for alpha in RIDGE_ALPHAS}
        for column in TARGET_COLUMNS
    }
    baseline_errors = {column: [] for column in TARGET_COLUMNS}
    actual_magnitudes = {column: [] for column in TARGET_COLUMNS}
    used_origins: set[dt.date] = set()

    for delivery_date in origins:
        cutoff = _cutoff_for_delivery_date(delivery_date)
        known_end = cutoff - dt.timedelta(
            hours=FORECAST_INFORMATION_PROTECTION_HOURS
        )
        for target_end in _interval_endpoints(delivery_date):
            local_start_hour = (
                target_end.astimezone(CENTRAL) - dt.timedelta(hours=1)
            ).hour
            for column in TARGET_COLUMNS:
                actual = values_by_timestamp.get(target_end, {}).get(column)
                if actual is None:
                    continue
                try:
                    features, targets, prediction_feature = _training_data(
                        values_by_timestamp,
                        hour_index,
                        current_known_end=known_end,
                        delivery_date=delivery_date,
                        target_end=target_end,
                        column=column,
                    )
                except ValueError:
                    continue
                observations = _same_hour_observations(
                    hour_index,
                    known_end=known_end,
                    local_start_hour=local_start_hour,
                    column=column,
                )
                if len(observations) < FORECAST_BASELINE_DAYS:
                    continue
                baseline = max(
                    0.0,
                    float(np.median(observations[-FORECAST_BASELINE_DAYS:])),
                )
                if column == "erco_solar_generation_mwh" and _is_local_night(
                    target_end
                ):
                    baseline = 0.0
                baseline_errors[column].append(abs(baseline - actual))
                actual_magnitudes[column].append(abs(actual))
                used_origins.add(delivery_date)
                for alpha in RIDGE_ALPHAS:
                    prediction = max(
                        0.0,
                        _ridge_prediction(
                            features,
                            targets,
                            prediction_feature,
                            alpha=alpha,
                        ),
                    )
                    if column == "erco_solar_generation_mwh" and _is_local_night(
                        target_end
                    ):
                        prediction = 0.0
                    errors[column][alpha].append(abs(prediction - actual))

    target_metrics: dict[str, dict[str, float | int]] = {}
    for column in TARGET_COLUMNS:
        if not baseline_errors[column]:
            raise ValueError(
                f"no usable {RIDGE_SELECTION_YEAR} validation samples for {column}"
            )
        mae_by_alpha = {
            alpha: float(np.mean(column_errors))
            for alpha, column_errors in errors[column].items()
            if column_errors
        }
        if not mae_by_alpha:
            raise ValueError(f"Ridge validation produced no predictions for {column}")
        selected_alpha = min(
            mae_by_alpha,
            key=lambda alpha: (mae_by_alpha[alpha], alpha),
        )
        magnitude = float(np.mean(actual_magnitudes[column]))
        ridge_mae = mae_by_alpha[selected_alpha]
        median_mae = float(np.mean(baseline_errors[column]))
        target_metrics[column] = {
            "selected_alpha": float(selected_alpha),
            "sample_count": len(baseline_errors[column]),
            "ridge_mae": ridge_mae,
            "ridge_nmae": ridge_mae / magnitude if magnitude > 0.0 else 0.0,
            "median_mae": median_mae,
            "median_nmae": median_mae / magnitude if magnitude > 0.0 else 0.0,
        }
    return {
        "selection_year": RIDGE_SELECTION_YEAR,
        "purpose": "ridge_alpha_selection",
        "origin_dates": [date.isoformat() for date in sorted(used_origins)],
        "origin_count": len(used_origins),
        "targets": target_metrics,
    }


def ridge_selection_dates() -> tuple[dt.date, ...]:
    """返回用于 Ridge 超参数选择的完整 2023 年日期。"""

    days = (RIDGE_SELECTION_END - RIDGE_SELECTION_START).days + 1
    return tuple(RIDGE_SELECTION_START + dt.timedelta(days=offset) for offset in range(days))


def forecast_delivery_day(
    history: Sequence[Mapping[str, object]],
    *,
    cutoff_utc: str,
    delivery_date: str,
    alphas: Mapping[str, float] | None = None,
) -> list[dict[str, object]]:
    """只用保护期内的历史，预测交割日每个实际小时。"""

    cutoff = _timestamp(cutoff_utc, label="cutoff_utc")
    try:
        target_date = dt.date.fromisoformat(delivery_date)
    except (TypeError, ValueError) as error:
        raise ValueError("delivery_date must use YYYY-MM-DD") from error
    values_by_timestamp = _history_by_timestamp(history)
    hour_index = _build_hour_index(values_by_timestamp)
    chosen_alphas = dict(alphas or select_ridge_alpha(history))
    if set(chosen_alphas) != set(TARGET_COLUMNS):
        raise ValueError("Ridge alpha values must cover every forecast target")
    return _forecast_rows(
        values_by_timestamp,
        hour_index,
        cutoff=cutoff,
        delivery_date=target_date,
        alphas=chosen_alphas,
    )


def forecast_delivery_dates(
    history: Sequence[Mapping[str, object]],
    *,
    delivery_dates: Sequence[dt.date],
    alphas: Mapping[str, float],
    skip_unavailable: bool = False,
) -> list[dict[str, object]]:
    """共用一次历史索引，为多个交割日统一生成无泄漏逐小时预测。"""

    chosen_alphas = dict(alphas)
    if set(chosen_alphas) != set(TARGET_COLUMNS):
        raise ValueError("Ridge alpha values must cover every forecast target")
    values_by_timestamp = _history_by_timestamp(history)
    hour_index = _build_hour_index(values_by_timestamp)
    rows: list[dict[str, object]] = []
    for delivery_date in delivery_dates:
        try:
            rows.extend(
                _forecast_rows(
                    values_by_timestamp,
                    hour_index,
                    cutoff=_cutoff_for_delivery_date(delivery_date),
                    delivery_date=delivery_date,
                    alphas=chosen_alphas,
                )
            )
        except ForecastUnavailableError:
            if not skip_unavailable:
                raise
    return rows


def median_baseline(
    history: Sequence[Mapping[str, object]],
    *,
    cutoff_utc: str,
    delivery_date: str,
) -> list[dict[str, object]]:
    """返回保护期内的 28 天同小时中位数基线。"""

    cutoff = _timestamp(cutoff_utc, label="cutoff_utc")
    try:
        target_date = dt.date.fromisoformat(delivery_date)
    except (TypeError, ValueError) as error:
        raise ValueError("delivery_date must use YYYY-MM-DD") from error
    known_end = cutoff - dt.timedelta(hours=FORECAST_INFORMATION_PROTECTION_HOURS)
    values_by_timestamp = _history_by_timestamp(history)
    rows: list[dict[str, object]] = []
    for target_end in _interval_endpoints(target_date):
        local_start_hour = (target_end.astimezone(CENTRAL) - dt.timedelta(hours=1)).hour
        row: dict[str, object] = {
            "forecast_cutoff_utc": _timestamp_text(cutoff),
            "forecast_target_end_utc": _timestamp_text(target_end),
            "forecast_method": "same_hour_median_28d_v1",
        }
        for column, forecast_column in TARGET_TO_FORECAST.items():
            observations = _same_hour_observations(
                hour_index,
                known_end=known_end,
                local_start_hour=local_start_hour,
                column=column,
            )
            if len(observations) < FORECAST_BASELINE_DAYS:
                raise ValueError(f"insufficient baseline history for {column}")
            value = max(
                0.0,
                float(np.median(observations[-FORECAST_BASELINE_DAYS:])),
            )
            if column == "erco_solar_generation_mwh" and _is_local_night(target_end):
                value = 0.0
            row[forecast_column] = value
        rows.append(row)
    return rows
