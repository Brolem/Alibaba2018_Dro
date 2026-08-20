# 数据中心算电协同调度研究：顶层设计文档

> 状态：研究方案已定稿，代码与实验已推进到四窗口 + 双侧鲁棒 + 样本外回测。
> 本文件是项目的顶层设计文档：先回答“做什么、为什么”，再说明“系统怎么分层、数据怎么流动、目录怎么组织”，最后给出方法、评价与红线。实现口径见 `docs/design.md`，数学公式见 `docs/model.md`，实验结果见 `docs/results.md`。

## 1. 一句话摘要

在**不确定条件**下，对一个**同址本地光伏（PV）+ 电池储能（BESS）**的**数据中心**做**日前算电协同调度**：用真实生产集群 trace 中的**可延迟批处理工作负载**作为算力侧柔性资源，以**分布鲁棒/鲁棒优化（DRO/RO）**处理**算力侧（作业到达/时长）与能源侧（可再生/碳）的双侧不确定**，在**严格无泄漏、样本外标定**的评估下，量化**购电成本、购电量、碳排放、尖峰/爬坡**的下降，并如实报告能量回弹校正。

## 2. 研究定位与目标

- 研究方向：**不确定条件下的优化**；论文**偏调度**；课题组传统路线是“本地新能源 + 储能”的算电协同。
- 发表目标：**两个月内投出第一篇论文**，**对分区要求不高**（Energy Reports / Energies / Electronics / Frontiers in Energy Research，或中文 EI）。
- 论文要体现的核心结果：**优化后购电成本下降、购电量下降、碳排放下降、尖峰/爬坡改善、PV 自用率提升**。

## 3. 研究问题

**主问题**：在真实批处理/在线混合负载与异质服务器组成的同址数据中心中，如何用**从 trace 标定的可延迟批处理柔性** + **本地 PV/BESS**，在**可再生与作业到达/时长双侧不确定**下，做**日前分布鲁棒调度**，最小化购电成本并降低碳排放，同时保证在线服务与批处理截止期？

**三个可证伪命题**：

1. 与静态/无能源感知基线相比，风险感知的批处理延迟调度可降低购电成本，且不违反任何在线服务或批处理 deadline。
2. 在预注册成本容忍度内，加入 PV/BESS 与碳信号可进一步降低购电量与碳排，且四季节方向一致。
3. 相比“只考虑可再生不确定”，**联合考虑算力侧（作业到达/时长）与能源侧不确定**的 DRO 能更稳地满足服务约束，代价是更高的保守性——这一保守性—可靠性折中可被量化。

## 4. 创新点

1. **从 trace 标定可延迟窗口（不拍脑袋设 deadline）**：用 `task_type`、`task_name` 的 DAG 依赖与观测到的起止时间，反推每类批处理作业的 release/deadline 与可延迟量，而非假设固定 slack。
2. **双侧不确定**：把“作业到达率、执行时长、可延迟窗口”与“可再生出力/碳强度”一起纳入同一个 DRO/机会约束，区别于文献里只做单侧（能源）不确定。
3. **无泄漏 + 样本外标定**：模糊集/Γ 预算只用 2024 残差标定，2025 四窗口做纯样本外评估；日前决策只读 48h 保护前数据，实际风光/碳只用于事后评价；并量化 in-sample 标定会高估多少收益。
4. **能量回弹 + 服务器 base/idle 功率**：延迟批处理不是凭空消失，须满足能量守恒（回弹）；建模服务器空闲/基座功率，只有延迟后能关停空闲机才真正降功率。

## 5. 数据方案（原始 / 处理 / 结果分层）

本仓库把数据分成三层，分别放 `data/raw/`、`data/processed/`、`data/results/`，边界见 `data/README.md`：

- **原始数据（raw）**：不可变、可重新下载的公开来源文件；大文件不入库。
- **处理后/派生输入（processed）**：由原始数据确定性生成的、纳入版本控制的可复现输入。
- **实验结果（results）**：调度与回测产出的指标/表，可复现但可重新生成。

### 5.1 算力侧：Alibaba cluster-trace-v2018

