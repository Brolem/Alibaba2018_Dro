# 能源原始数据（raw/energy）

以下文件是 2026-08-15 前后下载、未经变换的公开原始输入。它们服务于“Houston 负荷区成本信号 + ERCOT 系统级可再生能源与碳信号”的论文实验边界；不把系统级风光或碳排放解释为 Houston 本地能源数据。

| 文件 | 来源与用途 | SHA-256 |
| --- | --- | --- |
| `eia_930_erco_full_history.xlsx` | [EIA Hourly Electric Grid Monitor](https://www.eia.gov/electricity/gridmonitor/about) 的 ERCO 平衡区完整历史工作簿；用于构造无泄漏预测与事后评价 | `0EFF7C52C9014F83EDF83831C21C130E7055DD1DCCE24235369040EFE8AA41E0` |
| `ercot_2025_historical_dam_load_zone_and_hub_prices.zip` | [ERCOT Historical DAM Load Zone and Hub Prices](https://www.ercot.com/mp/data-products/data-product-details?id=np4-180-er) 的 2025 年公开年度归档；用于生成共享年度表 | `30DF71EBB306BBE8C6CC075598D2E5BD47079B8AB9E0442979F3331353618320` |
| `ercot_2024_historical_dam_load_zone_and_hub_prices.zip` | 同上，2024 年归档；用于构造 2025 年 1 月窗口的 2024-12 上下文 | `B9FD0B9AA9EC83376C6385C91416174857CA6BA556C6DF08E942A2F98B89AF65` |

这些原始公开文件只保存在本地 `data/raw/energy/`，由 `.gitignore` 明确排除，不进入 Git 提交。复现时重新下载后必须核对上表 SHA-256；派生结果见 `data/processed/energy/README.md`。

## ERCOT 工作簿结构

ERCOT 工作簿按 `Jan` 至 `Dec` 分表，字段为 `Delivery Date`、`Hour Ending`、`Repeated Hour Flag`、`Settlement Point`、`Settlement Point Price`。`Settlement Point` 为 `LZ_HOUSTON` 的记录共有 8,760 个，覆盖 2025-01-01 01:00 至 2025-12-31 24:00，且价格字段无空值。正式处理阶段以该字段作为数据中心成本信号。

## EIA 工作簿结构

EIA 工作簿的 `Published Hourly Data` 表含 `UTC time`、`Local date`、`Local time`、`Time zone`、`Demand`、`NG: SUN`、`NG: WND` 和 `CO2 Emissions Intensity for Consumed Electricity` 等已发布列。正式处理阶段以 `UTC time` 作为跨数据源对齐的主时间索引，并显式处理时区与夏令时；不得跨当地日期通过全表行号拼接 ERCOT 与 EIA 数据。

风、光和碳字段是 ERCOT 平衡区系统信号，不能表述为 Houston 本地发电或本地边际排放。
