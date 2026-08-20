# 建模公式与映射

> 本文档是 `docs/design.md` 的数学补充，给出完整优化模型与各类映射。公式用 LaTeX。

## 1. 符号

- 时间索引 $t = 0, \dots, T-1$，$T = 720$（30 天核心期）。
- 决策变量（日前）：
  - $b_t$：批处理可延迟功率（MW）；
  - $g_t$：购电功率（MW）；
  - $c_t, d_t$：BESS 充/放电功率（MW）；
  - $e_t$：BESS 能量（MWh）。
- 参数：
  - $P^{\text{must}}$：必须满足的固定功率（在线 + 基座），MW；
  - $B_t$：批处理基线能量（MWh/h）；
  - $W_t$：柔性窗口能量上界（MWh）；
  - $p_t$：日前电价（USD/MWh）；
  - $\rho_t$：消费侧碳强度（kgCO₂/kWh）；
  - $\pi_t$：本地 PV 出力（MW）；
  - $G^{\max}, R^{\max}$：并网功率、爬坡上限；
  - $P^{\text{BESS}}, E^{\text{BESS}}$：BESS 功率/能量；
  - $\eta$：BESS 往返效率；
  - $SOC^{\min}, SOC^{\max}, SOC^0$：SOC 下界/上界/初值。

## 2. 确定性模型（LP）

### 2.1 功率平衡

$$g_t = P^{\text{must}} + b_t + c_t - d_t - \pi_t, \qquad \forall t$$

$$g_t \ge 0$$

### 2.2 批处理能量守恒与柔性包络

$$\sum_{t} b_t = \sum_t B_t$$

$$0 \le b_t \le W_t, \qquad \forall t$$

### 2.3 BESS

$$e_0 = SOC^0\, E^{\text{BESS}}$$

$$e_{t+1} = e_t + \sqrt{\eta}\, c_t - d_t / \sqrt{\eta}, \qquad \forall t$$

$$SOC^{\min} E^{\text{BESS}} \le e_t \le SOC^{\max} E^{\text{BESS}}$$

$$0 \le c_t,\ d_t \le P^{\text{BESS}}$$

$$e_T = e_0 \quad (\text{末态回到初态})$$

### 2.4 并网与爬坡

$$0 \le g_t \le G^{\max}, \qquad \forall t$$

$$|g_t - g_{t-1}| \le R^{\max}, \qquad \forall t \ge 1$$

### 2.5 目标（词典序，两层）

第一层：最小化总购电成本

$$C^* = \min \sum_t p_t\, g_t$$

第二层：在 1% 成本保护带内最小化碳

$$\min \sum_t \rho_t\, g_t \qquad \text{s.t.} \quad \sum_t p_t\, g_t \le C^*\,(1 + 1\%)$$

> 术语：这是“词典序（两次顺序求解）”，**不是**“两阶段 LP”（后者专指日前决策 + 实现后再调度的随机规划结构）。

## 3. Γ-budget 鲁棒扩展

### 3.1 算力侧（批处理总能量不确定）

不确定集：$E \in \left[E^{\text{nom}},\ E^{\text{nom}}(1+\delta)\right]$，$\Gamma \in [0,1]$ 为鲁棒预算。

鲁棒能量守恒：

$$\sum_t b_t = E^{\text{nom}}(1 + \Gamma\,\delta), \qquad E^{\text{nom}} = \sum_t B_t$$

其中 $\delta \approx 7.9\%$（由 8 天日能量 $CV=0.43$ 除以 $\sqrt{30}$ 估计）。

### 3.2 能源侧（PV 预测误差不确定）

$$\pi_t = \pi_t^{\text{nom}}\,(1 - \Gamma_{pv}\,\varepsilon), \qquad
\pi_t^{\text{nom}} = \Pi^{\text{cap}} \cdot \frac{s_t}{\max_t s_t}$$

其中 $s_t$ 是 ERCO 系统太阳预测、$\Pi^{\text{cap}}$ 是本地 PV 容量、$\varepsilon \approx 24.3\%$（2024 太阳能 NMAE）、$\Gamma_{pv}\in[0,1]$。

## 4. 各种映射

### 4.1 CPU 任务 → 核需求

批处理任务 $i$ 的核需求与能量：

$$q_i = \frac{\text{plan\_cpu}_i}{100} \times \text{instance\_num}_i \quad [\text{cores}]$$

$$E_i = q_i \times \frac{\text{duration}_i}{3600} \quad [\text{core-hours}]$$

小时基线能量：

$$B_t = \sum_{i} q_i \cdot \frac{\text{overlap}_{i,t}}{3600}$$

在线必须满足核数（静态预留）：

$$\bar{c}^{\text{online}} = \frac{1}{100} \sum_j \max_t \text{cpu\_request}_{j,t}$$

### 4.2 核 → 服务器功率（线性模型）

$$P^{\text{IT}}(t) = N^{\text{mach}} P^{\text{idle}} + \left(\bar{c}^{\text{online}} + \hat{b}_t\right) P^{\text{core}}$$

$$P^{\text{fac}}(t) = \text{PUE} \cdot P^{\text{IT}}(t)$$

换算成调度器里的量：

$$P^{\text{must}} = \text{PUE}\left(N^{\text{mach}} P^{\text{idle}} + \bar{c}^{\text{online}} P^{\text{core}}\right) / 10^6$$

$$B_t^{\text{[MWh]}} = B_t^{\text{[core-h]}} \cdot \text{PUE} \cdot P^{\text{core}} / 10^6$$

> 局限：无 usage 表，`plan_cpu`/`cpu_request` 是计划/预留值；`online 362k + batch 平均 514k > 物理 387k`，约超订 2.3 倍；$P^{\text{core}}$ 是场景值。绝对 MW 是场景量级，只有相对下降比例可信。

### 4.3 workload → 柔性包络

基线：$B_t$（任务能量按小时累加）。

带 slack 的窗口能量：

$$W_t = \sum_i E_i\, \mathbb{1}\!\left[t \in \left[\left\lfloor \tfrac{s_i}{3600}\right\rfloor,\ \left\lfloor \tfrac{e_i + \rho\,\text{dur}_i}{3600}\right\rfloor\right]\right]$$

其中 $s_i, e_i$ 是任务起止秒、$\rho$ 是 slack 比例、$\text{dur}_i = e_i - s_i$。

### 4.4 蒙特卡洛扩展（8 天 → 30 天）

$$n_t \sim \text{Poisson}\!\left(\lambda_{t \bmod 192}\right)$$

$$(d_k, E_k) \sim \hat{P}(\text{duration}, \text{energy})$$

其中 $\lambda_h$ 是 8 天逐小时到达率，$\hat{P}$ 是从真实任务记录有放回重采样的经验联合分布（保留时长—能量相关）。

### 4.5 残差 → 不确定集

- 算力侧：$E \in \left[E^{\text{nom}},\ E^{\text{nom}}(1+\delta)\right]$。
- 能源侧：$\pi_t = \pi_t^{\text{nom}}(1 - \Gamma_{pv}\varepsilon)$，$\varepsilon$ 来自 2024 残差 NMAE。

## 5. 功率映射的已知问题

1. 无 `machine_usage` / `container_usage`，用的是计划/预留值，不是实测利用率。
2. `online + batch` 约超订 2.3 倍（上界代理，非同时实际占用）。
3. `P^{core} = 3W` 是场景假设，绝对 MW 不可信，相对下降比例可信。

更正确的映射需要 usage 表 + SPECpower 型：

$$P_{\text{machine}} = P_{\text{idle}} + (P_{\max} - P_{\text{idle}}) \times \text{utilization}$$
