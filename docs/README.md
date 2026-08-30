# 项目文件导航与分工

## 端到端数据流

```mermaid
flowchart LR
    WRAW["data/raw/workload/*.csv"] --> GW["scripts/generate_workload.py"]
    GW --> W30["nominal_workload_30d.csv"]
    GW --> WTR["compute_training_scenarios_30d.csv"]
    GW --> WM["nominal_workload_manifest.json / workload_stats.json"]

    ERAW["data/raw/energy/*"] --> PE["scripts/prepare_energy_inputs.py"]
    PE --> EH["eia_history.py"]
    PE --> FC["forecasting.py"]
    PE --> EN["energy.py"]
    PE --> RS["residuals.py"]
    EN --> EWIN["processed/energy/windows/*.csv"]
    RS --> JR["joint_residuals_2024.csv"]

    JR --> PS["scripts/prepare_saa_scenarios.py"]
    WTR --> PS
    WM --> PS
    PS --> SC["scenarios.py"]
    SC --> CB["calibration_day_blocks_2024.csv"]
    SC --> SM["saa_scenarios_manifest.json"]

    EWIN --> IN["inputs.py → HourlyInput"]
    W30 --> IN
    WM --> IN
    IN --> DET["run_four_windows.py"]
    DET --> SCH["scheduler.py"]
    SCH --> DRES["four_windows_mainline_summary.csv"]

    CB --> UNC["run_uncertainty_methods.py"]
    SM --> UNC
    W30 --> UNC
    WM --> UNC
    UNC --> LOAD["scenarios.py → ScenarioRealization"]
    UNC --> BUILD["inputs.py → HourlyInput"]
    LOAD --> DSAA["scheduler.py → 分解 SAA / 三类运行风险场景回放"]
    BUILD --> DSAA
    DSAA --> URES["run_config.json / saa_cv_runs.csv / summary / selection"]
```

### 流程如何由代码实现

| 阶段 | 入口与核心调用 | 输入 | 输出 |
| --- | --- | --- | --- |
| 1. workload 审计 | `audit_workload_days.py`、`analyze_workload.py` | Alibaba 原始 trace | 日级审计与容量统计 |
| 2. workload 构造 | `generate_workload.py::aggregate_trace` → `generate_nominal_scenario` / `generate_scenarios` | `batch_task.csv` 与固定随机种子 | 30 天名义负荷、训练场景、累计释放/截止包络、manifest |
| 3. 能源历史读取 | `prepare_energy_inputs.py` → `eia_history.load_erco_history` / `load_houston_dam_prices` | EIA-930、ERCOT DAM | 对齐的历史风光、碳强度和价格 |
| 4. 无泄漏预测 | `forecasting.select_ridge_alpha` → `forecast_delivery_dates` | 历史能源序列 | 日前预测及 2023 参数选择证据 |
| 5. 能源窗口与残差 | `energy.write_study_inputs`；`residuals.write_joint_residuals` | 预测、实际值、价格 | 论文窗口 CSV、2024 联合残差和季节折 |
| 6. SAA 场景登记 | `prepare_saa_scenarios.py` → `scenarios.write_calibration_day_blocks` / `write_saa_scenario_manifest` | 联合残差、DAM、workload 场景 | 校准日块表与可重建 manifest |
| 7. 模型输入 | `inputs.build_hourly_input` 或 `build_hourly_input_from_rows` | 能源窗口、名义 workload、容量统计 | 每小时 `HourlyInput` |
| 8. 场景重建 | `scenarios.load_saa_scenarios` | manifest、校准表、workload 源日 | 联合 `ScenarioRealization` 序列 |
| 9. 确定性实验 | `run_four_windows.py` → `scheduler.solve_wind_solar_storage` → `replay_actual_wind_solar` | `HourlyInput` | 确定性日前计划与实际回放结果 |
| 10. SAA 实验 | `run_uncertainty_methods.py` → `solve_decomposed_saa_wind_solar_storage` | `HourlyInput` + 联合场景 | 活动场景主问题、全场景回放与三类运行风险 |
| 11. 校准选择 | `summarize_saa_runs` → `select_saa_sample_size` | 每窗口结果 CSV | 每个 N 完成 36 窗口后计算 Wilson 上界；首个达标 N 写入选择 JSON 并停止 |

定位问题时从结果文件的 `run_config.json` 和 manifest 哈希反向追踪：结果目录 → 运行脚本 → `inputs.py`/`scenarios.py` → processed 数据 → 对应准备脚本 → raw 数据。不要直接手工修改中间 CSV 来修正模型结果。

## 核心代码

| 文件 | 用途 |
| --- | --- |
| `../alibaba2018_dro/config.py` | 公共实验参数、资源容量和时间窗口常量；碳预算常量仅供历史诊断复现 |
| `../alibaba2018_dro/eia_history.py` | 读取 EIA-930 与 ERCOT DAM 历史数据 |
| `../alibaba2018_dro/forecasting.py` | 带 48 小时保护的能源预测模型 |
| `../alibaba2018_dro/energy.py` | 构造年度能源表与论文窗口输入 |
| `../alibaba2018_dro/inputs.py` | 把能源、容量和 workload 包络对齐为小时模型输入 |
| `../alibaba2018_dro/residuals.py` | 生成风、光、碳联合残差日块与季节折 |
| `../alibaba2018_dro/scenarios.py` | 读取 manifest 并重建 SAA 训练、验证和回放场景 |
| `../alibaba2018_dro/scheduler.py` | 确定性/SAA 日前优化、三类运行风险追索与实际回放；碳排放事后核算 |

