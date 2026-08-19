"""对 Alibaba cluster-trace-v2018 做只读统计，产出可延迟柔性特征。

只用已下载的 batch_task.csv / container_meta.csv / machine_meta.csv，
不依赖 machine_usage / container_usage。所有“能量/占比”均为资源需求口径的
静态代理，不是实测能耗；真实利用率需要 usage 表。

输出：data/workload/workload_stats.json
"""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter, defaultdict
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKLOAD = ROOT / "data" / "workload"

DAG_RE = re.compile(r"^[A-Z]\d+(_\d+)*$")
SECONDS_PER_HOUR = 3600
NOMINAL_HOURS = 8 * 24  # 8 天


def _quantile_from_hist(hist: dict[int, int], q: float) -> float:
    """从以小时为 bin 的直方图近似分位数（小时）。"""
    total = sum(hist.values())
    if total == 0:
        return 0.0
    target = total * q
    cum = 0
    for hour in sorted(hist):
        cum += hist[hour]
        if cum >= target:
            return float(hour)
    return float(max(hist))


def analyze_batch_task() -> dict:
    path = WORKLOAD / "batch_task.csv"
    by_type = defaultdict(
        lambda: {
            "tasks": 0,
            "instances": 0,
            "core_seconds": 0.0,
            "plan_cpu_sum": 0.0,
            "plan_mem_sum": 0.0,
            "duration_seconds_sum": 0,
            "duration_seconds_valid": 0,
        }
    )
    duration_hist: dict[int, int] = defaultdict(int)
    arrival: dict[int, dict] = defaultdict(
        lambda: {"tasks": 0, "arriving_cores": 0.0}
    )
    dag_style = 0
    independent = 0
    other_names: Counter[str] = Counter()

    total_tasks = 0
    total_instances = 0
    unique_jobs: set[str] = set()
    deferrable_core_seconds = 0.0
    negative_duration = 0
    duration_sum = 0
    duration_valid = 0
    max_duration = 0

    with path.open("r", encoding="utf-8", newline="") as f:
        for line in f:
            parts = line.rstrip("\r\n").split(",")
            # 顺序：task_name, instance_num, job_name, task_type, status,
            #       start_time, end_time, plan_cpu, plan_mem
            task_name = parts[0]
            instance_num = int(parts[1]) if parts[1] else 0
            job_name = parts[2]
            tt = parts[3]
            start_time = int(parts[5])
            end_time = int(parts[6])
            plan_cpu = float(parts[7]) if parts[7] else 0.0
            plan_mem = float(parts[8]) if parts[8] else 0.0

            total_tasks += 1
            total_instances += instance_num
            unique_jobs.add(job_name)

            duration = end_time - start_time
            if duration < 0:
                negative_duration += 1
                duration = 0
            max_duration = max(max_duration, duration)
            duration_sum += duration
            duration_valid += 1
            duration_hist[duration // SECONDS_PER_HOUR] += 1

            # plan_cpu 的 100 = 1 核；instance_num 为实例数
            core_seconds = (plan_cpu / 100.0) * instance_num * duration
            deferrable_core_seconds += core_seconds

            rec = by_type[tt]
            rec["tasks"] += 1
            rec["instances"] += instance_num
            rec["core_seconds"] += core_seconds
            rec["plan_cpu_sum"] += plan_cpu
            rec["plan_mem_sum"] += plan_mem
            rec["duration_seconds_sum"] += duration
            rec["duration_seconds_valid"] += 1

            hour = start_time // SECONDS_PER_HOUR
            arrival[hour]["tasks"] += 1
            arrival[hour]["arriving_cores"] += (plan_cpu / 100.0) * instance_num

            if DAG_RE.match(task_name):
                dag_style += 1
            elif task_name.startswith("task_"):
                independent += 1
            else:
                other_names[task_name] += 1

    task_type_out = {
        k: {
            **v,
            "mean_duration_hours": (
                (v["duration_seconds_sum"] / v["duration_seconds_valid"])
                / SECONDS_PER_HOUR
                if v["duration_seconds_valid"]
                else 0.0
            ),
        }
        for k, v in sorted(by_type.items())
    }

    return {
        "total_tasks": total_tasks,
        "total_instances": total_instances,
        "unique_jobs": len(unique_jobs),
        "negative_duration_tasks": negative_duration,
        "max_duration_hours": max_duration / SECONDS_PER_HOUR,
        "duration": {
            "mean_hours": (duration_sum / duration_valid) / SECONDS_PER_HOUR
            if duration_valid
            else 0.0,
            "median_hours": _quantile_from_hist(duration_hist, 0.50),
            "p90_hours": _quantile_from_hist(duration_hist, 0.90),
        },
        "task_type": task_type_out,
        "dag_classification": {
            "dag_style": dag_style,
            "independent_task_prefix": independent,
            "other": sum(other_names.values()),
            "other_top_names": other_names.most_common(20),
        },
        "arrival_hourly": {
            str(h): dict(v) for h, v in sorted(arrival.items())
        },
        "deferrable_core_seconds": deferrable_core_seconds,
    }


def analyze_online_and_machines() -> dict:
    container_max: dict[str, tuple[int, int, int, int]] = {}
    container_machines: set[str] = set()
    with (WORKLOAD / "container_meta.csv").open("r", encoding="utf-8", newline="") as f:
        for line in f:
            parts = line.rstrip("\r\n").split(",")
            # container_id, machine_id, time_stamp, app_du, status,
            # cpu_request, cpu_limit, mem_size
            cid = parts[0]
            machine_id = parts[1]
            ts = int(parts[2])
            cpu_request = int(parts[5])
            cpu_limit = int(parts[6])
            container_machines.add(machine_id)
            prev = container_max.get(cid)
            if prev is None:
                container_max[cid] = (cpu_request, cpu_limit, ts, ts)
            else:
                container_max[cid] = (
                    max(prev[0], cpu_request),
                    max(prev[1], cpu_limit),
                    min(prev[2], ts),
                    max(prev[3], ts),
                )

    machine_cpu: dict[str, int] = {}
    with (WORKLOAD / "machine_meta.csv").open("r", encoding="utf-8", newline="") as f:
        for line in f:
            parts = line.rstrip("\r\n").split(",")
            # machine_id, time_stamp, fd1, fd2, cpu_num, mem_size, status
            machine_id = parts[0]
            cpu_num = int(parts[4])
            machine_cpu[machine_id] = max(machine_cpu.get(machine_id, 0), cpu_num)

    online_reserved_cores = sum(v[0] for v in container_max.values()) / 100.0
    online_limit_cores = sum(v[1] for v in container_max.values()) / 100.0
    total_machine_cores = sum(machine_cpu.values())

    return {
        "distinct_containers": len(container_max),
        "distinct_machines_in_container_meta": len(container_machines),
        "distinct_machines_in_machine_meta": len(machine_cpu),
        "total_machine_cores": total_machine_cores,
        "online_reserved_cores": online_reserved_cores,
        "online_limit_cores": online_limit_cores,
        "online_static_reservation_ratio": (
            online_reserved_cores / total_machine_cores
            if total_machine_cores
            else 0.0
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--out",
        type=Path,
        default=WORKLOAD / "workload_stats.json",
    )
    args = parser.parse_args()

    batch = analyze_batch_task()
    online = analyze_online_and_machines()

    deferrable_core_seconds = batch["deferrable_core_seconds"]
    online_core_seconds = online["online_reserved_cores"] * (
        NOMINAL_HOURS * SECONDS_PER_HOUR
    )
    deferrable_share = (
        deferrable_core_seconds / (deferrable_core_seconds + online_core_seconds)
        if (deferrable_core_seconds + online_core_seconds) > 0
        else 0.0
    )

    result = {
        "note": (
            "资源需求口径的静态代理，未使用 machine_usage/container_usage；"
            "在线负载按 8 天全时段在线的静态预留口径估计，不是实测能耗。"
        ),
        "nominal_horizon_hours": NOMINAL_HOURS,
        "batch_task": batch,
        "online_static": online,
        "deferrable_share_proxy": {
            "deferrable_core_seconds": deferrable_core_seconds,
            "online_core_seconds": online_core_seconds,
            "deferrable_share": deferrable_share,
        },
    }

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"written: {args.out}")
    print("total_tasks:", result["batch_task"]["total_tasks"])
    print("deferrable_share_proxy:", round(result["deferrable_share_proxy"]["deferrable_share"], 4))
    print(
        "online_static_reservation_ratio:",
        round(result["online_static"]["online_static_reservation_ratio"], 4),
    )


if __name__ == "__main__":
    main()
