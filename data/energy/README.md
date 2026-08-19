# ERCOT 2025 DAM 价格与 EIA ERCO 风光/碳原始数据

以下文件在 2026-08-15 下载，保留为未经变换的原始输入。它们服务于“Houston 负荷区成本信号 + ERCOT 系统级可再生能源与碳信号”的论文实验边界；不将系统级风光或碳排放解释为 Houston 本地能源数据。

| 文件 | 来源与内容 | SHA256 |
| --- | --- | --- |
| `ercot_2025_historical_dam_load_zone_and_hub_prices.zip` | [ERCOT Historical DAM Load Zone and Hub Prices](https://www.ercot.com/mp/data-products/data-product-details?id=np4-180-er) 的 2025 年公开年度归档；ZIP 内含 `rpt.00013060.0000000000000000.DAMLZHBSPP_2025.xlsx` | `30DF71EBB306BBE8C6CC075598D2E5BD47079B8AB9E0442979F3331353618320` |
| `eia_930_erco_full_history.xlsx` | [EIA Hourly Electric Grid Monitor](https://www.eia.gov/electricity/gridmonitor/about) 的 ERCO 平衡区完整历史工作簿 | `0EFF7C52C9014F83EDF83831C21C130E7055DD1DCCE24235369040EFE8AA41E0` |

这两个原始公开文件只保存在本地 `data/energy/`，由 `.gitignore` 明确排除，不进入 Git 提交。复现时重新下载后必须先核对上表 SHA-256；`ercot_2025_houston_hourly.csv` 才是纳入版本控制的共享正式输入。

ERCOT 工作簿按 `Jan` 至 `Dec` 分表，字段为 `Delivery Date`、`Hour Ending`、`Repeated Hour Flag`、`Settlement Point`、`Settlement Point Price`。`Settlement Point` 为 `LZ_HOUSTON` 的记录共有 8,760 个，覆盖 2025-01-01 01:00 至 2025-12-31 24:00，且价格字段无空值。正式处理阶段以该字段作为数据中心成本信号。

EIA 工作簿的 `Published Hourly Data` 表含 `UTC time`、`Local date`、`Local time`、`Time zone`、`Demand`、`NG: SUN`、`NG: WND` 和 `CO2 Emissions Intensity for Consumed Electricity` 等已发布列。正式处理阶段以 `UTC time` 作为跨数据源对齐的主时间索引，并显式处理时区与夏令时；不得跨当地日期通过全表行号拼接 ERCOT 与 EIA 数据。

## 共享年度表：ERCOT 2025 Houston

`ercot_2025_houston_hourly.csv` 是论文线与求职线共用的 2025 年小时能源表，由下列命令生成：

```powershell
conda run -n scip_env python scripts/prepare_ercot_2025_houston_energy.py
```

该文件有 8,760 行。主键 `timestamp_utc` 是 EIA 定义的小时结束 UTC 时刻，范围为 `2025-01-01T07:00:00Z` 至 `2026-01-01T06:00:00Z`。EIA 记录以 `Local date` 为 2025 年筛选条件；ERCOT 记录以 `Delivery Date` 为 2025 年筛选条件。每个当地日期先校验两来源的记录数相等，之后依来源内顺序一一配对。此规则保留春季短日的 23 小时和秋季长日的 25 小时；秋季两条 ERCOT `Hour Ending = 02:00` 记录的 `Repeated Hour Flag` 分别保留为 `N` 和 `Y`，并对应两个连续 UTC 时刻。

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

风、光和碳字段是 ERCOT 平衡区系统信号，不能表述为 Houston 本地发电或本地边际排放。源工作簿在以下 2025 小时未发布相应数值，年度表保留为空，绝不以零、均值或插值替代：

- `erco_consumed_co2_intensity_lbs_per_kwh`：72 小时，`2025-12-03T07:00:00Z` 至 `2025-12-06T06:00:00Z`；
- `erco_solar_generation_mwh`、`erco_wind_generation_mwh`：各 48 小时，`2025-12-04T07:00:00Z` 至 `2025-12-06T06:00:00Z`。

脚本只使用标准库读取 XLSX 容器，并在生成前校验两个原始文件的 SHA-256。论文窗口的快照及其输入哈希清单写入 `outputs/paper/alibaba2018_dro/day_ahead/inputs/`；这些快照不是求职线的输入数据。
