# 数据中心算电协同调度研究：项目规格与数据说明

> 状态：研究方案已定稿（算力侧由 Alibaba 2026 Spot GPU 改为 Alibaba cluster-trace-v2018）
> 用途：新项目入口文档。读这一份即可了解研究问题、数据、方法、创新点、发表约束与待办。

## 0. 一句话摘要

在**不确定条件**下，对一个**同址本地光伏（PV）+ 电池储能（BESS）**的**数据中心**做**日前算电协同调度**：用真实生产集群 trace 中的**可延迟批处理工作负载**作为算力侧柔性资源，以**分布鲁棒/鲁棒优化（DRO/RO）**处理**算力侧（作业到达/时长）与能源侧（可再生/碳）的双侧不确定**，在**严格无泄漏、样本外标定**的评估下，量化**购电成本、购电量、碳排放、尖峰/爬坡**的下降，并如实报告能量回弹校正。

## 1. 研究定位与目标

- 研究方向：**不确定条件下的优化**；论文**偏调度**；课题组传统路线是“本地新能源 + 储能”的算电协同。
- 发表目标：**两个月内投出第一篇论文**，**对分区要求不高**（Energy Reports / Energies / Electronics / Frontiers in Energy Research，或中文 EI）。
- 论文要体现的核心结果：**优化后购电成本下降、购电量下降、碳排放下降、尖峰/爬坡改善、PV 自用率提升**。

## 2. 研究问题（定稿）

**主问题**：在真实批处理/在线混合负载与异质服务器组成的同址数据中心中，如何用**从 trace 标定的可延迟批处理柔性** + **本地 PV/BESS**，在**可再生与作业到达/时长双侧不确定**下，做**日前分布鲁棒调度**，最小化购电成本并降低碳排放，同时保证在线服务与批处理截止期？

**三个可证伪命题**：

1. 与静态/无能源感知基线相比，风险感知的批处理延迟调度可降低购电成本，且不违反任何在线服务或批处理 deadline。
2. 在预注册成本容忍度内，加入 PV/BESS 与碳信号可进一步降低购电量与碳排，且四季节方向一致。
3. 相比“只考虑可再生不确定”，**联合考虑算力侧（作业到达/时长）与能源侧不确定**的 DRO 能更稳地满足服务约束，代价是更高的保守性——这一保守性—可靠性折中可被量化。

## 3. 创新点（四个细节）

1. **从 trace 标定可延迟窗口（不拍脑袋设 deadline）**：用 `task_type`/优先级与观测到的起止/调度延迟，反推每类批处理作业的 release/deadline 与可延迟量，而非假设固定 slack。
2. **双侧不确定**：把“作业到达率、执行时长、可延迟窗口”与“可再生出力/碳强度”一起纳入同一个 DRO/机会约束，区别于文献里只做单侧（能源）不确定。
3. **无泄漏 + 样本外标定**：模糊集/Γ 预算只用 2024 残差标定，2025 四窗口做纯样本外评估；日前决策只读 48h 保护前数据，实际风光/碳只用于事后评价；并量化 in-sample 标定会高估多少收益。
4. **能量回弹 + 服务器 base/idle 功率**：延迟批处理不是凭空消失，须满足能量守恒（回弹）；建模服务器空闲/基座功率，只有延迟后能关停空闲机才真正降功率。

## 4. 数据方案（最终选择）

### 4.1 算力侧：Alibaba cluster-trace-v2018

- 文件：`batch_task.csv`、`container_meta.csv`、`machine_meta.csv`（可选 `batch_instance.csv`）。
- 划分：**批处理（可延迟）** 来自 `batch_task`/`batch_instance`；**在线服务（LRA，必须满足）** 来自 `container_meta`/`container_usage`。划分按“表”，不是某个 `task_type` 标志。
- 关键字段：`batch_task` 的 `task_name`（含 DAG 信息）、`instance_num`、`job_name`、`task_type`（1–12 类任务类型）、`status`、`start_time`/`end_time`、`plan_cpu`/`plan_mem`。
- 用途：用 `start_time/end_time` 反推 release/deadline；用 `task_name` 的 `M/R+数字` 结构重建 DAG；到达/时长用于双侧不确定分布。

### 4.2 能源/碳侧（本包已含）

- 电价：ERCOT `LZ_HOUSTON` 日前电价（2025）。
- 碳：EIA ERCO **消费侧**碳强度（平均，非边际）。
- 本地 PV：NSRDB + PVWatts 的 Houston 本地剖面（需按 2025 重拉；原 2020 剖面已移除）。
- BESS：纯场景参数（功率/能量/效率/SOC），无需天气数据。

