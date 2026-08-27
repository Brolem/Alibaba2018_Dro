"""把能源窗口、workload 柔性包络、在线负荷对齐成统一小时输入（MW）。"""

from __future__ import annotations

import csv
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from .config import (
    EFFECTIVE_REPLAY_CAPACITY_FRACTION,
    N_MACHINES,
    PHYSICAL_CAPACITY_CORES,
    POWER_SCENARIOS,
)


ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
DATA_RAW = DATA / "raw"
DATA_PROCESSED = DATA / "processed"
DATA_RESULTS = DATA / "results"

@dataclass(frozen=True)
class HourlyInput:
    """调度器的一个小时输入：预测/实际能源、有效容量和柔性负荷。"""

    hour: int
    timestamp_utc: str
    dam_lz_houston_usd_per_mwh: float
    forecast_erco_solar_generation_mwh: float
    forecast_erco_wind_generation_mwh: float
    forecast_consumed_co2_lbs_per_kwh: float
    actual_consumed_co2_lbs_per_kwh: float
    online_mw: float
    base_mw: float
    batch_baseline_mwh: float
    batch_window_mwh: float
    actual_erco_solar_generation_mwh: float = 0.0
    actual_erco_wind_generation_mwh: float = 0.0
    batch_capacity_mw: float | None = None
    batch_cumulative_arrived_mwh: float | None = None
    batch_cumulative_due_mwh: float | None = None
    effective_capacity_cores: float | None = None
    workload_scale: float = 1.0
    workload_mwh_per_core_hour: float = 0.0


def _energy_core_rows(window_csv: Path, core_hours: int) -> list[dict[str, str]]:
    """读取一个能源窗口 CSV，只取核心期（period_role == "core"）的小时行。"""
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
    """读取 workload 柔性包络 CSV，返回全部小时行。"""
    with envelope_csv.open("r", encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def _online_cores(stats_json: Path) -> float:
    """从 workload 统计 JSON 读取在线负载的静态预留核数。"""
    payload = json.loads(stats_json.read_text(encoding="utf-8"))
    return float(payload["online_static"]["online_reserved_cores"])


def _effective_replay_scale(
    envelope: list[dict[str, str]],
    online_cores: float,
    effective_capacity_fraction: float,
) -> tuple[float, float]:
    """以统一缩放使在线与批处理代理不超过有效回放容量。"""

    if not 0.0 < effective_capacity_fraction <= 1.0:
        raise ValueError("effective_capacity_fraction must be in (0, 1]")
    effective_capacity_cores = (
        PHYSICAL_CAPACITY_CORES * effective_capacity_fraction
    )
    # 柔性窗口列是当前仍可调度的工作量，不是瞬时核需求；尺度闭合只使用
    # “到达即执行”的逐小时基线平均核数，调度阶段另受 batch_capacity_mw 限制。
    raw_peak_cores = max(
        online_cores + float(row["baseline_cores"] or 0.0)
        for row in envelope
    )
    if raw_peak_cores <= 0.0:
        return 1.0, effective_capacity_cores
    return min(1.0, effective_capacity_cores / raw_peak_cores), effective_capacity_cores


def build_hourly_input(
    window_csv: Path,
    envelope_csv: Path,
    stats_json: Path,
    *,
    core_days: int = 30,
    scenario: str = "base",
    effective_capacity_fraction: float = EFFECTIVE_REPLAY_CAPACITY_FRACTION,
) -> list[HourlyInput]:
    """取一个能源窗口的 30 天核心期，与 workload 包络逐小时对齐并换算成 MW。"""

    core_hours = core_days * 24
    energy = _energy_core_rows(window_csv, core_hours)
    return build_hourly_input_from_rows(
        energy,
        envelope_csv,
        stats_json,
        core_days=core_days,
        scenario=scenario,
        effective_capacity_fraction=effective_capacity_fraction,
    )


def build_hourly_input_from_rows(
    energy_rows: Sequence[Mapping[str, object]],
    envelope_csv: Path,
    stats_json: Path,
    *,
    core_days: int = 30,
    scenario: str = "base",
    effective_capacity_fraction: float = EFFECTIVE_REPLAY_CAPACITY_FRACTION,
) -> list[HourlyInput]:
    """把已按时序排列的能源行与公共 workload 包络对齐并换算为 MW。"""

    if scenario not in POWER_SCENARIOS:
        raise ValueError(f"unknown power scenario: {scenario}")
    power = POWER_SCENARIOS[scenario]
    power_per_core_mw = power["pue"] * power["active_w_per_core"] / 1e6
    base_mw = (
        power["pue"]
        * N_MACHINES
        * power["idle_w_per_machine"]
        / 1e6
    )

    core_hours = core_days * 24
    if len(energy_rows) < core_hours:
        raise ValueError(f"energy_rows has {len(energy_rows)} hours, need {core_hours}")
    energy = list(energy_rows[:core_hours])
    envelope = _read_envelope(envelope_csv)
    if len(envelope) < core_hours:
        raise ValueError(
            f"{envelope_csv.name} has {len(envelope)} hours, need {core_hours}"
        )

    raw_online_cores = _online_cores(stats_json)
    workload_scale, effective_capacity_cores = _effective_replay_scale(
        envelope,
        raw_online_cores,
        effective_capacity_fraction,
    )
    online_cores = raw_online_cores * workload_scale
    online_mw = online_cores * power_per_core_mw
    batch_capacity_mw = max(
        0.0, (effective_capacity_cores - online_cores) * power_per_core_mw
    )

    aligned: list[HourlyInput] = []
    for hour, energy_row in enumerate(energy):
        workload_row = envelope[hour]
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
                actual_consumed_co2_lbs_per_kwh=float(
                    energy_row["erco_consumed_co2_intensity_lbs_per_kwh"] or 0.0
                ),
                online_mw=online_mw,
                base_mw=base_mw,
                batch_baseline_mwh=(
                    float(workload_row["baseline_energy_core_hours"] or 0.0)
                    * workload_scale
                    * power_per_core_mw
                ),
                batch_window_mwh=(
                    float(workload_row["flexible_window_energy_core_hours"] or 0.0)
                    * workload_scale
                    * power_per_core_mw
                ),
                actual_erco_solar_generation_mwh=float(
                    energy_row["erco_solar_generation_mwh"] or 0.0
                ),
                actual_erco_wind_generation_mwh=float(
                    energy_row["erco_wind_generation_mwh"] or 0.0
                ),
                batch_capacity_mw=batch_capacity_mw,
                batch_cumulative_arrived_mwh=(
                    float(workload_row["cumulative_arrived_core_hours"] or 0.0)
                    * workload_scale
                    * power_per_core_mw
                    if "cumulative_arrived_core_hours" in workload_row
                    else None
                ),
                batch_cumulative_due_mwh=(
                    float(workload_row["cumulative_due_core_hours"] or 0.0)
                    * workload_scale
                    * power_per_core_mw
                    if "cumulative_due_core_hours" in workload_row
                    else None
                ),
                effective_capacity_cores=effective_capacity_cores,
                workload_scale=workload_scale,
                workload_mwh_per_core_hour=workload_scale * power_per_core_mw,
            )
        )
    return aligned
