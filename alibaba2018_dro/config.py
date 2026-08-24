from __future__ import annotations

import datetime as dt


# 论文窗口结构：30 天核心期（720 小时），前后各 171 小时上下文/结算尾段
CORE_HOURS = 30 * 24
CONTEXT_HOURS = 171
TAIL_HOURS = 171
# 窗口文件名里的残留标识（d168 / h3），不是实际约束
MAX_BATCH_DURATION_HOURS = 168
COMPLETION_SLACK_HOURS = 3
# BESS 吞吐衰减成本：按累计充、放电总吞吐量计价，而非 CAPEX 年化。
# 这是透明的研究情景参数，不是市场报价；主结果取 20，敏感性取 10/20/40。
BESS_DEGRADATION_COST_USD_PER_MWH_THROUGHPUT = 20.0
BESS_DEGRADATION_COST_SENSITIVITIES_USD_PER_MWH_THROUGHPUT = (10.0, 20.0, 40.0)
# 风光储—碳预算主线的固定情景。容量是反事实本地资源装机，不是 ERCO 实际装机。
PHYSICAL_CAPACITY_CORES = 387_264
EFFECTIVE_REPLAY_CAPACITY_FRACTION = 0.70
EFFECTIVE_REPLAY_CAPACITY_SENSITIVITIES = (0.60, 0.70, 0.80)
# 2024 年 EIA ERCO 实际发电量 P99；仅用作固定本地资源形状缩放基准。
RESOURCE_REFERENCE_YEAR = 2024
SOLAR_REFERENCE_MWH = 20_101.19
WIND_REFERENCE_MWH = 25_748.17
PV_CAPACITY_FRACTION_OF_MUST_LOAD = 0.50
WIND_CAPACITY_FRACTION_OF_MUST_LOAD = 0.50
CARBON_BUDGET_REDUCTIONS = (0.00, 0.05, 0.10)
DEFAULT_CARBON_BUDGET_REDUCTION = 0.05
# 有效容量缩放不改变物理机队的 idle 功率；这是已登记的功率情景口径。
IDLE_POWER_BASIS = "fixed_physical_fleet"
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
