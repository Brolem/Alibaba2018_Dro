# 实现与验证日志

本文只记录代码实现、执行命令、验证证据和已知边界。稳定的数学定义见
[model.md](model.md)，当前能力与后续路线见 [design.md](design.md)，正式实验结果见
[results.md](results.md)。每次实现只在最合适的文档中详细记录，避免同一内容在多个文件重复并逐渐不一致。

## 2026-08-27：SAA 场景与有限追索原型

- 提交：`6b77762 Add SAA scenario calibration and recourse prototype`。
- 生成 `calibration_day_blocks_2024.csv` 和 `saa_scenarios_manifest.json`，固定训练、验证和回放场景的来源、随机种子与输入哈希。
- 将 SAA 接入共同日前模型；固定日前 BESS，允许场景批处理在累计释放/截止包络内有限追索。
- 执行发现：固定批处理与场景包络冲突；改成有限追索后，单体式 `N=20` 联合 MILP 在时限内仍无可行 incumbent。因此没有启动完整三折校准。

## 2026-08-27：活动场景分解预检

- 提交：`b7371ab Add decomposed SAA preflight workflow`。
- 实现活动场景约束生成、风险一致的独立场景回放、单场景零违反恢复、warm start 和 4 进程并行回放。
- `fold_1 / N=20 / 1 window` 两轮预检：3 场景主问题约 81 秒最优；加入 1 条完整场景后，4 场景主问题约 107 秒最优。工作负载、并网和爬坡违反均为 `0/20`，碳违反连续两轮为 `11/20`。
- 结论：逐轮增加一条完整碳场景可解，但碳风险收敛不足；完整校准继续暂停，下一步改为场景 LP 对偶割。

## 2026-08-27：碳排放 LP 对偶割

### 实现

- 新增固定日前 BESS 的场景最小碳排放 LP。变量为场景批处理、购电和弃电；硬约束保留批处理累计包络、总工作量、并网上限和爬坡限制。
- 初版尝试从 PySCIPOpt 读取对偶值，依次遇到：变换后约束为空、旧对偶 API 被当前版本拒绝、求解后 LP 行集合为空。关闭预求解或把约束设为可修改仍不能得到经有限差分验证的对偶值。
- 安装 SciPy 后，改用 `scipy.optimize.linprog(method="highs")` 显式构造同一 LP，并从平衡等式的 `eqlin.marginals` 构造关于日前充/放电功率的支撑切平面。
- SAA 主问题支持全局场景编号和多条碳割；同一场景的所有割共享一个碳违约二进制变量，碳机会约束分母仍为完整样本数。
- 分解循环对当前全部碳违反场景批量生成割；工作负载、并网和爬坡通道仍使用完整活动场景。
- 结果表新增 `carbon_cut_count`，用于审计每次求解实际使用的割数。

### 已完成的小规模验证

```powershell
conda run --no-capture-output -n scip_env python -m unittest tests.test_mainline_scheduler.MainlineSchedulerTests.test_saa_workload_chance_constraint_uses_joint_scenario_envelope -v
```

- 基点割对 `0.01 MW` 充电扰动满足下界性质，且预测值与重新求解的 LP 最优值在 `1e-5` 精度内一致。
- 在碳违约额度为零时，人为构造的不可满足割会使主问题不可行，证明割已实际接入约束系统。

### 预检结果与边界

- 首次命令：`fold_1 / N=20 / 1 window / 3 iterations / 4 workers / 120 s`。第 1 轮 3 场景主问题在 82 秒最优；全场景回放为 workload `0/20`、carbon `11/20`、grid `0/20`、ramp `0/20`，并批量生成 11 条碳割。第 2 轮保持 3 个完整活动场景，但在 120 秒内无可行 incumbent（`mip_gap=1e20`），因此没有执行第 2 次回放。证据位于 `data/results/calibration/saa_carbon_dual_cuts_preflight/`。
- 失败分析：首版每条割沿用“全时域最大购电排放”Big-M，该值比支撑平面在 BESS 可行域内的最大值更松；且 120 秒只略高于旧 4 场景主问题的约 107 秒，不能区分数值松弛与真实不可行。
- 改进：利用逐时凸包 $p_t^{ch}+p_t^{dis}\le P^{BESS}$，将割的 Big-M 收紧为 $\max\{0,\alpha_s+P^{BESS}\sum_t\max(0,g_{s,t}^{ch},g_{s,t}^{dis})-\bar E\}$，并输出每批割的 Big-M 范围；在独立目录以 300 秒时限重跑。
- 收紧后预检证据位于 `data/results/calibration/saa_carbon_tight_cuts_preflight/`。11 条割的 Big-M 为 `197787.168--224285.176 kg`；第 2 轮延长到 300 秒后仍无 incumbent（`mip_gap=1e20`）。因此“仅因 120 秒过短”被排除，单纯继续延长时限不作为下一措施。
- 下一改进：加入碳割后，先解“1 个完整活动场景 + 当前全部碳割 + 原 `2/20` 碳违约额度”的较小恢复主问题，构造满足割的日前 BESS warm start，再进入下一轮完整活动场景主问题。该恢复问题若不可行，也可直接暴露当前割集合与风险额度的冲突。
- 恢复预检证据位于 `data/results/calibration/saa_carbon_cut_restore_preflight/`：小恢复模型 38 秒判定不可行，随后完整第 2 轮在 269 秒判定不可行。为量化冲突，新增“先最小化割违约数”的诊断目标。
- 首次下界诊断执行在 `data/results/calibration/saa_carbon_cut_lower_bound_preflight/`，小恢复模型 36 秒判定不可行；诊断阶段因目标只含违约二进制量，出现 1 小时同时充放并被互补性审计拒绝，未生成结果 CSV。改进为无任意权重的两阶段词典序：先固定最小违约数，再以原运行成本（含 BESS 吞吐衰减成本）整理日前计划。
- 最终证据位于 `data/results/calibration/saa_carbon_cut_lower_bound_lexicographic_preflight/`：第 1 轮主问题 76 秒最优，回放为 workload `0/20`、carbon `11/20`、grid `0/20`、ramp `0/20`；`2/20` 恢复模型 34 秒判定不可行；两阶段诊断 93 秒最优，得到 `carbon_cut_violation_lower_bound=6`。
- 结论：当前割是场景最小碳值函数的支撑下界，割松弛仍至少需要 `6/20` 个碳违约场景，因此正式允许的 `2/20` 无法满足。完整三折 SAA 校准继续暂停；下一步必须在碳预算收紧率、碳可靠性目标或资产配置之间做显式研究设计选择，不能把求解失败误判为继续加时即可解决。
