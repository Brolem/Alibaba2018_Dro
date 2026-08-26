"""由 Alibaba 8 天轨迹生成实验共用的 30 天名义算力负荷。

原始任务只用于计算到达小时和 core-hour 工作量；可延迟窗口是独立的研究参数，
不再使用逐任务 Poisson 到达，也不再用 ``slack_ratio * duration`` 推导 deadline。

生成流程：
1. 将 ``batch_task.csv`` 聚合为 8 x 24 的到达工作量矩阵；
2. 排除审计确认属于追踪起点状态快照的第 1 天；
3. 用平衡两日循环块构造唯一 30 天名义序列，避免源日重复次数失衡；
4. 将名义序列缩放到完整日均值乘以 30 天的总工作量；
5. 按固定柔性窗口构造累计已到达/累计到期包络。

可选训练场景只用于后续 SAA/RO/DRO 不确定性标定，不是不同方法各自的
实验输入；默认不生成，样本数应由收敛实验确定。

依赖：仅 numpy。
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np


ROOT = Path(__file__).resolve().parents[1]
RAW_WORKLOAD = ROOT / "data" / "raw" / "workload"
PROCESSED_WORKLOAD = ROOT / "data" / "processed" / "workload"
SECONDS_PER_HOUR = 3600
HOURS_PER_DAY = 24
TRACE_DAYS = 8
DEFAULT_EXCLUDED_SOURCE_DAYS_ONE_BASED = (1,)


@dataclass(frozen=True)
class TraceAggregate:
    """8 天源轨迹的逐日逐小时工作量及读取审计。"""

    daily_arrival_work: np.ndarray
    source_sha256: str
    rows_read: int
    positive_work_rows: int
    zero_work_rows: int
    negative_duration_rows: int
    outside_trace_rows: int


@dataclass(frozen=True)
class WorkloadScenario:
    """一个固定总工作量的聚合柔性场景。"""

    scenario_id: int
    source_days: tuple[int, ...]
    normalization_scale: float
    arrival_work: np.ndarray
    due_work: np.ndarray
    active_window_work: np.ndarray
    cumulative_arrived: np.ndarray
    cumulative_due: np.ndarray


def aggregate_trace(
    batch_task_path: Path,
    *,
    trace_days: int = TRACE_DAYS,
) -> TraceAggregate:
    """把任务记录聚合为 ``trace_days x 24`` 的到达 core-hour 矩阵。"""

    if trace_days <= 0:
        raise ValueError("trace_days must be positive")
    trace_hours = trace_days * HOURS_PER_DAY
    daily = np.zeros((trace_days, HOURS_PER_DAY), dtype=np.float64)
    digest = hashlib.sha256()
    rows_read = 0
    positive_work_rows = 0
    zero_work_rows = 0
    negative_duration_rows = 0
    outside_trace_rows = 0

    with batch_task_path.open("rb") as input_file:
        for raw_line in input_file:
            digest.update(raw_line)
            rows_read += 1
            fields = raw_line.rstrip(b"\r\n").split(b",")
            instance_num = int(fields[1]) if fields[1] else 0
            start = int(fields[5])
            end = int(fields[6])
            plan_cpu = float(fields[7]) if fields[7] else 0.0

            if end < start:
                negative_duration_rows += 1
                continue
            release_hour = start // SECONDS_PER_HOUR
            if release_hour < 0 or release_hour >= trace_hours:
                outside_trace_rows += 1
                continue

            duration_seconds = end - start
            work_core_hours = (
                instance_num
                * (plan_cpu / 100.0)
                * duration_seconds
                / SECONDS_PER_HOUR
            )
            if work_core_hours <= 0.0:
                zero_work_rows += 1
                continue

            day, hour = divmod(release_hour, HOURS_PER_DAY)
            daily[day, hour] += work_core_hours
            positive_work_rows += 1

    if not np.isfinite(daily).all() or float(daily.sum()) <= 0.0:
        raise ValueError("trace aggregation produced no finite positive work")
    return TraceAggregate(
        daily_arrival_work=daily,
        source_sha256=digest.hexdigest(),
        rows_read=rows_read,
        positive_work_rows=positive_work_rows,
        zero_work_rows=zero_work_rows,
        negative_duration_rows=negative_duration_rows,
        outside_trace_rows=outside_trace_rows,
    )


def sample_source_days(
    *,
    trace_days: int,
    target_days: int,
    block_days: int,
    rng: np.random.Generator,
) -> tuple[int, ...]:
    """循环抽取连续日块，保留短期相邻日结构。"""

    if trace_days <= 0 or target_days <= 0 or block_days <= 0:
        raise ValueError("trace_days, target_days and block_days must be positive")
    sampled: list[int] = []
    block_count = math.ceil(target_days / block_days)
    for _ in range(block_count):
        start_day = int(rng.integers(0, trace_days))
        sampled.extend(
            (start_day + offset) % trace_days for offset in range(block_days)
        )
    return tuple(sampled[:target_days])


def sample_balanced_source_days(
    *,
    trace_days: int,
    target_days: int,
    block_days: int,
    rng: np.random.Generator,
) -> tuple[int, ...]:
    """平衡使用循环日块，作为唯一名义序列的确定性抽样规则。"""

    if trace_days <= 0 or target_days <= 0 or block_days <= 0:
        raise ValueError("trace_days, target_days and block_days must be positive")
    block_count = math.ceil(target_days / block_days)
    full_cycles, remaining_blocks = divmod(block_count, trace_days)
    starts = list(range(trace_days)) * full_cycles
    if remaining_blocks:
        starts.extend(
            int(value)
            for value in rng.choice(
                trace_days,
                size=remaining_blocks,
                replace=False,
            )
        )
    rng.shuffle(starts)
    sampled: list[int] = []
    for start_day in starts:
        sampled.extend(
            (start_day + offset) % trace_days for offset in range(block_days)
        )
    return tuple(sampled[:target_days])


def _scenario_from_source_days(
    daily_arrival_work: np.ndarray,
    *,
    scenario_id: int,
    source_days: tuple[int, ...],
    flex_window_hours: int,
    target_total_work: float,
) -> WorkloadScenario:
    """按给定源日序列构造固定总工作量与累计柔性包络。"""

    if daily_arrival_work.ndim != 2 or daily_arrival_work.shape[1] != HOURS_PER_DAY:
        raise ValueError("daily_arrival_work must have shape (days, 24)")
    if not source_days:
        raise ValueError("source_days must not be empty")
    if min(source_days) < 0 or max(source_days) >= daily_arrival_work.shape[0]:
        raise ValueError("source_days contains an index outside daily_arrival_work")
    if flex_window_hours < 0:
        raise ValueError("flex_window_hours must be non-negative")
    if target_total_work <= 0.0:
        raise ValueError("target_total_work must be positive")

    arrival = np.concatenate([daily_arrival_work[day] for day in source_days])
    sampled_total = float(arrival.sum())
    if sampled_total <= 0.0:
        raise ValueError("sampled scenario has zero total work")
    normalization_scale = target_total_work / sampled_total
    arrival = arrival * normalization_scale

    hours = len(arrival)
    due = np.zeros(hours, dtype=np.float64)
    for release_hour, work in enumerate(arrival):
        due_hour = min(release_hour + flex_window_hours, hours - 1)
        due[due_hour] += work

    active_window = np.zeros(hours, dtype=np.float64)
    rolling_work = 0.0
    for hour, work in enumerate(arrival):
        rolling_work += float(work)
        expired_hour = hour - flex_window_hours - 1
        if expired_hour >= 0:
            rolling_work -= float(arrival[expired_hour])
        active_window[hour] = max(0.0, rolling_work)

    cumulative_arrived = np.cumsum(arrival)
    cumulative_due = np.cumsum(due)
    cumulative_arrived[-1] = target_total_work
    cumulative_due[-1] = target_total_work

    if np.any(cumulative_due - cumulative_arrived > 1e-6):
        raise ValueError("cumulative due work exceeds cumulative arrived work")
    return WorkloadScenario(
        scenario_id=scenario_id,
        source_days=source_days,
        normalization_scale=normalization_scale,
        arrival_work=arrival,
        due_work=due,
        active_window_work=active_window,
        cumulative_arrived=cumulative_arrived,
        cumulative_due=cumulative_due,
    )


def build_scenario(
    daily_arrival_work: np.ndarray,
    *,
    scenario_id: int,
    days: int,
    block_days: int,
    flex_window_hours: int,
    target_total_work: float,
    rng: np.random.Generator,
) -> WorkloadScenario:
    """重采样到达工作量，并以独立柔性窗口构造累计包络。"""

    source_days = sample_source_days(
        trace_days=daily_arrival_work.shape[0],
        target_days=days,
        block_days=block_days,
        rng=rng,
    )
    return _scenario_from_source_days(
        daily_arrival_work,
        scenario_id=scenario_id,
        source_days=source_days,
        flex_window_hours=flex_window_hours,
        target_total_work=target_total_work,
    )


def generate_nominal_scenario(
    daily_arrival_work: np.ndarray,
    *,
    days: int,
    seed: int,
    block_days: int,
    flex_window_hours: int,
) -> tuple[WorkloadScenario, float]:
    """生成所有方法共用的平衡块重采样名义负荷。"""

    target_total_work = float(daily_arrival_work.mean(axis=0).sum()) * days
    rng = np.random.default_rng(seed)
    source_days = sample_balanced_source_days(
        trace_days=daily_arrival_work.shape[0],
        target_days=days,
        block_days=block_days,
        rng=rng,
    )
    return (
        _scenario_from_source_days(
            daily_arrival_work,
            scenario_id=0,
            source_days=source_days,
            flex_window_hours=flex_window_hours,
            target_total_work=target_total_work,
        ),
        target_total_work,
    )


def generate_scenarios(
    daily_arrival_work: np.ndarray,
    *,
    days: int,
    scenario_count: int,
    seed: int,
    block_days: int,
    flex_window_hours: int,
) -> tuple[list[WorkloadScenario], float]:
    """生成共享固定总工作量的场景库。"""

    if scenario_count <= 0:
        raise ValueError("scenario_count must be positive")
    trace_days = daily_arrival_work.shape[0]
    target_total_work = float(daily_arrival_work.sum()) / trace_days * days
    rng = np.random.default_rng(seed)
    scenarios = [
        build_scenario(
            daily_arrival_work,
            scenario_id=scenario_id,
            days=days,
            block_days=block_days,
            flex_window_hours=flex_window_hours,
            target_total_work=target_total_work,
            rng=rng,
        )
        for scenario_id in range(scenario_count)
    ]
    return scenarios, target_total_work


SCENARIO_COLUMNS = [
    "scenario_id",
    "hour",
    "baseline_cores",
    "baseline_energy_core_hours",
    "flexible_window_energy_core_hours",
    "arrival_work_core_hours",
    "due_work_core_hours",
    "cumulative_arrived_core_hours",
    "cumulative_due_core_hours",
]


def _scenario_rows(scenario: WorkloadScenario):
    arrival_values = np.round(scenario.arrival_work, 6)
    due_values = np.round(scenario.due_work, 6)
    target_total = round(float(scenario.arrival_work.sum()), 6)
    arrival_values[-1] += target_total - float(arrival_values.sum())
    due_values[-1] += target_total - float(due_values.sum())
    cumulative_arrived = np.cumsum(arrival_values)
    cumulative_due = np.cumsum(due_values)

    for hour in range(len(arrival_values)):
        arrival = float(arrival_values[hour])
        yield [
            scenario.scenario_id,
            hour,
            round(arrival, 6),
            round(arrival, 6),
            round(float(scenario.active_window_work[hour]), 6),
            round(arrival, 6),
            round(float(due_values[hour]), 6),
            round(float(cumulative_arrived[hour]), 6),
            round(float(cumulative_due[hour]), 6),
        ]


def write_scenario_csv(path: Path, scenarios: list[WorkloadScenario]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as output_file:
        writer = csv.writer(output_file)
        writer.writerow(SCENARIO_COLUMNS)
        for scenario in scenarios:
            writer.writerows(_scenario_rows(scenario))


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as input_file:
        for chunk in iter(lambda: input_file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--block-days", type=int, default=2)
    parser.add_argument("--flex-window-hours", type=int, default=6)
    parser.add_argument(
        "--exclude-source-days",
        type=int,
        nargs="*",
        default=list(DEFAULT_EXCLUDED_SOURCE_DAYS_ONE_BASED),
        help="按 1 开始编号；默认排除审计确认不完整的第 1 天",
    )
    parser.add_argument(
        "--training-scenario-count",
        type=int,
        default=0,
        help="仅供后续 SAA/RO/DRO 标定；默认不生成，最终数量由收敛实验确定",
    )
    parser.add_argument(
        "--source",
        type=Path,
        default=RAW_WORKLOAD / "batch_task.csv",
    )
    parser.add_argument(
        "--nominal-out",
        type=Path,
        default=PROCESSED_WORKLOAD / "nominal_workload_30d.csv",
        help="所有方法共用的唯一 30 天名义算力负荷",
    )
    parser.add_argument(
        "--training-scenarios-out",
        type=Path,
        default=PROCESSED_WORKLOAD / "compute_training_scenarios_30d.csv",
    )
    parser.add_argument(
        "--manifest-out",
        type=Path,
        default=PROCESSED_WORKLOAD / "nominal_workload_manifest.json",
    )
    args = parser.parse_args()

    trace = aggregate_trace(args.source)
    excluded_days = sorted(set(args.exclude_source_days))
    invalid_exclusions = [
        day for day in excluded_days if day < 1 or day > trace.daily_arrival_work.shape[0]
    ]
    if invalid_exclusions:
        raise ValueError(f"exclude-source-days outside trace: {invalid_exclusions}")
    included_days = [
        day
        for day in range(1, trace.daily_arrival_work.shape[0] + 1)
        if day not in excluded_days
    ]
    if len(included_days) < args.block_days:
        raise ValueError("too few included source days for requested block length")
    retained_daily_work = trace.daily_arrival_work[
        np.asarray([day - 1 for day in included_days], dtype=np.int64)
    ]

    nominal, target_total_work = generate_nominal_scenario(
        retained_daily_work,
        days=args.days,
        seed=args.seed,
        block_days=args.block_days,
        flex_window_hours=args.flex_window_hours,
    )
    write_scenario_csv(args.nominal_out, [nominal])

    training_scenarios: list[WorkloadScenario] = []
    if args.training_scenario_count < 0:
        raise ValueError("training-scenario-count must be non-negative")
    if args.training_scenario_count:
        training_scenarios, training_target = generate_scenarios(
            retained_daily_work,
            days=args.days,
            scenario_count=args.training_scenario_count,
            seed=args.seed + 1,
            block_days=args.block_days,
            flex_window_hours=args.flex_window_hours,
        )
        if not math.isclose(training_target, target_total_work, abs_tol=1e-6):
            raise ValueError("nominal and training targets must match")
        write_scenario_csv(args.training_scenarios_out, training_scenarios)

    try:
        source_reference = args.source.resolve().relative_to(ROOT.resolve()).as_posix()
    except ValueError:
        source_reference = args.source.name

    manifest = {
        "method": "aggregate_workload_balanced_nominal_block_bootstrap_v2",
        "note": (
            "工作量由 batch_task 的观测时长与计划 CPU 计算；固定柔性窗口是独立的"
            "反事实研究参数，不是生产 SLA。所有方法共用唯一名义负荷；可选训练场景"
            "只用于不确定性标定。"
        ),
        "source": {
            "path": source_reference,
            "sha256": trace.source_sha256,
            "trace_days": TRACE_DAYS,
            "rows_read": trace.rows_read,
            "positive_work_rows": trace.positive_work_rows,
            "zero_work_rows": trace.zero_work_rows,
            "negative_duration_rows": trace.negative_duration_rows,
            "outside_trace_rows": trace.outside_trace_rows,
            "daily_total_work_core_hours": [
                round(float(value), 6)
                for value in trace.daily_arrival_work.sum(axis=1)
            ],
            "included_source_days_one_based": included_days,
            "excluded_source_days": [
                {
                    "day_one_based": day,
                    "reason": (
                        "追踪起点状态快照：任务数仅为第2—8天均值的1.31%，"
                        "且绝大多数第0小时记录为Waiting，不代表正常到达日。"
                    ),
                }
                for day in excluded_days
            ],
            "daily_audit": "data/processed/workload/workload_daily_audit.json",
        },
        "parameters": {
            "days": args.days,
            "nominal_seed": args.seed,
            "block_days": args.block_days,
            "flex_window_hours": args.flex_window_hours,
            "target_total_work_core_hours": round(target_total_work, 6),
            "flex_window_sensitivity_hours": [2, 6, 12, 24],
            "training_scenario_count": args.training_scenario_count,
            "training_seed": args.seed + 1,
        },
        "nominal_scenario": {
            "scenario_id": nominal.scenario_id,
            "source_days_one_based": [
                included_days[day] for day in nominal.source_days
            ],
            "normalization_scale": round(nominal.normalization_scale, 12),
            "total_work_core_hours": round(float(nominal.arrival_work.sum()), 6),
            "sampling_rule": (
                "平衡使用所有保留日的两日循环块；完整轮次后仅随机补足剩余块，"
                "再随机排列块顺序。"
            ),
        },
        "training_scenarios": [
            {
                "scenario_id": scenario.scenario_id,
                "source_days_one_based": [
                    included_days[day] for day in scenario.source_days
                ],
                "normalization_scale": round(scenario.normalization_scale, 12),
                "total_work_core_hours": round(
                    float(scenario.arrival_work.sum()), 6
                ),
            }
            for scenario in training_scenarios
        ],
        "outputs": {args.nominal_out.name: file_sha256(args.nominal_out)},
    }
    if training_scenarios:
        manifest["outputs"][args.training_scenarios_out.name] = file_sha256(
            args.training_scenarios_out
        )
    args.manifest_out.parent.mkdir(parents=True, exist_ok=True)
    args.manifest_out.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    print(f"written: {args.nominal_out}")
    if training_scenarios:
        print(f"written: {args.training_scenarios_out}")
    print(f"written: {args.manifest_out}")
    print("source_rows:", trace.rows_read)
    print("included_source_days_one_based:", included_days)
    print("excluded_source_days_one_based:", excluded_days)
    print("nominal_hours:", args.days * HOURS_PER_DAY)
    print("training_scenario_count:", len(training_scenarios))
    print("flex_window_hours:", args.flex_window_hours)
    print("target_total_work_core_hours:", round(target_total_work, 2))


if __name__ == "__main__":
    main()
