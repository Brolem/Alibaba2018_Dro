"""联合不确定性场景的可审计日块和可复现抽样清单。"""

from __future__ import annotations

import csv
import hashlib
import json
import math
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .residuals import CSV_COLUMNS, RESIDUAL_COLUMNS


HOURS_PER_DAY = 24
CALIBRATION_BLOCKS_FILENAME = "calibration_day_blocks_2024.csv"
CALIBRATION_BLOCKS_MANIFEST_FILENAME = "calibration_day_blocks_manifest.json"
SAA_MANIFEST_FILENAME = "saa_scenarios_manifest.json"
SAA_SAMPLE_SIZES = (20, 50, 100, 200)
SAA_DAYS = 30
SAA_VALIDATION_WINDOWS_PER_FOLD = 12
WORKLOAD_TRAINING_SEED = 20240801
WORKLOAD_VALIDATION_SEED = 20240802
WORKLOAD_REPLAY_SEED = 20250801
ENERGY_TRAINING_SEED = 20240811
ENERGY_VALIDATION_SEED = 20240812
ENERGY_REPLAY_SEED = 20250811
(
    RESIDUAL_SOLAR_COLUMN,
    RESIDUAL_WIND_COLUMN,
    RESIDUAL_CARBON_COLUMN,
) = RESIDUAL_COLUMNS