更详细的数据流见 `../alibaba2018_dro/README.md`。`scheduler.py` 使用 PySCIPOpt；碳排放 LP 对偶割另外使用 SciPy/HiGHS。

## 执行脚本

| 文件 | 用途 |
| --- | --- |
| `../scripts/analyze_workload.py` | 审计原始 workload 并统计容量、工作量和时间特征 |
| `../scripts/audit_workload_days.py` | 检查逐日 workload 数据完整性与异常 |
| `../scripts/generate_workload.py` | 生成名义 workload、场景池和累计柔性包络 |
| `../scripts/plot_aggregate_workload.py` | 绘制聚合 workload |
| `../scripts/plot_resampled_workload.py` | 绘制重采样 workload 场景 |
| `../scripts/prepare_energy_inputs.py` | 生成无泄漏预测、能源窗口和输入清单 |
| `../scripts/prepare_saa_scenarios.py` | 生成 2024 校准日块表与 SAA manifest |
| `../scripts/run_four_windows.py` | 运行确定性四窗口基线 |
| `../scripts/run_uncertainty_methods.py` | 按 N 自适应运行 SAA 三折分解、验证、Wilson 汇总与最小达标样本量选择 |

## 验证代码

| 文件 | 主要检查 |
| --- | --- |
| `../tests/test_energy_inputs.py` | 数据时间切分、预测保护、窗口和价格输入 |
| `../tests/test_energy_residuals.py` | 联合残差、季节折和缺失日处理 |
| `../tests/test_mainline_scheduler.py` | 优化约束、BESS、SAA 追索和碳对偶割 |
| `../tests/test_scenarios.py` | 校准表与 manifest 可重建性 |
| `../tests/test_uncertainty_calibration.py` | Wilson 门槛、汇总和样本选择规则 |
| `../tests/test_workload_scenarios.py` | 工作量守恒、柔性包络和名义场景平衡 |

## 数据与结果目录

| 路径 | 用途 |
| --- | --- |
| `../data/raw/` | 原始来源数据；不手工改写 |
| `../data/processed/` | 由准备脚本生成的清洗数据、场景和 manifest |
| `../data/results/` | 实验结果、运行配置和预检证据 |
| `../data/results/calibration/` | 不确定性方法校准与各阶段预检；不同配置使用独立子目录 |

## 文档索引

各文档职责与“给谁看”：

| 文档 | 职责 | 给谁看 |
| --- | --- | --- |
| `../README.md` | 已确认研究主线、数据边界、实验设计与当前实现状态 | 所有人 |
| `design.md` | 目标模型的实现边界、数据接口与迁移顺序 | 复现 / 维护 |
| `model.md` | 成本目标、风光储和联合不确定性的主模型数学公式 | 写论文 / 实现 |
| `compute_envelope.md` | 算力包络、有效容量、单位转换及对应代码数据流的完整推导 | 写论文 / 读代码 |
| `implementation_log.md` | 每次代码实现、执行命令、失败原因、改进措施与验证证据 | 复现 / 维护 |
| `results.md` | 旧 PV+BESS 开发原型结果档案，不是主线论文结果 | 回归对照 |
| `paper_tables_figures.md` | 主线完成后应生成的论文图表与统一报告口径 | 写论文 |
| `forecasting.md` | 无泄漏预测、联合残差和实际回放口径 | 预测 / 回测 |
| `../data/README.md` | 原始/处理/结果三层数据边界 | 复现 |
| `../data/raw/energy/README.md` | 能源原始数据来源与哈希 | 复现 |
| `../data/raw/workload/README.md` | Alibaba v2018 下载与字段 | 复现 |
| `../data/processed/energy/README.md` | 能源派生输入（年度表 + 窗口） | 复现 |
| `../data/processed/workload/README.md` | 算力侧派生输入（包络/不确定集） | 复现 |
| `../data/results/README.md` | 实验结果文件与复现命令 | 写论文 / 复现 |
| `../alibaba2018_dro/README.md` | 代码模块职责与数据流 | 读代码 |

分工原则：

- `../README.md` = “做什么、为什么、研究边界和当前状态”；
- `design.md` = “目标如何实现、当前差距和实施顺序”；
- `model.md` = “目标数学模型与不确定性定义”；
- `compute_envelope.md` = “算力包络和容量如何从数据推导并由代码实现”；
- `implementation_log.md` = “每次实际改了什么、如何验证、失败后如何改进”；
- `results.md` = “旧原型跑了什么，仅可作回归对照”。

当前确定性主线的机器可读结果保存在 `../data/results/four_windows_mainline_summary.csv`；联合不确定性方法完成同口径比较后，再将正式论文结果写入 `results.md`。公式只放 `model.md`。

文件管理规则：

1. 优先修改职责相符的现有文件，不为单次实验复制模型或说明文档。
2. 每次实现必须更新 `implementation_log.md`；数学定义稳定后才更新 `model.md`，阶段能力变化时更新 `design.md`。
3. 生成数据放入 `data/processed/`，实验输出放入 `data/results/`；每种预检配置使用独立结果目录，不覆盖历史证据。
4. 正式实验结果至少保留运行配置、随机种子或 manifest、输入哈希和结果表；失败预检必须标明不可用于论文结论。
5. 同一事实只在一处详细维护，其它文件用链接和一句摘要导航。