### 4.3 不确定性残差

- 能源侧：2024 预测残差，来自 EIA 全历史（本包 `data/energy/eia_930_erco_full_history.xlsx`，覆盖 2015-07→2026-08），需要**额外落盘带符号逐小时残差**（现有代码只存了聚合 MAE）。
- 算力侧：从 v2018 的作业到达/时长经验分布估计。

### 4.4 明确不用的数据（及原因）

- **Alibaba 2026 Spot GPU**：Spot 柔性仅占核心容量约 1.9%（HP 占主导），算力侧柔性杠杆太小。
- **预打包“Multi-Scale Workflow Scheduling and Energy Data”**：最多 1000 jobs、规模过小；完整数据需邮件作者；柔性由 DAG 固定。
- **DCcluster-Opt / sustain-cluster**：为 RL / 地理分布式设计，与单 DC 的 DRO/RO MILP 不匹配。
- **Google 2019 完整版**：有 `scheduling_class`/`priority`，但 2.4 TiB、JSON/protobuf，两个月处理偏重；本仓库里的 `google_2019_28d_5min.csv` 只有 `avg_cpu/avg_mem/avg_assigned_mem/avg_cycles_per_instruction` 四列。

## 5. 数据契约与字段映射

| v2018 表 | 关键字段（真实 schema，CSV 无表头） | 派生到模型 |
| --- | --- | --- |
| `batch_task.csv` | `task_name`, `instance_num`, `job_name`, `task_type`, `status`, `start_time`, `end_time`, `plan_cpu`, `plan_mem` | 可延迟批处理作业：release/deadline、资源需求、DAG 依赖 |
| `container_meta.csv` | `container_id`, `machine_id`, `time_stamp`, `app_du`, `status`, `cpu_request`, `cpu_limit`, `mem_size` | 在线服务（LRA）负载：必须满足的 CPU/内存请求 |
| `machine_meta.csv` | `machine_id`, `time_stamp`, `failure_domain_1`, `failure_domain_2`, `cpu_num`, `mem_size`, `status` | 服务器容量（`cpu_num`、归一化 `mem_size`） |
| 能源表 | `dam_lz_houston_usd_per_mwh`, `erco_solar_generation_mwh`, `erco_wind_generation_mwh`, `erco_consumed_co2_intensity_lbs_per_kwh` | 电价、系统风光、碳强度（本地 PV 待 2025 重拉） |

柔性包络生成：以 `batch_task`（或 `batch_instance`）为可延迟作业集合，由 `start_time/end_time` 得到观测执行区间，反推 deadline（如 `end_time` 或 `start_time + 观测时长 + 标定 slack`），汇总为每小时的“可调度功率上/下界”。`container_meta` 的在线负载不参与延迟。

## 6. 方法（模型）

- 形式：**两阶段/滚动** MILP。第一阶段定批处理作业的 gang/启停、BESS 充放电、购电；第二阶段在实现的不确定下再调度。
- 不确定处理：**预算不确定集 RO 或 DRO（机会约束）**，名义值用 48h 保护的 Ridge 预测，模糊集/Γ 用 2024 残差标定。
- 目标：词典序或加权 `min 购电成本 + λ·碳`（并约束 PV 自用率）。
- 约束：在线服务必须满足、批处理满足 deadline、能量守恒（回弹）、BESS SOC、功率平衡、**并网功率上限**与**爬坡上限**（作为扫参场景，报告可行性边界）。
- 功率模型：服务器能耗 = 利用率相关功率 + base/idle 功率；延迟后允许关停空闲机。

## 7. 评价指标与诚实边界

主指标：**购电成本（USD）**。辅助：**购电量、碳排放（kgCO₂）、尖峰功率、并网爬坡、PV 自用率、批处理完成率/延迟、求解时间/间隙**。

诚实边界（写进论文，避免被拒）：

- 调度与弹性 **不减少 IT 总能耗**（作业总计算量不变），减少的是**购电量/购电成本/碳/尖峰**。
- 本地 PV/BESS 是**反事实场景**，不是实测资产；尺寸按规则设定并做敏感性。
- 碳用**平均消费侧碳强度**，**不写边际碳**。
- 柔性由 trace 的 `task_type`/起止时间标定，**不虚构 SLO**。
- v2018 只有 8 天，四季节方向一致性是“情景方向一致”，**不是统计显著性**。

