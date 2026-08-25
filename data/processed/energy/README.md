# 能源派生输入（processed/energy）

本目录保存由原始能源数据确定性生成的、纳入版本控制的共享输入：

- `ercot_2025_houston_hourly.csv`：共享 2025 年度小时表。
- `windows/`：四个 1062 小时无泄漏论文窗口及其输入清单。
- `residuals/`：2024 年风、光、碳逐小时联合残差及审计清单。

## 共享年度表：ERCOT 2025 Houston

`ercot_2025_houston_hourly.csv` 是项目共用的 2025 年小时能源表，共 8,760 行。主键 `timestamp_utc` 是 EIA 定义的小时结束 UTC 时刻，范围为 `2025-01-01T07:00:00Z` 至 `2026-01-01T06:00:00Z`。EIA 记录以 `Local date` 为 2025 年筛选；ERCOT 记录以 `Delivery Date` 为 2025 年筛选。每个当地日期先校验两来源记录数相等，再按来源内顺序一一配对。此规则保留春季短日的 23 小时和秋季长日的 25 小时；秋季两条 ERCOT `Hour Ending = 02:00` 记录的 `Repeated Hour Flag` 分别保留为 `N` 和 `Y`，并对应两个连续 UTC 时刻。

| 字段 | 含义 | 单位或格式 |
| --- | --- | --- |
| `timestamp_utc` | EIA 小时结束 UTC 时刻，唯一且递增 | `YYYY-MM-DDTHH:MM:SSZ` |
| `local_date` | EIA 报告当地日期 | `YYYY-MM-DD` |
| `local_hour` | EIA 当地日内连续小时号 | 1–25 |
| `local_time_end` | EIA 当地小时结束时刻 | `HH:MM:SS` |
| `delivery_date` | ERCOT DAM 交割日期 | `YYYY-MM-DD` |
| `hour_ending` | ERCOT DAM 小时结束标签 | `HH:MM` |
| `repeated_hour_flag` | ERCOT 秋季重复小时标志 | `N` 或 `Y` |
| `dam_lz_houston_usd_per_mwh` | `LZ_HOUSTON` DAM 结算点价格 | USD/MWh |
| `erco_solar_generation_mwh` | ERCO 报告的 `NG: SUN` | MWh |
| `erco_wind_generation_mwh` | ERCO 报告的 `NG: WND` | MWh |
| `erco_consumed_co2_intensity_lbs_per_kwh` | ERCO 消费侧碳强度 | lbs/kWh |

风、光和碳字段是 ERCOT 平衡区系统信号，不是 Houston 本地发电或本地边际排放。源工作簿未发布的 2025 小时在年度表里保持为空，绝不以零、均值或插值替代：

- `erco_consumed_co2_intensity_lbs_per_kwh`：72 小时，`2025-12-03T07:00:00Z` 至 `2025-12-06T06:00:00Z`；
- `erco_solar_generation_mwh`、`erco_wind_generation_mwh`：各 48 小时，`2025-12-04T07:00:00Z` 至 `2025-12-06T06:00:00Z`。

## 论文窗口（windows/）

由 `scripts/prepare_energy_inputs.py` 从共享年度表 + 2024 上下文 + EIA 全历史生成，每窗口包含 171 小时上下文、720 小时核心期、171 小时结算尾（共 1062 小时），并仅为核心期与结算尾写入无泄漏预测。

| 文件 | 内容 |
| --- | --- |
| `2025-01-01_30d_d168_h3_energy.csv` | 1 月窗口 |
| `2025-04-01_30d_d168_h3_energy.csv` | 4 月窗口 |
| `2025-07-01_30d_d168_h3_energy.csv` | 7 月窗口 |
| `2025-10-01_30d_d168_h3_energy.csv` | 10 月窗口 |
| `inputs_manifest.json` | schema 版本、窗口结构、来源与输出 SHA-256、2024 Ridge 验证结果 |

窗口 CSV 的正式列顺序与 `alibaba2018_dro/config.py` 的 `ENERGY_INPUT_COLUMNS` 一致；`period_role` 取值 `context` / `core` / `settlement_tail`。复现时用 `scripts/prepare_energy_inputs.py --source ...` 重新生成并核对 `inputs_manifest.json` 里的输出哈希。

Ridge 参数只用 2023 年全年逐日滚动起点选择；旧清单中的 12 个 2024 月度起点已经废弃，不得用于论文结论。2024 年全部可预测日期统一生成残差，并按月份分为 `fold_1={1,4,7,10}`、`fold_2={2,5,8,11}`、`fold_3={3,6,9,12}`。SAA、RO、DRO 必须使用相同留出折选择各自参数，选定后再用全部可用 2024 日块重拟合。

## 2024 联合残差（residuals/）

`joint_residuals_2024.csv` 共 8,760 行，其中 8,759 行三类实际值和预测值完整；形成 362 个可用 24 小时联合日块。审计清单 `residuals_manifest.json` 登记以下排除项：

| 交割日 | 原因 |
| --- | --- |
| 2024-03-10 | DST，23 小时 |
| 2024-03-28 | 一小时实际碳强度缺失 |
| 2024-03-31 | 最近 24 小时预测特征含上述缺失值，完整预测不可用 |
| 2024-11-03 | DST，25 小时 |

三个季节平衡折分别含 123、120、119 个可用日块。原始缺失值不插值、不补零；所有排除原因和输出 SHA-256 均保存在清单中。
