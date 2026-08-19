# 实现设计记录（scheduler 数据接口、功率模型与基线 MILP）

> 本文记录“代码实现层”的设计与口径，研究问题/创新点见 README。随实现推进持续更新。

## 1. 数据接口与单位口径

### 1.1 统一小时索引

- 能源窗口：4 个 ERCOT 2025 窗口，每个 30 天核心期 720 小时（另有 171h 上下文、171h 结算尾，调度只用核心期）。
- 算力侧：由 8 天 `batch_task` 用蒙特卡洛生成器扩展为 30 天 720 小时柔性包络（见 §2）。
- 两个 720 小时序列按小时对齐，`timestamp_utc` 以能源窗口 `interval_end_utc` 为准。

### 1.2 批处理需求：用 `baseline_energy_core_hours`，不用 `baseline_cores`

- `baseline_cores` = 该小时所有重叠任务的 `plan_cpu/100 × instance_num` 之和（把每个任务按整小时满功率计入），短任务会被高估，甚至超过集群核数，不能当功率用。
- `baseline_energy_core_hours` = 各任务 `(plan_cpu/100) × instance_num × 重叠小时` 的精确值，单位是 core-hours，等价于“该小时平均核数”，是可用口径。
- 因此调度器的可延迟需求统一取 `baseline_energy_core_hours`。

### 1.3 在线负载（必须满足）

- 用 `container_meta` 的 `cpu_request` 静态预留：`online_reserved_cores = 362,072` 核，当作恒定在线负载下界（无 usage 表，只能按静态预留近似）。

## 2. 工作负载扩展（方法 2）

- `scripts/generate_workload.py`：读 `compute_uncertainty.json` 的逐小时到达率，用 numpy `Poisson` 生成每小时到达数；每个到达有放回抽取真实任务记录（时长 + 能量），保留时长-能量联合分布。
- 输出 `data/workload/generated_envelope_30d.csv`（720 小时），消除 8 天滚动周期。
- 校验：30 天基线能量 = 8 天的 3.69×；窗口能量/基线能量 = 3.60×，与原 trace 一致。

## 3. 功率模型（cores → MW）

> 无 usage 表，故这是“资源需求代理”，不是实测功率；绝对量级是场景参数，结果重点在相对降本降碳。

```text
PUE                       = 1.2
WATTS_PER_CORE            = 3.0 W   # 每核活动功率（场景）
IDLE_WATTS_PER_MACHINE    = 150.0 W  # 每台机器空闲/基座功率（场景）
N_MACHINES                = 4034
```

```text
power_per_core_mw = PUE × WATTS_PER_CORE / 1e6
online_mw         = online_reserved_cores × power_per_core_mw        # 固定、必须满足
batch_mwh(t)      = baseline_energy_core_hours(t) × power_per_core_mw # 可延迟
base_mw           = PUE × N_MACHINES × IDLE_WATTS_PER_MACHINE / 1e6    # 固定基座
```

诚实边界：`online_mw` 是静态预留（上界），`batch_mwh` 是 `plan_cpu` 计划需求；二者都不声称实测功率。基座功率在基线里为固定常数，不影响优化，仅用于功率平衡与总量报告。

## 4. 基线 MILP（先确定性，后加 DRO）

### 4.1 变量（小时 t，0..719）

- `p_grid[t]`：购电功率（MW），≥0。
- `p_bess_ch[t]`, `p_bess_dis[t]`：BESS 充/放电（MW），≥0。
- `e_bess[t]`：BESS 能量（MWh）。
- `batch[t]`：调度后的可延迟功率（MW），在柔性包络内。

### 4.2 约束

1. 功率平衡：`p_grid[t] + p_bess_dis[t] + p_pv[t] = online_mw + batch[t] + p_bess_ch[t]`。
2. 批处理能量守恒：`Σ batch[t] = Σ batch_mwh[t]`（总可延迟能量不减少，只平移）。
3. 批处理柔性包络：`0 ≤ batch[t] ≤ window_mwh[t]`，其中 `window_mwh` 来自柔性窗口能量。
4. BESS SOC：`e_bess[t+1] = e_bess[t] + η_ch·p_bess_ch[t] − p_bess_dis[t]/η_dis`，`SOC_min ≤ e ≤ SOC_max`。
5. 并网/爬坡：`0 ≤ p_grid[t] ≤ G_max`，`|p_grid[t]−p_grid[t−1]| ≤ R_max`（扫参）。

### 4.3 目标（层级）

先 `min Σ dam_price[t] × p_grid[t]`；再在成本保护带内 `max Σ (p_pv[t] − p_bess_ch[t])` 提升风光匹配；最后降碳。首版先做第一层（成本）。

### 4.4 基线对照

- “无能源感知 + 不延迟”：`batch[t] = batch_mwh[t]`（观测值），BESS 不动作。

### 4.5 首层结果（仅批处理能量平移，尚未加功率/爬坡/BESS/DRO）

Jan 2025 窗口（PySCIPOpt，LP）：

- 基线批处理购电成本 40,316.99（无平移）；
- 柔性平移后 18,081.89；
- **成本下降 55.15%**。

注意：这是“能量平移”的松弛上界（`batch[t] ≤ window[t]` 且无并网/爬坡约束），绝对值还受 §3 的 core 口径不确定性影响，但下降比例是尺度不变的。后续加物理约束后会下降。

## 5. 待定

- 本地 PV 2025 剖面未拉；先用 ERCO 系统太阳形状代理或暂设 `p_pv[t]=0`。
- BESS 尺寸/效率/SOC 按场景给定，需定默认值。
- DRO 形式：预算 RO（Γ）；模糊集用 2024 能源残差 + 算力到达/时长经验分布标定。

## 6. config 参数评审（进行中）

- 预测/窗口参数（90 天历史、48h 保护、28 天基线、2024 验证、Ridge α）与 `docs/forecasting.md` 一致，合理。
- 功率模型：`PUE=1.2`、`WATTS_PER_CORE=3.0`、`IDLE_WATTS_PER_MACHINE=150`、`N_MACHINES=4034` 各自在合理区间；但 `online_reserved_cores(362k) + batch 平均核数(514k) > 物理核数(387k)`，说明“在线静态预留”和“plan_cpu×instance_num”都是上界代理、非同时实际占用，绝对 MW 会被高估约 2 倍。优化对尺度不敏感，绝对 MW/$ 需后续用利用率假设归一化。
- `MAX_BATCH_DURATION_HOURS=168`、`COMPLETION_SLACK_HOURS=3` 目前只是窗口文件名 `d168_h3` 的残留标识，非实际约束，可保留。