- 文件：`batch_task.csv`、`container_meta.csv`、`machine_meta.csv`（可选 `batch_instance.csv`）。
- 划分：**批处理（可延迟）** 来自 `batch_task`/`batch_instance`；**在线服务（LRA，必须满足）** 来自 `container_meta`/`container_usage`。划分按“表”，不是某个 `task_type` 标志。
- 关键字段：`batch_task` 的 `task_name`（含 DAG 信息）、`instance_num`、`job_name`、`task_type`（1–12 类任务类型）、`status`、`start_time`/`end_time`、`plan_cpu`/`plan_mem`。
- 用途：用 `start_time/end_time` 反推 release/deadline；用 `task_name` 的 `M/R+数字` 结构重建 DAG；到达/时长用于双侧不确定分布。

### 5.2 能源/碳侧

- 电价：ERCOT `LZ_HOUSTON` 日前电价（2025）。
- 碳：EIA ERCO **消费侧**碳强度（平均，非边际）。
- 本地 PV：**反事实场景**，用 ERCO 系统太阳预测形状 × 场景容量构造（非 NSRDB/PVWatts 实测本地剖面；原 2020 剖面已移除）。
- BESS：纯场景参数（功率/能量/效率/SOC），无需天气数据。

### 5.3 不确定性残差

- 能源侧：2024 预测残差，来自 EIA 全历史（`data/raw/energy/eia_930_erco_full_history.xlsx`，覆盖 2015-07→2026-08）。当前能源侧 PV 相对误差用已提交 `inputs_manifest.json` 的 2024 Ridge `ridge_nmae ≈ 0.243`；更严格的“95 分位数相对误差”需要额外落盘带符号逐小时残差（现有代码只存了聚合 MAE，属后续增强项）。
- 算力侧：从 v2018 的作业到达/时长经验分布估计。

### 5.4 明确不用的数据（及原因）

- **Alibaba 2026 Spot GPU**：Spot 柔性仅占核心容量约 1.9%（HP 占主导），算力侧柔性杠杆太小。
- **预打包“Multi-Scale Workflow Scheduling and Energy Data”**：最多 1000 jobs、规模过小；完整数据需邮件作者；柔性由 DAG 固定。
- **DCcluster-Opt / sustain-cluster**：为 RL / 地理分布式设计，与单 DC 的 DRO/RO MILP 不匹配。
- **Google 2019 完整版**：有 `scheduling_class`/`priority`，但 2.4 TiB、JSON/protobuf，两个月处理偏重。

## 6. 数据契约与字段映射

| v2018 表 | 关键字段（真实 schema，CSV 无表头） | 派生到模型 |
| --- | --- | --- |
| `batch_task.csv` | `task_name`, `instance_num`, `job_name`, `task_type`, `status`, `start_time`, `end_time`, `plan_cpu`, `plan_mem` | 可延迟批处理作业：release/deadline、资源需求、DAG 依赖 |
| `container_meta.csv` | `container_id`, `machine_id`, `time_stamp`, `app_du`, `status`, `cpu_request`, `cpu_limit`, `mem_size` | 在线服务（LRA）负载：必须满足的 CPU/内存请求 |
| `machine_meta.csv` | `machine_id`, `time_stamp`, `failure_domain_1`, `failure_domain_2`, `cpu_num`, `mem_size`, `status` | 服务器容量（`cpu_num`、归一化 `mem_size`） |
| 能源表 | `dam_lz_houston_usd_per_mwh`, `erco_solar_generation_mwh`, `erco_wind_generation_mwh`, `erco_consumed_co2_intensity_lbs_per_kwh` | 电价、系统风光、碳强度（本地 PV 为反事实场景） |

柔性包络生成：以 `batch_task`（或 `batch_instance`）为可延迟作业集合，由 `start_time/end_time` 得到观测执行区间，反推 deadline（如 `end_time` 或 `start_time + 观测时长 + 标定 slack`），汇总为每小时的“可调度功率上/下界”。`container_meta` 的在线负载不参与延迟。

## 7. 系统架构与模块分层

按“数据 → 输入/预测 → 调度 → 回测/结果”四层组织：

