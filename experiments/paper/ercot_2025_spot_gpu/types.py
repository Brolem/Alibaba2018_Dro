from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class EnergyInterval:
    """One hourly energy interval expressed by its UTC start and end instants."""

    interval_start_utc: str
    interval_end_utc: str


@dataclass(frozen=True)
class Job:
    """One trace job after preserving its model-specific whole-gang request."""

    job_id: str
    organization: str
    priority: Literal["HP", "Spot"]
    gpu_model: str
    gpu_count: float
    submit_time_seconds: int
    release_hour: int
    duration_seconds: int
    required_run_hours: int
    deadline_hour: int | None


@dataclass(frozen=True)
class ReplaySelection:
    """Deterministic representative trace core and the jobs needed by replay."""

    core_start_hour: int
    core_end_hour: int
    core_hours: int
    max_spot_duration_hours: int
    median_eligible_spot_gpu_hours: float
    selected_eligible_spot_gpu_hours: float
    excluded_spot_over_max_duration_count: int
    spot_jobs: tuple[Job, ...]
    hp_jobs: tuple[Job, ...]


@dataclass(frozen=True)
class PowerScenario:
    """A named, immutable facility incremental-power sensitivity scenario."""

    name: Literal["low", "baseline", "high"]
    pue: float
    it_overhead_multiplier: float
    active_power_fraction: float
    model_tdp_watts: tuple[tuple[str, float], ...]


@dataclass(frozen=True)
class ScheduledRun:
    """One recovered, whole-gang execution hour for a trace job."""

    job_id: str
    trace_hour: int
    gpu_model: str
    gpu_count: float
