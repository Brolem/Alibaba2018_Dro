"""绘制所有方法共用的 30 天名义聚合工作量。"""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

import matplotlib
import numpy as np


matplotlib.use("Agg")
import matplotlib.pyplot as plt


ROOT = Path(__file__).resolve().parents[1]
PROCESSED_WORKLOAD = ROOT / "data" / "processed" / "workload"
FIGURES = ROOT / "docs" / "figures"
HOURS_PER_DAY = 24


def load_nominal_workload(path: Path) -> np.ndarray:
    """从长表读取唯一名义负荷的逐小时到达工作量。"""

    selected: list[tuple[int, float]] = []
    with path.open("r", encoding="utf-8", newline="") as input_file:
        for row in csv.DictReader(input_file):
            if int(row["scenario_id"]) != 0:
                raise ValueError("nominal workload must contain only scenario_id=0")
            selected.append(
                (int(row["hour"]), float(row["arrival_work_core_hours"]))
            )
    selected.sort(key=lambda item: item[0])
    if not selected:
        raise ValueError(f"nominal workload is empty: {path}")
    expected_hours = list(range(len(selected)))
    if [hour for hour, _ in selected] != expected_hours:
        raise ValueError("nominal workload hours must be contiguous from zero")
    if len(selected) % HOURS_PER_DAY:
        raise ValueError("scenario horizon must contain complete 24-hour days")
    return np.asarray([work for _, work in selected], dtype=np.float64)


def load_nominal_manifest(path: Path) -> tuple[list[int], dict]:
    """读取公共名义负荷的来源日序列和生成参数。"""

    manifest = json.loads(path.read_text(encoding="utf-8"))
    match = manifest["nominal_scenario"]
    if int(match["scenario_id"]) != 0:
        raise ValueError("nominal manifest must use scenario_id=0")
    return [int(day) for day in match["source_days_one_based"]], manifest[
        "parameters"
    ]


def plot_nominal_workload(
    path: Path,
    hourly_work: np.ndarray,
    *,
    source_days: list[int],
    block_days: int,
    flex_window_hours: int,
) -> None:
    """绘制30天小时序列和逐日工作量，使用论文双栏宽度与300 DPI。"""

    days = len(hourly_work) // HOURS_PER_DAY
    if len(source_days) != days:
        raise ValueError("manifest source-day count does not match scenario horizon")
    hourly_million = hourly_work / 1_000_000.0
    daily_million = hourly_work.reshape(days, HOURS_PER_DAY).sum(axis=1) / 1_000_000.0
    trace_days = np.arange(len(hourly_work)) / HOURS_PER_DAY

    matplotlib.rcParams.update(
        {
            "font.family": "sans-serif",
            "font.sans-serif": ["Arial", "Helvetica", "DejaVu Sans"],
            "font.size": 9,
            "axes.titlesize": 10,
            "axes.labelsize": 9,
            "xtick.labelsize": 8,
            "ytick.labelsize": 8,
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
        figsize=(6.9, 5.25),
        gridspec_kw={"height_ratios": [2.5, 1.35], "hspace": 0.38},
    )

    hourly_axis.plot(trace_days, hourly_million, color="#0077BB", linewidth=0.8)
    hourly_axis.fill_between(
        trace_days,
        hourly_million,
        color="#0077BB",
        alpha=0.14,
        linewidth=0,
    )
    for boundary in range(block_days, days, block_days):
        hourly_axis.axvline(
            boundary,
            color="#777777",
            linewidth=0.45,
            alpha=0.55,
        )
    hourly_axis.set_xlim(0.0, float(days))
    hourly_axis.set_ylim(bottom=0.0)
    hourly_axis.set_xticks(np.arange(0, days + 1, 5))
    hourly_axis.set_xlabel("Synthetic experiment day")
    hourly_axis.set_ylabel("Arriving workload\n(million core-hours)")
    hourly_axis.set_title("(a) Hourly aggregate workload arrivals")
    hourly_axis.grid(axis="y", color="#D0D0D0", linewidth=0.45)

    day_numbers = np.arange(1, days + 1)
    daily_axis.bar(
        day_numbers,
        daily_million,
        width=0.78,
        color="#009988",
        linewidth=0,
    )
    daily_axis.set_xlim(0.25, days + 0.75)
    daily_axis.set_ylim(0.0, float(daily_million.max()) * 1.08)
    daily_axis.set_xticks(np.arange(1, days + 1, 3))
    daily_axis.set_xlabel("Synthetic experiment day")
    daily_axis.set_ylabel("Daily total\n(million core-hours)")
    daily_axis.set_title("(b) Daily aggregate workload")
    daily_axis.grid(axis="y", color="#D0D0D0", linewidth=0.45)

    figure.suptitle(
        "Shared Thirty-Day Nominal Workload Used by All Methods",
        fontsize=11,
        y=0.995,
    )
    figure.text(
        0.5,
        0.008,
        f"Balanced two-day circular blocks over complete source days; fixed total workload; "
        f"H = {flex_window_hours} h is excluded from arrival workload.",
        ha="center",
        va="bottom",
        fontsize=7.5,
        color="#4D4D4D",
    )
    figure.subplots_adjust(top=0.91, bottom=0.13, left=0.13, right=0.985)
    path.parent.mkdir(parents=True, exist_ok=True)
    figure.savefig(path)
    plt.close(figure)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--nominal",
        type=Path,
        default=PROCESSED_WORKLOAD / "nominal_workload_30d.csv",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=PROCESSED_WORKLOAD / "nominal_workload_manifest.json",
    )
    parser.add_argument(
        "--figure-out",
        type=Path,
        default=FIGURES / "fig_nominal_workload_30d.png",
    )
    args = parser.parse_args()

    hourly_work = load_nominal_workload(args.nominal)
    source_days, parameters = load_nominal_manifest(args.manifest)
    plot_nominal_workload(
        args.figure_out,
        hourly_work,
        source_days=source_days,
        block_days=int(parameters["block_days"]),
        flex_window_hours=int(parameters["flex_window_hours"]),
    )

    print(f"written: {args.figure_out}")
    print("nominal_scenario_id: 0")
    print("hours:", len(hourly_work))
    print("total_work_core_hours:", round(float(hourly_work.sum()), 6))
    print("source_days_one_based:", source_days)


if __name__ == "__main__":
    main()