1. **数据层**：`data/raw`（原始）、`data/processed`（派生输入）、`data/results`（结果）。
2. **输入/预测层**（`alibaba2018_dro`）：
   - `eia_history.py`：只依赖标准库读取 EIA-930 XLSX 与 ERCOT DAM 归档。
   - `forecasting.py`：48 小时信息保护下的滚动 Ridge 预测器（numpy）。
   - `energy.py`：把价格/风光/碳组合成 1062 小时无泄漏论文窗口并写哈希清单。
   - `inputs.py`：把能源窗口 + workload 柔性包络 + 在线负荷对齐成统一小时输入（MW）。
3. **调度层**：`scheduler.py`：日前调度 LP（批处理平移 + BESS + PV + 双侧 Γ-budget RO + SAA），用 PySCIPOpt。
4. **回测/结果层**（`scripts/`）：扫参、四窗口、对照基线、样本外回测与绘图，写入 `data/results/` 与 `docs/figures/`。

## 8. 数据流

```text
原始能源/EIA（data/raw/energy）
        │  eia_history + forecasting + energy
        ▼
共享年度表 + 1062h 窗口（data/processed/energy）
        │
        └──► inputs.build_hourly_input ──┐
                                          ├──► scheduler（LP/DRO/SAA）──► data/results
batch_task（data/raw/workload）          │
        │  analyze / build_envelope / generate_workload
        ▼
柔性包络 + 在线核数（data/processed/workload）──┘
```

即：`raw → processed → scheduler → results`；预测与不确定集标定只用 2024 及保护期前数据，2025 四窗口做样本外评价。

## 9. 目录结构

```text
Alibaba2018_Dro/
├── README.md                      # 顶层设计文档（本文件）
├── alibaba2018_dro/               # Python 主包
│   ├── config.py                  # 窗口/预测/功率场景常量
│   ├── eia_history.py             # EIA XLSX / ERCOT DAM 读取（标准库）
│   ├── forecasting.py             # 48h 保护 Ridge 预测器
│   ├── energy.py                  # 无泄漏论文窗口输入构造
│   ├── inputs.py                  # 能源 + workload → 统一小时输入
│   └── scheduler.py               # 日前调度 LP（PySCIPOpt）
├── scripts/                       # 实验脚本（见 docs/README.md）
├── tests/test_energy_inputs.py    # 能源输入/预测合同测试
├── docs/                          # 详细文档与论文图表
│   ├── design.md                  # 实现设计记录
│   ├── model.md                   # 建模公式与映射
│   ├── forecasting.md             # 无泄漏预测设计
│   ├── results.md                 # 实验结果
│   ├── paper_tables_figures.md    # 论文图表清单
│   └── figures/
└── data/
    ├── README.md                  # 原始/处理/结果分层说明
    ├── raw/                       # 原始数据（大文件不入库）
    │   ├── energy/
    │   └── workload/
    ├── processed/                 # 处理后/派生输入（入库）
    │   ├── energy/
    │   └── workload/
    └── results/                   # 实验结果（入库）
```

## 10. 方法（模型）

- 形式：**两阶段/滚动** MILP。第一阶段定批处理作业的 gang/启停、BESS 充放电、购电；第二阶段在实现的不确定下再调度。
- 不确定处理：**预算不确定集 RO / DRO**，名义值用 48h 保护的 Ridge 预测，模糊集/Γ 用 2024 残差标定；已实现逐小时 Bertsimas–Sim 鲁棒对偶与 SAA 对照。
- 目标：**词典序（两次顺序求解）**，先 `min 购电成本`，再在 1% 成本保护带内 `min 碳`；风光匹配降级为报告指标（PV 自用率/弃电率）。
- 约束：在线服务必须满足、批处理满足 deadline、能量守恒（回弹）、BESS SOC、功率平衡、**并网功率上限**与**爬坡上限**（作为扫参场景，报告可行性边界）。
- 功率模型：服务器能耗 = 利用率相关功率 + base/idle 功率；延迟后允许关停空闲机。完整公式见 `docs/model.md`。

## 11. 评价指标与诚实边界

主指标：**购电成本（USD）**。辅助：**购电量、碳排放（kgCO₂）、尖峰功率、并网爬坡、PV 自用率、批处理完成率/延迟、求解时间/间隙**。

诚实边界（写进论文，避免被拒）：