## 8. 发表需求与顾虑（及对应处理）

| 顾虑 | 处理 |
| --- | --- |
| 原数据集柔性占比太低（约 2%） | 换 Alibaba v2018，在线/批处理划分，柔性占比大得多 |
| 并网/爬坡约束可能不可行 | 把 `G_max`/`R_max` 做成扫参 + 报告“最小 BESS 需求/可行性边界”，不做死约束 |
| 2024 残差不可直接用 | 从本包 EIA 全历史落盘逐小时带符号残差；系统风光残差可作本地 PV 代理 |
| “能耗下降”会被审稿人反驳 | 改为“购电量/购电成本/碳排放/尖峰下降”，并加能量回弹 + base power 校正 |
| 怕成“套模板” | 创新点压在 trace 标定柔性 + 双侧不确定 + 无泄漏标定 + 回弹校正 |
| 两个月、低分区 | 用成熟 DRO/RO MILP，方法不冒险；先文档后代码；场景规模控制 |

## 9. 两个月里程碑

1. 第 1 周：写 `design.md`（研究问题/数据合同/方法/基线/指标）。
2. 第 2–3 周：实现 Alibaba v2018 的 workload 解析 + 柔性标定 + 双侧不确定，写失败测试。
3. 第 4–5 周：DRO/RO 调度器 + PV/BESS + 并网/爬坡扫参，跑通一个窗口。
4. 第 6–7 周：四窗口/敏感性 + 写作、图表、复现附录、投稿。

## 10. 红线与非目标

- 不把系统级风光写成数据中心本地风电/光伏；不把消费侧碳写成边际碳。
- 不虚构在线服务的 SLO 或可延迟量；不把 8 天窗口结果写成显著性证据。
- 不声称“生产规模真实数据”（v2018 是 8 天、约 4000 台，属 case study 级别）。

## 11. 未决事项（待确认）

1. 统计 `batch_task`（12 类 `task_type`）的可延迟能量占比与 `task_name` 的 DAG 依赖结构，确定柔性包络。
2. 确定本地 PV 用 NSRDB/PVWatts 2025 重拉（原 2020 剖面已移除）。
3. 确定 DRO 具体形式（预算 RO / 机会约束 DRO / Wasserstein DRO）与求解器。
4. 确定投稿目标（英文低分区 or 中文 EI），据此决定能源侧是否换成国内电网数据。

## 12. 本包内容清单

```text
alibaba2018_dro_bundle/
├── README.md                          # 本文档
├── docs/
│   └── 因果预测设计.md                  # 48h 保护因果预测设计
├── experiments/alibaba2018_dro/
│   ├── __init__.py
│   ├── config.py                       # 窗口/预测常量
│   ├── eia_history.py                  # 读 EIA XLSX（标准库，无依赖）
│   ├── energy.py                       # ERCOT/EIA 输入构造
│   └── forecasting.py                  # 48h 保护 Ridge 预测器
├── scripts/
│   ├── prepare_alibaba2018_dro_inputs.py            # 能源输入物化脚本
│   ├── analyze_v2018_workload.py                    # workload 流式统计
│   └── build_compute_uncertainty_envelope.py        # 算力侧不确定集与柔性包络
├── tests/alibaba2018_dro/test_inputs.py             # 输入合同测试
└── data/
    ├── energy/
    │   ├── ercot_2025_houston_hourly.csv          # 2025 DAM 价 + EIA 风光/碳
    │   ├── eia_930_erco_full_history.xlsx         # EIA 全历史（预测/残差用，gitignore）
    │   ├── windows/                               # 4 个 1062h 窗口输入 + manifest
    │   └── README.md                              # 数据来源与哈希
    └── workload/
        ├── README.md                              # Alibaba v2018 下载与字段说明
        ├── machine_meta.csv / container_meta.csv / batch_task.csv
        └── workload_stats.json / compute_uncertainty.json / hourly_flexibility_envelope.csv
```

## 13. 启动提示

- 代码包名已统一为 `alibaba2018_dro`，可直接 `import experiments.alibaba2018_dro.forecasting`。
- 算力侧 Alibaba v2018 已下载并校验（`machine_meta` / `container_meta` / `batch_task`；`batch_task.csv` 较大，已 gitignore）。
- `test_inputs.py` 在本环境依赖的 XLSX 读取库可能需要先安装 `openpyxl`（或改用包内标准库 `eia_history.iter_xlsx_rows`）后再跑。