@dataclass(frozen=True)
class ScenarioRealization:
    """一个联合 30 天场景；工作量以 core-hour、能源项以原始 EIA 单位记录。"""

    scenario_id: int
    workload_source_days_one_based: tuple[int, ...]
    energy_delivery_dates: tuple[str, ...]
    cumulative_arrived_core_hours: tuple[float, ...]
    cumulative_due_core_hours: tuple[float, ...]
    residual_solar_mwh: tuple[float, ...]
    residual_wind_mwh: tuple[float, ...]
    residual_carbon_lbs_per_kwh: tuple[float, ...]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as input_file:
        for block in iter(lambda: input_file.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def _is_true(value: object) -> bool:
    return str(value).strip().lower() == "true"


def _finite_float(value: object, *, label: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{label} must be numeric") from error
    if not math.isfinite(number):
        raise ValueError(f"{label} must be finite")
    return number


def _hour_ending(row: Mapping[str, object], *, delivery_date: str) -> int:
    raw_value = row.get("hour_ending")
    text = str(raw_value).strip()
    if ":" in text:
        hour_text, minute_text = text.split(":", maxsplit=1)
        if minute_text != "00":
            raise ValueError(
                f"invalid ERCOT hour-ending minute for {delivery_date}: {text}"
            )
        value = _finite_float(hour_text, label="ERCOT hour_ending")
    else:
        value = _finite_float(raw_value, label="ERCOT hour_ending")
    if not value.is_integer() or not 1 <= int(value) <= HOURS_PER_DAY:
        raise ValueError(f"invalid ERCOT hour ending for {delivery_date}: {value}")
    return int(value)


def _read_csv_by_date(path: Path) -> dict[str, list[dict[str, str]]]:
    grouped: dict[str, list[dict[str, str]]] = defaultdict(list)
    with path.open("r", encoding="utf-8", newline="") as input_file:
        for row in csv.DictReader(input_file):
            date_text = row.get("delivery_date", "")
            if not date_text:
                raise ValueError(f"{path.name} has a row without delivery_date")
            grouped[date_text].append(row)
    if not grouped:
        raise ValueError(f"{path.name} is empty")
    return dict(grouped)


def _valid_residual_block(rows: Sequence[Mapping[str, object]], *, date_text: str) -> bool:
    if len(rows) != HOURS_PER_DAY:
        return False
    if any(not _is_true(row.get("complete_row")) for row in rows):
        return False
    if any(not _is_true(row.get("usable_24h_block")) for row in rows):
        return False
    try:
        hour_indices = sorted(int(str(row.get("hour_index"))) for row in rows)
    except ValueError as error:
        raise ValueError(f"invalid residual hour_index for {date_text}") from error
    if hour_indices != list(range(HOURS_PER_DAY)):
        return False
    for row in rows:
        for column in RESIDUAL_COLUMNS:
            _finite_float(row.get(column), label=f"{date_text} {column}")
    return True


def write_calibration_day_blocks(
    *,
    residual_csv: Path,
    dam_price_rows: Sequence[Mapping[str, object]],
    dam_price_source: Path,
    output_directory: Path,
) -> dict[str, object]:
    """合并可用的 2024 联合残差日块与同日 LZ_HOUSTON DAM 价格。"""

    residual_by_date = _read_csv_by_date(residual_csv)
    prices_by_date: dict[str, list[Mapping[str, object]]] = defaultdict(list)
    for row in dam_price_rows:
        date_text = str(row.get("delivery_date", ""))
        if not date_text:
            raise ValueError("ERCOT price row has no delivery_date")
        prices_by_date[date_text].append(row)

    calibration_rows: list[dict[str, object]] = []
    excluded: list[dict[str, object]] = []
    fold_counts: Counter[str] = Counter()
    for date_text in sorted(residual_by_date):
        residual_rows = residual_by_date[date_text]
        if not _valid_residual_block(residual_rows, date_text=date_text):
            excluded.append(
                {"delivery_date": date_text, "reason": "residual_not_usable_24h_block"}
            )
            continue
        price_rows = prices_by_date.get(date_text, [])
        try:
            ordered_prices = sorted(
                price_rows,
                key=lambda row: _hour_ending(row, delivery_date=date_text),
            )
            price_hours = [
                _hour_ending(row, delivery_date=date_text) for row in ordered_prices
            ]
            if price_hours != list(range(1, HOURS_PER_DAY + 1)):
                raise ValueError("must contain exactly hour ending 1 through 24")
            price_values = [
                _finite_float(
                    row.get("dam_lz_houston_usd_per_mwh"),
                    label=f"{date_text} DAM price",
                )
                for row in ordered_prices
            ]
        except ValueError as error:
            excluded.append(
                {
                    "delivery_date": date_text,
                    "reason": "dam_price_not_usable_24h_block",
                    "detail": str(error),
                }
            )
            continue

        ordered_residuals = sorted(residual_rows, key=lambda row: int(row["hour_index"]))
        fold = ordered_residuals[0]["cv_fold"]
        if any(row["cv_fold"] != fold for row in ordered_residuals):
            raise ValueError(f"residual CV folds differ within {date_text}")
        fold_counts[fold] += 1
        for residual_row, price in zip(ordered_residuals, price_values, strict=True):
            calibration_rows.append(
                {
                    **{column: residual_row[column] for column in CSV_COLUMNS},
                    "dam_lz_houston_usd_per_mwh": price,
                }
            )

    if not calibration_rows:
        raise ValueError("no residual day blocks could be paired with DAM prices")
    output_directory.mkdir(parents=True, exist_ok=True)
    output_path = output_directory / CALIBRATION_BLOCKS_FILENAME
    fieldnames = (*CSV_COLUMNS, "dam_lz_houston_usd_per_mwh")
    with output_path.open("w", encoding="utf-8", newline="") as output_file:
        writer = csv.DictWriter(output_file, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        writer.writerows(calibration_rows)

    block_count = len(calibration_rows) // HOURS_PER_DAY
    manifest = {
        "schema_version": 1,
        "year": 2024,
        "purpose": "SAA_RO_DRO_calibration_day_blocks",
        "day_block_rule": (
            "retain only complete 24-hour joint residual blocks paired with exactly "
            "hour-ending 1..24 LZ_HOUSTON DAM prices"
        ),
        "row_count": len(calibration_rows),
        "usable_day_block_count": block_count,
        "usable_day_block_count_by_fold": dict(sorted(fold_counts.items())),
        "excluded_dates": excluded,
        "source": {
            "joint_residuals_2024": _sha256(residual_csv),
            "ercot_2024_dam_price_archive": _sha256(dam_price_source),
        },
        "output": {CALIBRATION_BLOCKS_FILENAME: _sha256(output_path)},
    }
    manifest_path = output_directory / CALIBRATION_BLOCKS_MANIFEST_FILENAME
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return manifest


def _read_workload_days(
    workload_csv: Path,
    included_days: Sequence[int],
) -> dict[int, list[float]]:
    grouped: dict[int, list[tuple[int, float]]] = defaultdict(list)
    with workload_csv.open("r", encoding="utf-8", newline="") as input_file:
        for row in csv.DictReader(input_file):
            day = int(row["day"])
            hour = int(row["hour_of_day"])
            grouped[day].append(
                (hour, _finite_float(row["arrival_work_core_hours"], label="workload"))
            )
    daily: dict[int, list[float]] = {}
    for day in included_days:
        values = sorted(grouped.get(day, []))
        if [hour for hour, _ in values] != list(range(HOURS_PER_DAY)):
            raise ValueError(f"workload source day {day} must have exactly 24 hours")
        daily[day] = [value for _, value in values]
    return daily


def _calibration_dates_by_fold(calibration_csv: Path) -> dict[str, list[str]]:
    grouped = _read_csv_by_date(calibration_csv)
    result: dict[str, list[str]] = defaultdict(list)
    for date_text, rows in grouped.items():
        if len(rows) != HOURS_PER_DAY:
            raise ValueError(f"calibration block {date_text} is not 24 hours")
        fold = rows[0]["cv_fold"]
        if any(row["cv_fold"] != fold for row in rows):
            raise ValueError(f"calibration block {date_text} has mixed folds")
        result[fold].append(date_text)
    return {fold: sorted(dates) for fold, dates in sorted(result.items())}


def _sample_workload_days(
    included_days: Sequence[int],
    *,
    rng: np.random.Generator,
    days: int,
    block_days: int,
) -> list[int]:
    if not included_days or block_days <= 0 or days <= 0:
        raise ValueError("workload sampling requires positive days and block_days")
    samples: list[int] = []
    for _ in range(math.ceil(days / block_days)):
        start = int(rng.integers(0, len(included_days)))
        samples.extend(
            included_days[(start + offset) % len(included_days)]
            for offset in range(block_days)
        )
    return samples[:days]


def _sample_energy_dates(
    dates: Sequence[str], *, rng: np.random.Generator, days: int
) -> list[str]:
    if not dates:
        raise ValueError("energy sampling requires at least one calibration day")
    return [dates[int(rng.integers(0, len(dates)))] for _ in range(days)]


def _scenario_specs(
    *,
    count: int,
    included_workload_days: Sequence[int],
    energy_dates: Sequence[str],
    workload_seed: int,
    energy_seed: int,
    days: int,
    block_days: int,
) -> list[dict[str, object]]:
    workload_rng = np.random.default_rng(workload_seed)
    energy_rng = np.random.default_rng(energy_seed)
    return [
        {
            "scenario_id": scenario_id,
            "workload_source_days_one_based": _sample_workload_days(
                included_workload_days,
                rng=workload_rng,
                days=days,
                block_days=block_days,
            ),
            "energy_delivery_dates": _sample_energy_dates(
                energy_dates,
                rng=energy_rng,
                days=days,
            ),
        }
        for scenario_id in range(count)
    ]


def write_saa_scenario_manifest(
    *,
    calibration_csv: Path,
    workload_csv: Path,
    workload_manifest: Path,
    output_path: Path,
) -> dict[str, object]:
    """写出 3 折训练、验证、最终训练及 2025 工作量回放的随机源日清单。"""

    workload_payload = json.loads(workload_manifest.read_text(encoding="utf-8"))
    parameters = workload_payload["parameters"]
    source = workload_payload["source"]
    days = int(parameters["days"])
    block_days = int(parameters["block_days"])
    flex_window_hours = int(parameters["flex_window_hours"])
    target_total = _finite_float(
        parameters["target_total_work_core_hours"], label="target_total_work_core_hours"
    )
    included_days = [int(day) for day in source["included_source_days_one_based"]]
    _read_workload_days(workload_csv, included_days)
    dates_by_fold = _calibration_dates_by_fold(calibration_csv)
    if tuple(sorted(dates_by_fold)) != ("fold_1", "fold_2", "fold_3"):
        raise ValueError("calibration blocks must contain fold_1, fold_2, and fold_3")

    training_by_fold: dict[str, object] = {}
    validation_by_fold: dict[str, object] = {}
    all_energy_dates = [date for dates in dates_by_fold.values() for date in dates]
    for fold_index, held_out_fold in enumerate(sorted(dates_by_fold), start=1):
        training_dates = [
            date
            for fold, dates in dates_by_fold.items()
            if fold != held_out_fold
            for date in dates
        ]
        training_by_fold[held_out_fold] = {
            "held_out_fold": held_out_fold,
            "training_folds": [fold for fold in sorted(dates_by_fold) if fold != held_out_fold],
            "workload_seed": WORKLOAD_TRAINING_SEED + fold_index,
            "energy_seed": ENERGY_TRAINING_SEED + fold_index,
            "scenarios": _scenario_specs(
                count=max(SAA_SAMPLE_SIZES),
                included_workload_days=included_days,
                energy_dates=training_dates,
                workload_seed=WORKLOAD_TRAINING_SEED + fold_index,
                energy_seed=ENERGY_TRAINING_SEED + fold_index,
                days=days,
                block_days=block_days,
            ),
        }
        validation_by_fold[held_out_fold] = {
            "held_out_fold": held_out_fold,
            "workload_seed": WORKLOAD_VALIDATION_SEED + fold_index,
            "energy_seed": ENERGY_VALIDATION_SEED + fold_index,
            "pseudo_windows": _scenario_specs(
                count=SAA_VALIDATION_WINDOWS_PER_FOLD,
                included_workload_days=included_days,
                energy_dates=dates_by_fold[held_out_fold],
                workload_seed=WORKLOAD_VALIDATION_SEED + fold_index,
                energy_seed=ENERGY_VALIDATION_SEED + fold_index,
                days=days,
                block_days=block_days,
            ),
        }

    manifest = {
        "schema_version": 1,
        "method": "joint_30day_block_bootstrap_manifest_v1",
        "purpose": "SAA training, calibration validation, and 2025 workload replay",
        "shared_protocol": {
            "joint_draw_rule": (
                "each 30-day scenario pairs one workload two-day circular-block draw "
                "with 30 independently sampled 24-hour joint energy residual blocks"
            ),
            "nested_saa_sample_sizes": list(SAA_SAMPLE_SIZES),
            "prefix_rule": "N uses scenario_id 0 through N-1 from the same list",
            "chance_constraint_violation_rate": 0.10,
            "validation_pseudo_windows_per_fold": SAA_VALIDATION_WINDOWS_PER_FOLD,
        },
        "parameters": {
            "days": days,
            "hours": days * HOURS_PER_DAY,
            "workload_block_days": block_days,
            "flex_window_hours": flex_window_hours,
            "target_total_work_core_hours": target_total,
            "seeds": {
                "workload": {
                    "training_base": WORKLOAD_TRAINING_SEED,
                    "validation_base": WORKLOAD_VALIDATION_SEED,
                    "replay": WORKLOAD_REPLAY_SEED,
                },
                "energy": {
                    "training_base": ENERGY_TRAINING_SEED,
                    "validation_base": ENERGY_VALIDATION_SEED,
                    "replay_reserved": ENERGY_REPLAY_SEED,
                },
            },
        },
        "source": {
            "calibration_day_blocks": _sha256(calibration_csv),
            "aggregate_workload_8d": _sha256(workload_csv),
            "nominal_workload_manifest": _sha256(workload_manifest),
            "included_workload_source_days_one_based": included_days,
            "calibration_day_block_count_by_fold": {
                fold: len(dates) for fold, dates in dates_by_fold.items()
            },
        },
        "training_by_held_out_fold": training_by_fold,
        "validation_by_held_out_fold": validation_by_fold,
        "final_training": {
            "workload_seed": WORKLOAD_TRAINING_SEED,
            "energy_seed": ENERGY_TRAINING_SEED,
            "scenarios": _scenario_specs(
                count=max(SAA_SAMPLE_SIZES),
                included_workload_days=included_days,
                energy_dates=all_energy_dates,
                workload_seed=WORKLOAD_TRAINING_SEED,
                energy_seed=ENERGY_TRAINING_SEED,
                days=days,
                block_days=block_days,
            ),
        },
        "workload_replay_2025": {
            "workload_seed": WORKLOAD_REPLAY_SEED,
            "scenario_count": 100,
            "scenarios": [
                {
                    "scenario_id": scenario_id,
                    "workload_source_days_one_based": _sample_workload_days(
                        included_days,
                        rng=np.random.default_rng(WORKLOAD_REPLAY_SEED + scenario_id),
                        days=days,
                        block_days=block_days,
                    ),
                }
                for scenario_id in range(100)
            ],
        },
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return manifest


def _workload_cumulative_envelopes(
    *,
    workload_days: Mapping[int, Sequence[float]],
    source_days: Sequence[int],
    target_total_work: float,
    flex_window_hours: int,
) -> tuple[tuple[float, ...], tuple[float, ...]]:
    arrival = [
        value
        for day in source_days
        for value in workload_days[day]
    ]
    sampled_total = sum(arrival)
    if sampled_total <= 0.0:
        raise ValueError("sampled workload scenario has no positive work")
    scale = target_total_work / sampled_total
    arrival = [value * scale for value in arrival]
    due = [0.0] * len(arrival)
    for hour, value in enumerate(arrival):
        due[min(hour + flex_window_hours, len(arrival) - 1)] += value
    cumulative_arrived = list(np.cumsum(arrival))
    cumulative_due = list(np.cumsum(due))
    cumulative_arrived[-1] = target_total_work
    cumulative_due[-1] = target_total_work
    return tuple(cumulative_arrived), tuple(cumulative_due)


def _select_manifest_specs(
    payload: Mapping[str, object],
    *,
    split: str,
    held_out_fold: str | None,
) -> Sequence[Mapping[str, object]]:
    if split == "final_training":
        return payload["final_training"]["scenarios"]  # type: ignore[index]
    if held_out_fold is None:
        raise ValueError(f"held_out_fold is required for split={split}")
    if split == "training":
        return payload["training_by_held_out_fold"][held_out_fold]["scenarios"]  # type: ignore[index]
    if split == "validation":
        return payload["validation_by_held_out_fold"][held_out_fold]["pseudo_windows"]  # type: ignore[index]
    raise ValueError("split must be final_training, training, or validation")


def load_saa_scenarios(
    *,
    manifest_path: Path,
    calibration_csv: Path,
    workload_csv: Path,
    split: str = "final_training",
    held_out_fold: str | None = None,
    scenario_count: int | None = None,
) -> list[ScenarioRealization]:
    """按 manifest 重建 SAA/校准场景，供调度器直接接入。"""

    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    parameters = payload["parameters"]
    source_days = [int(day) for day in payload["source"]["included_workload_source_days_one_based"]]
    workload_days = _read_workload_days(workload_csv, source_days)
    target_total = _finite_float(
        parameters["target_total_work_core_hours"], label="target_total_work_core_hours"
    )
    flex_window_hours = int(parameters["flex_window_hours"])
    expected_hours = int(parameters["hours"])
    calibration_by_date = _read_csv_by_date(calibration_csv)
    specs = list(
        _select_manifest_specs(payload, split=split, held_out_fold=held_out_fold)
    )
    if scenario_count is not None:
        if scenario_count <= 0 or scenario_count > len(specs):
            raise ValueError("scenario_count is outside the manifest range")
        specs = specs[:scenario_count]
    realizations: list[ScenarioRealization] = []
    for spec in specs:
        workload_sources = tuple(
            int(day) for day in spec["workload_source_days_one_based"]
        )
        energy_dates = tuple(str(date) for date in spec["energy_delivery_dates"])
        cumulative_arrived, cumulative_due = _workload_cumulative_envelopes(
            workload_days=workload_days,
            source_days=workload_sources,
            target_total_work=target_total,
            flex_window_hours=flex_window_hours,
        )
        energy_rows = [
            row
            for date_text in energy_dates
            for row in sorted(
                calibration_by_date.get(date_text, []),
                key=lambda item: int(item["hour_index"]),
            )
        ]
        if len(energy_rows) != expected_hours:
            raise ValueError("scenario references a missing or incomplete energy day block")
        if len(cumulative_arrived) != expected_hours:
            raise ValueError("scenario workload horizon differs from manifest horizon")
        realizations.append(
            ScenarioRealization(
                scenario_id=int(spec["scenario_id"]),
                workload_source_days_one_based=workload_sources,
                energy_delivery_dates=energy_dates,
                cumulative_arrived_core_hours=cumulative_arrived,
                cumulative_due_core_hours=cumulative_due,
                residual_solar_mwh=tuple(
                    _finite_float(row[RESIDUAL_SOLAR_COLUMN], label="residual solar")
                    for row in energy_rows
                ),
                residual_wind_mwh=tuple(
                    _finite_float(row[RESIDUAL_WIND_COLUMN], label="residual wind")
                    for row in energy_rows
                ),
                residual_carbon_lbs_per_kwh=tuple(
                    _finite_float(row[RESIDUAL_CARBON_COLUMN], label="residual carbon")
                    for row in energy_rows
                ),
            )
        )
    if not realizations:
        raise ValueError("selected manifest split contains no joint scenarios")
    return realizations


def load_workload_replay_scenarios(
    *,
    manifest_path: Path,
    workload_csv: Path,
) -> list[ScenarioRealization]:
    """重建 manifest 中100条2025算力回放轨迹，能源残差留空为零。

    调用方应按具体2025能源窗口，用实际值减预测值替换三类残差；这样同一
    窗口只是一条能源观测，而100条轨迹仅用于估计算力条件风险。
    """

    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    parameters = payload["parameters"]
    source_days = [int(day) for day in payload["source"]["included_workload_source_days_one_based"]]
    workload_days = _read_workload_days(workload_csv, source_days)
    target_total = _finite_float(parameters["target_total_work_core_hours"], label="target_total_work_core_hours")
    flex_window_hours = int(parameters["flex_window_hours"])
    expected_hours = int(parameters["hours"])
    specs = payload["workload_replay_2025"]["scenarios"]
    zeros = (0.0,) * expected_hours
    realizations: list[ScenarioRealization] = []
    for spec in specs:
        workload_sources = tuple(int(day) for day in spec["workload_source_days_one_based"])
        cumulative_arrived, cumulative_due = _workload_cumulative_envelopes(
            workload_days=workload_days,
            source_days=workload_sources,
            target_total_work=target_total,
            flex_window_hours=flex_window_hours,
        )
        realizations.append(
            ScenarioRealization(
                scenario_id=int(spec["scenario_id"]),
                workload_source_days_one_based=workload_sources,
                energy_delivery_dates=(),
                cumulative_arrived_core_hours=cumulative_arrived,
                cumulative_due_core_hours=cumulative_due,
                residual_solar_mwh=zeros,
                residual_wind_mwh=zeros,
                residual_carbon_lbs_per_kwh=zeros,
            )
        )
    if len(realizations) != int(payload["workload_replay_2025"]["scenario_count"]):
        raise ValueError("workload replay manifest count mismatch")
    return realizations


def attach_bootstrap_energy_replay(
    workload_scenarios: Sequence[ScenarioRealization],
    *,
    calibration_csv: Path,
    energy_seed: int = ENERGY_REPLAY_SEED,
    residual_scale: float = 1.0,
) -> list[ScenarioRealization]:
    """为算力回放轨迹配对可复现的2024联合能源残差块自助样本。

    该函数用于压力/边界发现，不产生独立样本外能源观测。每个场景使用
    ``SeedSequence([energy_seed, scenario_id])`` 构造二维键随机流，并按完整24小时块
    有放回抽样，因而不同方法可按 ``scenario_id`` 严格配对复现。
    """

    if not workload_scenarios:
        raise ValueError("workload_scenarios must not be empty")
    if not math.isfinite(residual_scale) or residual_scale <= 0.0:
        raise ValueError("residual_scale must be positive and finite")
    calibration_by_date = _read_csv_by_date(calibration_csv)
    energy_dates = sorted(calibration_by_date)
    for date_text in energy_dates:
        rows = calibration_by_date[date_text]
        if not _valid_residual_block(rows, date_text=date_text):
            raise ValueError(f"calibration day {date_text} is not a usable 24h block")

    scenario_ids = [scenario.scenario_id for scenario in workload_scenarios]
    if len(set(scenario_ids)) != len(scenario_ids):
        raise ValueError("workload scenario ids must be unique")

    realizations: list[ScenarioRealization] = []
    for scenario in workload_scenarios:
        hours = len(scenario.cumulative_arrived_core_hours)
        if hours <= 0 or hours % HOURS_PER_DAY:
            raise ValueError("workload scenario horizon must contain complete days")
        if len(scenario.cumulative_due_core_hours) != hours:
            raise ValueError("workload scenario envelopes must have equal lengths")
        sampled_dates = tuple(
            _sample_energy_dates(
                energy_dates,
                rng=np.random.default_rng(
                    np.random.SeedSequence([energy_seed, scenario.scenario_id])
                ),
                days=hours // HOURS_PER_DAY,
            )
        )
        energy_rows = [
            row
            for date_text in sampled_dates
            for row in sorted(
                calibration_by_date[date_text],
                key=lambda item: int(item["hour_index"]),
            )
        ]
        realizations.append(
            ScenarioRealization(
                scenario_id=scenario.scenario_id,
                workload_source_days_one_based=scenario.workload_source_days_one_based,
                energy_delivery_dates=sampled_dates,
                cumulative_arrived_core_hours=scenario.cumulative_arrived_core_hours,
                cumulative_due_core_hours=scenario.cumulative_due_core_hours,
                residual_solar_mwh=tuple(
                    residual_scale
                    * _finite_float(row[RESIDUAL_SOLAR_COLUMN], label="residual solar")
                    for row in energy_rows
                ),
                residual_wind_mwh=tuple(
                    residual_scale
                    * _finite_float(row[RESIDUAL_WIND_COLUMN], label="residual wind")
                    for row in energy_rows
                ),
                residual_carbon_lbs_per_kwh=tuple(
                    residual_scale
                    * _finite_float(row[RESIDUAL_CARBON_COLUMN], label="residual carbon")
                    for row in energy_rows
                ),
            )
        )
    return realizations


def load_calibration_energy_rows(
    calibration_csv: Path,
    energy_delivery_dates: Sequence[str],
) -> list[dict[str, str]]:
    """按 manifest 中的交割日顺序拼接校准表的 24 小时预测/实际能源行。"""

    if not energy_delivery_dates:
        raise ValueError("energy_delivery_dates must not be empty")
    calibration_by_date = _read_csv_by_date(calibration_csv)
    rows: list[dict[str, str]] = []
    for date_text in energy_delivery_dates:
        daily_rows = sorted(
            calibration_by_date.get(date_text, []),
            key=lambda row: int(row["hour_index"]),
        )
        if len(daily_rows) != HOURS_PER_DAY:
            raise ValueError(f"calibration day {date_text} is missing or incomplete")
        if [int(row["hour_index"]) for row in daily_rows] != list(range(HOURS_PER_DAY)):
            raise ValueError(f"calibration day {date_text} has invalid hour indices")
        rows.extend(daily_rows)
    return rows


def load_hourly_downward_residual_quantiles(
    calibration_csv: Path,
    *,
    quantile: float = 0.90,
    held_out_fold: str | None = None,
) -> tuple[tuple[float, ...], tuple[float, ...]]:
    """计算训练折中 24 个小时位置的 PV/Wind 下偏分位数。

    下偏定义为 ``max(0, forecast-actual)``，即残差为负时的绝对值。
    指定 ``held_out_fold`` 时完全排除该折；不指定时使用全部可用日块。
    """

    if not 0.0 < quantile < 1.0:
        raise ValueError("quantile must be in (0, 1)")
    by_hour_solar: list[list[float]] = [[] for _ in range(HOURS_PER_DAY)]
    by_hour_wind: list[list[float]] = [[] for _ in range(HOURS_PER_DAY)]
    with calibration_csv.open(newline="", encoding="utf-8") as input_file:
        for row in csv.DictReader(input_file):
            if not _is_true(row.get("usable_24h_block", "False")):
                continue
            if held_out_fold is not None and row.get("cv_fold") == held_out_fold:
                continue
            hour = int(row["hour_index"])
            if not 0 <= hour < HOURS_PER_DAY:
                raise ValueError("calibration row contains an invalid hour_index")
            by_hour_solar[hour].append(
                max(0.0, -_finite_float(row[RESIDUAL_SOLAR_COLUMN], label="residual solar"))
            )
            by_hour_wind[hour].append(
                max(0.0, -_finite_float(row[RESIDUAL_WIND_COLUMN], label="residual wind"))
            )

    def linear_quantile(values: list[float]) -> float:
        if not values:
            raise ValueError("no calibration residuals remain after fold filtering")
        ordered = sorted(values)
        position = quantile * (len(ordered) - 1)
        lower = int(math.floor(position))
        upper = int(math.ceil(position))
        if lower == upper:
            return ordered[lower]
        fraction = position - lower
        return ordered[lower] + fraction * (ordered[upper] - ordered[lower])

    return (
        tuple(linear_quantile(values) for values in by_hour_solar),
        tuple(linear_quantile(values) for values in by_hour_wind),
    )