- 调度与弹性 **不减少 IT 总能耗**（作业总计算量不变），减少的是**购电量/购电成本/碳/尖峰**。
- 本地 PV/BESS 是**反事实场景**，不是实测资产；尺寸按规则设定并做敏感性。
- 碳用**平均消费侧碳强度**，**不写边际碳**。
- 柔性由 trace 的 `task_type`/起止时间标定，**不虚构 SLO**。
- v2018 只有 8 天，四季节方向一致性是“情景方向一致”，**不是统计显著性**。

## 12. 发表需求与顾虑（及对应处理）

| 顾虑 | 处理 |
| --- | --- |
| 原数据集柔性占比太低（约 2%） | 换 Alibaba v2018，在线/批处理划分，柔性占比大得多 |
| 并网/爬坡约束可能不可行 | 把 `G_max`/`R_max` 做成扫参 + 报告“最小 BESS 需求/可行性边界”，不做死约束 |
| 2024 残差不可直接用 | 从本包 EIA 全历史落盘逐小时带符号残差；系统风光残差可作本地 PV 代理 |
| “能耗下降”会被审稿人反驳 | 改为“购电量/购电成本/碳排放/尖峰下降”，并加能量回弹 + base power 校正 |
| 怕成“套模板” | 创新点压在 trace 标定柔性 + 双侧不确定 + 无泄漏标定 + 回弹校正 |
| 两个月、低分区 | 用成熟 DRO/RO MILP，方法不冒险；先文档后代码；场景规模控制 |

## 13. 里程碑

1. 第 1 周：写顶层设计与实现设计（研究问题/数据合同/方法/基线/指标）。
2. 第 2–3 周：实现 Alibaba v2018 的 workload 解析 + 柔性标定 + 双侧不确定，写失败测试。
3. 第 4–5 周：DRO/RO 调度器 + PV/BESS + 并网/爬坡扫参，跑通一个窗口。
4. 第 6–7 周：四窗口/敏感性 + 写作、图表、复现附录、投稿。

## 14. 红线与非目标

- 不把系统级风光写成数据中心本地风电/光伏；不把消费侧碳写成边际碳。
- 不虚构在线服务的 SLO 或可延迟量；不把 8 天窗口结果写成显著性证据。
- 不声称“生产规模真实数据”（v2018 是 8 天、约 4000 台，属 case study 级别）。

## 15. 未决事项

- 已确认：`task_type` 为 12 类、`task_name` 承载 DAG；可延迟能量占比静态代理约 58.7%；真实 deadline slack 仍需 `batch_instance` 标定。
- 已落地：本地 PV 用 ERCO 系统太阳形状 × 场景容量（2025 本地 NSRDB 剖面仅在审稿要求或时间富余时重拉）。
- 已落地：双侧 Γ-budget RO、逐小时 Bertsimas–Sim 鲁棒、SAA 三种不确定处理；求解器 PySCIPOpt（`scip_env`）。
- 待定：投稿目标（英文低分区 or 中文 EI），据此决定能源侧是否换成国内电网数据。

## 16. 文档索引

- `docs/design.md`：实现设计（数据接口、功率模型、不确定集标定、主实验配置）。
- `docs/model.md`：建模公式与映射（LaTeX）。
- `docs/forecasting.md`：无泄漏时序预测设计。
- `docs/results.md`：实验结果。
- `docs/paper_tables_figures.md`：论文图表清单。
- `data/README.md` 与各层 `README.md`：原始/处理/结果数据的来源与口径。

## 17. 启动提示

- 主包名 `alibaba2018_dro`，直接 `import alibaba2018_dro.forecasting`；脚本 `scripts/*.py` 会把项目根加入 `sys.path`。
- Alibaba v2018 三表已下载校验；`batch_task.csv`（约 802 MB）与原始 `.tar.gz` 已 gitignore，`container_meta.csv`、`machine_meta.csv` 较小、暂入库。
- 能源输入测试：`python -m unittest tests.test_energy_inputs`（12/12 通过，覆盖 48h 保护、窗口结构、schema、哈希清单与缺值不插补）。
- 调度求解：`scheduler.py` 需 `scip_env`（PySCIPOpt）；其余模块 numpy / 标准库即可。
