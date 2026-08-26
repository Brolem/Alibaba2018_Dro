"""绘制 Alibaba v2018 原始 8 天聚合批处理工作量。

负荷口径与算力场景生成器一致：先将每条任务转换为 core-hour 工作量，
再按任务开始小时聚合。柔性窗口不参与本图，避免把可调度存量误解为瞬时负荷。
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

import matplotlib
import numpy as np


matplotlib.use("Agg")
import matplotlib.pyplot as plt


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.generate_workload import RAW_WORKLOAD, TRACE_DAYS, aggregate_trace


PROCESSED_WORKLOAD = ROOT / "data" / "processed" / "workload"
FIGURES = ROOT / "docs" / "figures"


def write_hourly_csv(path: Path, daily_work: np.ndarray) -> None:
    """保存 8 x 24 聚合工作量，供后续绘图复用与数值核验。"""

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as output_file:
        writer = csv.writer(output_file)
        writer.writerow(
            ["trace_hour", "day", "hour_of_day", "arrival_work_core_hours"]
        )
        for day in range(daily_work.shape[0]):
            for hour_of_day in range(daily_work.shape[1]):
                writer.writerow(
                    [
                        day * 24 + hour_of_day,
                        day + 1,
                        hour_of_day,
                        round(float(daily_work[day, hour_of_day]), 6),
                    ]
                )


def plot_workload(path: Path, daily_work: np.ndarray) -> None:
    """绘制逐小时曲线及逐日总工作量。"""

    hourly_million = daily_work.reshape(-1) / 1_000_000.0
    daily_million = daily_work.sum(axis=1) / 1_000_000.0
    trace_hours = np.arange(hourly_million.size)
    day_centers = np.arange(TRACE_DAYS) * 24 + 11.5

    plt.rcParams.update(
        {
            "font.family": "sans-serif",
            "font.sans-serif": ["Arial", "Helvetica", "DejaVu Sans"],
            "font.size": 10,
            "axes.titlesize": 12,
            "axes.labelsize": 10,
            "xtick.labelsize": 9,
            "ytick.labelsize": 9,
            "figure.dpi": 300,
            "savefig.dpi": 300,
            "savefig.bbox": "tight",
            "axes.spines.top": False,
            "axes.spines.right": False,
        }
    )
    figure, (hourly_axis, daily_axis) = plt.subplots(
        2,
        1,
        figsize=(11.5, 6.8),
        gridspec_kw={"height_ratios": [3.0, 1.35], "hspace": 0.38},
    )

    hourly_axis.axvspan(-0.5, 23.5, color="#EE7733", alpha=0.10, linewidth=0)
    hourly_axis.plot(trace_hours, hourly_million, color="#0077BB", linewidth=1.25)
    hourly_axis.fill_between(
        trace_hours,
        hourly_million,
        color="#0077BB",
        alpha=0.16,
        linewidth=0,
    )
    for boundary in range(24, TRACE_DAYS * 24, 24):
        hourly_axis.axvline(boundary - 0.5, color="#9aa1a8", linewidth=0.7)
    hourly_axis.set_xlim(-0.5, TRACE_DAYS * 24 - 0.5)
    hourly_axis.set_xticks(day_centers, [f"Day {day}" for day in range(1, 9)])
    hourly_axis.set_ylabel("Arriving workload\n(million core-hours)")
    hourly_axis.set_title("Hourly aggregate batch-workload arrivals")
    hourly_axis.grid(axis="y", color="#d5d8dc", linewidth=0.6)
    hourly_axis.text(
        11.5,
        hourly_axis.get_ylim()[1] * 0.93,
        "Excluded boundary day",
        ha="center",
        va="top",
        fontsize=8,
        color="#9A3412",
    )

    bars = daily_axis.bar(
        np.arange(1, TRACE_DAYS + 1),
        daily_million,
        width=0.68,
        color=["#EE7733"] + ["#009988"] * (TRACE_DAYS - 1),
    )
    daily_axis.set_xticks(np.arange(1, TRACE_DAYS + 1))
    daily_axis.set_xlabel("Trace day")
    daily_axis.set_ylabel("Daily total\n(million core-hours)")
    daily_axis.set_title("Daily aggregate workload")
    daily_axis.grid(axis="y", color="#d5d8dc", linewidth=0.6)
    daily_axis.bar_label(bars, fmt="%.2f", padding=3, fontsize=8)
    daily_axis.set_ylim(0.0, max(daily_million) * 1.18)

    figure.suptitle(
        "Alibaba Cluster Trace v2018: Eight-Day Aggregate Batch Workload",
        fontsize=14,
        y=0.995,
    )
    figure.text(
        0.5,
        0.01,
        "Workload = instance_num × (plan_cpu / 100) × observed duration / 3600; "
        "grouped by task start hour. Day 1 is excluded from resampling after boundary audit; "
        "flexibility window is excluded.",
        ha="center",
        va="bottom",
        fontsize=8.5,
        color="#4d5156",
    )
    figure.subplots_adjust(top=0.91, bottom=0.12, left=0.105, right=0.98)
    path.parent.mkdir(parents=True, exist_ok=True)
    figure.savefig(path)
    plt.close(figure)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--source",
        type=Path,
        default=RAW_WORKLOAD / "batch_task.csv",
    )
    parser.add_argument(
        "--csv-out",
        type=Path,
        default=PROCESSED_WORKLOAD / "aggregate_workload_8d.csv",
    )
    parser.add_argument(
        "--figure-out",
        type=Path,
        default=FIGURES / "fig_aggregate_workload_8d.png",
    )
    args = parser.parse_args()

    trace = aggregate_trace(args.source)
    write_hourly_csv(args.csv_out, trace.daily_arrival_work)
    plot_workload(args.figure_out, trace.daily_arrival_work)

    print(f"written: {args.csv_out}")
    print(f"written: {args.figure_out}")
    print("source_sha256:", trace.source_sha256)
    print("rows_read:", trace.rows_read)
    print(
        "daily_total_work_core_hours:",
        [round(float(value), 2) for value in trace.daily_arrival_work.sum(axis=1)],
    )


if __name__ == "__main__":
    main()
