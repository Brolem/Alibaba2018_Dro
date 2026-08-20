from __future__ import annotations

import datetime as dt


# 论文窗口结构：30 天核心期（720 小时），前后各 171 小时上下文/结算尾段
CORE_HOURS = 30 * 24
CONTEXT_HOURS = 171
TAIL_HOURS = 171
# 窗口文件名里的残留标识（d168 / h3），不是实际约束
MAX_BATCH_DURATION_HOURS = 168
COMPLETION_SLACK_HOURS = 3
# 层级目标第二层的成本保护带：C ≤ C* + 1%·max(|C*|, 1 USD)
COST_GUARDRAIL_FRACTION = 0.01
# 预测器超参：90 天滚动历史、48 小时信息保护期、28 天同小时基线、2024 验证
FORECAST_HISTORY_DAYS = 90
FORECAST_INFORMATION_PROTECTION_HOURS = 48
FORECAST_BASELINE_DAYS = 28
FORECAST_VALIDATION_YEAR = 2024
RIDGE_ALPHAS = (0.01, 0.1, 1.0, 10.0, 100.0)
FORECAST_METHOD = "direct_ridge_90d_v1"
# 四个固定 2025 主窗口（1/4/7/10 月）
PAPER_WINDOW_STARTS = (
    dt.date(2025, 1, 1),
    dt.date(2025, 4, 1),
    dt.date(2025, 7, 1),
    dt.date(2025, 10, 1),
)

ENERGY_INPUT_COLUMNS = (
    "window_id",
    "window_hour",
    "period_role",
    "interval_start_utc",
    "interval_end_utc",
    "local_date",
    "dam_lz_houston_usd_per_mwh",
    "erco_solar_generation_mwh",
    "erco_wind_generation_mwh",
    "erco_consumed_co2_intensity_lbs_per_kwh",
    "forecast_cutoff_utc",
    "forecast_method",
    "forecast_erco_solar_generation_mwh",
    "forecast_erco_wind_generation_mwh",
    "forecast_consumed_co2_lbs_per_kwh",
)

N_MACHINES = 4034

# 功率模型：本实验无功率遥测，按 2018 年代约 96 核生产服务器的公开区间设定。
# 主结果用 "base"，low/high 做敏感性，不声称实测功率。
POWER_SCENARIOS = {
    "low": {"pue": 1.10, "idle_w_per_machine": 100.0, "active_w_per_core": 2.0},
    "base": {"pue": 1.20, "idle_w_per_machine": 150.0, "active_w_per_core": 3.0},
    "high": {"pue": 1.40, "idle_w_per_machine": 200.0, "active_w_per_core": 4.0},
}
