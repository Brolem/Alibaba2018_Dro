"""审计 Alibaba v2018 batch_task 的逐日完整性与时间边界。

本脚本只读原始文件，输出可复现的逐日审计 JSON。它不生成调度场景，
用于在重采样前识别左/右边界不完整日和异常持续时间记录。
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter, defaultdict
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RAW_WORKLOAD = ROOT / "data" / "raw" / "workload"
PROCESSED_WORKLOAD = ROOT / "data" / "processed" / "workload"
SECONDS_PER_HOUR = 3600
HOURS_PER_DAY = 24
TRACE_DAYS = 8


def audit_batch_task(path: Path, *, trace_days: int = TRACE_DAYS) -> dict:
    """流式统计每天的任务到达、工作量、状态和右边界跨越情况。"""

    trace_seconds = trace_days * HOURS_PER_DAY * SECONDS_PER_HOUR
    daily = [
        {
            "day": day + 1,
            "task_rows": 0,
            "positive_work_rows": 0,
            "instances": 0,
            "arriving_cores": 0.0,
            "work_core_hours": 0.0,
            "negative_duration_rows": 0,
            "zero_work_rows": 0,
            "end_after_trace_rows": 0,
            "end_after_trace_work_core_hours": 0.0,
            "first_start_second": None,
            "last_start_second": None,
            "hourly_task_rows": [0] * HOURS_PER_DAY,
            "hourly_work_core_hours": [0.0] * HOURS_PER_DAY,
            "status_counts": Counter(),
            "positive_work_rows_by_status": Counter(),
            "work_core_hours_by_status": defaultdict(float),
            "nonpositive_instance_rows": 0,
            "nonpositive_plan_cpu_rows": 0,
        }
        for day in range(trace_days)
    ]
    digest = hashlib.sha256()
    rows_read = 0
    malformed_rows = 0
    start_before_trace_rows = 0
    start_after_trace_rows = 0

    with path.open("rb") as input_file:
        for raw_line in input_file:
            digest.update(raw_line)
            rows_read += 1
            fields = raw_line.rstrip(b"\r\n").split(b",")
            if len(fields) < 9:
                malformed_rows += 1
                continue

            instance_num = int(fields[1]) if fields[1] else 0
            status = fields[4].decode("utf-8", errors="replace")
            start = int(fields[5])
            end = int(fields[6])
            plan_cpu = float(fields[7]) if fields[7] else 0.0
            if start < 0:
                start_before_trace_rows += 1
                continue
            if start >= trace_seconds:
                start_after_trace_rows += 1
                continue

            day_index, second_in_day = divmod(start, HOURS_PER_DAY * SECONDS_PER_HOUR)
            hour_in_day = second_in_day // SECONDS_PER_HOUR
            record = daily[day_index]
            record["task_rows"] += 1
            record["instances"] += instance_num
            record["arriving_cores"] += instance_num * plan_cpu / 100.0
            record["hourly_task_rows"][hour_in_day] += 1
            record["status_counts"][status] += 1
            if instance_num <= 0:
                record["nonpositive_instance_rows"] += 1
            if plan_cpu <= 0.0:
                record["nonpositive_plan_cpu_rows"] += 1
            if record["first_start_second"] is None:
                record["first_start_second"] = start
            else:
                record["first_start_second"] = min(record["first_start_second"], start)
            if record["last_start_second"] is None:
                record["last_start_second"] = start
            else:
                record["last_start_second"] = max(record["last_start_second"], start)

            if end < start:
                record["negative_duration_rows"] += 1
                continue
            duration_seconds = end - start
            work_core_hours = (
                instance_num
                * (plan_cpu / 100.0)
                * duration_seconds
                / SECONDS_PER_HOUR
            )
            if work_core_hours <= 0.0:
                record["zero_work_rows"] += 1
                continue

            record["positive_work_rows"] += 1
            record["work_core_hours"] += work_core_hours
            record["positive_work_rows_by_status"][status] += 1
            record["work_core_hours_by_status"][status] += work_core_hours
            record["hourly_work_core_hours"][hour_in_day] += work_core_hours
            if end > trace_seconds:
                record["end_after_trace_rows"] += 1
                record["end_after_trace_work_core_hours"] += work_core_hours

    serializable_daily: list[dict] = []
    for record in daily:
        task_rows = record["task_rows"]
        work = record["work_core_hours"]
        end_after_work = record["end_after_trace_work_core_hours"]
        serializable_daily.append(
            {
                **record,
                "arriving_cores": round(record["arriving_cores"], 6),
                "work_core_hours": round(work, 6),
                "mean_work_core_hours_per_task": round(
                    work / task_rows if task_rows else 0.0,
                    6,
                ),
                "active_arrival_hours": sum(
                    count > 0 for count in record["hourly_task_rows"]
                ),
                "end_after_trace_work_fraction": round(
                    end_after_work / work if work else 0.0,
                    8,
                ),
                "end_after_trace_work_core_hours": round(end_after_work, 6),
                "hourly_work_core_hours": [
                    round(value, 6) for value in record["hourly_work_core_hours"]
                ],
                "status_counts": dict(sorted(record["status_counts"].items())),
                "positive_work_rows_by_status": dict(
                    sorted(record["positive_work_rows_by_status"].items())
                ),
                "work_core_hours_by_status": {
                    status: round(value, 6)
                    for status, value in sorted(
                        record["work_core_hours_by_status"].items()
                    )
                },
            }
        )

    retained_days = serializable_daily[1:]
    retained_work = [day["work_core_hours"] for day in retained_days]
    retained_tasks = [day["task_rows"] for day in retained_days]
    retained_status_work: defaultdict[str, float] = defaultdict(float)
    for day in retained_days:
        for status, value in day["work_core_hours_by_status"].items():
            retained_status_work[status] += value
    retained_total_work = sum(retained_work)
    return {
        "method": "batch_task_daily_boundary_audit_v2",
        "source": {
            "path": path.resolve().relative_to(ROOT.resolve()).as_posix(),
            "sha256": digest.hexdigest(),
            "rows_read": rows_read,
            "malformed_rows": malformed_rows,
            "start_before_trace_rows": start_before_trace_rows,
            "start_after_trace_rows": start_after_trace_rows,
            "trace_days": trace_days,
        },
        "daily": serializable_daily,
        "boundary_ratios": {
            "day1_to_days2_8_mean_tasks": round(
                serializable_daily[0]["task_rows"]
                / (sum(retained_tasks) / len(retained_tasks)),
                8,
            ),
            "day1_to_days2_8_mean_work": round(
                serializable_daily[0]["work_core_hours"]
                / (sum(retained_work) / len(retained_work)),
                8,
            ),
        },
        "retained_days_2_8": {
            "total_work_core_hours": round(retained_total_work, 6),
            "mean_daily_work_core_hours": round(
                retained_total_work / len(retained_days),
                6,
            ),
            "work_core_hours_by_status": {
                status: round(value, 6)
                for status, value in sorted(retained_status_work.items())
            },
            "work_fraction_by_status": {
                status: round(value / retained_total_work, 8)
                for status, value in sorted(retained_status_work.items())
            },
            "nonpositive_instance_rows": sum(
                day["nonpositive_instance_rows"] for day in retained_days
            ),
            "nonpositive_plan_cpu_rows": sum(
                day["nonpositive_plan_cpu_rows"] for day in retained_days
            ),
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--source",
        type=Path,
        default=RAW_WORKLOAD / "batch_task.csv",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=PROCESSED_WORKLOAD / "workload_daily_audit.json",
    )
    args = parser.parse_args()

    result = audit_batch_task(args.source)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"written: {args.out}")
    print("source_sha256:", result["source"]["sha256"])
    print("rows_read:", result["source"]["rows_read"])
    print("day1_to_days2_8_mean_tasks:", result["boundary_ratios"]["day1_to_days2_8_mean_tasks"])
    print("day1_to_days2_8_mean_work:", result["boundary_ratios"]["day1_to_days2_8_mean_work"])


if __name__ == "__main__":
    main()
