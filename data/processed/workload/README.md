# 算力侧派生输入（processed/workload）

本目录保存由 `data/raw/workload` 确定性生成的、纳入版本控制的可复现输入。

| 文件 | 生成脚本 | 用途 |
| --- | --- | --- |
| `workload_stats.json` | `scripts/analyze_workload.py` | 任务/时长/观测执行开始统计、在线静态预留核数、可延迟能量占比代理 |
| `workload_daily_audit.json` | `scripts/audit_workload_days.py` | 逐日任务数、状态、工作量与时间边界完整性审计 |
| `aggregate_workload_8d.csv` | `scripts/plot_aggregate_workload.py` | 原始 8 天轨迹按观测执行开始小时聚合的工作释放代理，单位 core-hour |
| `nominal_workload_30d.csv` | `scripts/generate_workload.py` | 确定性、SAA、RO、DRO 共用的唯一 30 天名义负荷与累计柔性包络 |
| `nominal_workload_manifest.json` | `scripts/generate_workload.py` | 源哈希、完整日选择、名义随机种子、平衡日块、柔性窗口与输出哈希 |

## 派生规则

1. `batch_task` 提供观测执行开始/结束时间、实例数与计划 CPU；开始时间仅作为聚合工作释放代理，不解释为真实提交/到达时刻。`container_meta` 提供确定性在线负载代理。
2. 每条正工作量记录转换为 $w_i=instance\_num_i(plan\_cpu_i/100)\max(end_i-start_i,0)/3600$ core-hour，再按观测执行开始小时聚合；负持续时间、零工作量与 8 天核心期外记录写入清单审计。
3. 第 1 天主要是追踪起点状态快照：任务数仅为第 2—8 天均值的 1.31%，第 0 小时集中 23,557 条 `Waiting` 记录，不能作为正常工作释放日；名义负荷只使用第 2—8 天。第 8 天全天记录完整并保留。
4. 第 2—8 天的正工作量中，`Terminated` 占 98.83%，`Failed` 占 0.87%，`Running` 占 0.30%；后两者仍代表已请求或已消耗的观测工作量，故不额外删除。第 2—8 天没有非正实例数；非正 CPU 请求自然形成零工作量并被排除。完整统计保存在 `workload_daily_audit.json`。
5. 原始持续时间只用于计算 $w_i$，不被人为拉长。最大可延迟窗口 $H$ 是独立的反事实参数：主值 6 h，敏感性 2/6/12/24 h。
6. 唯一名义负荷采用平衡两日循环块：30 天由 15 个两日块组成，所有 7 个保留源日各出现 4 或 5 次，再随机排列块顺序。循环块允许第 8 天接第 2 天，这是在短轨迹上采用平稳日序列的透明假设。鉴于完整数据仅 7 天，块长固定为 2 天，不新增 1/2/3 天块长敏感性。总量缩放到第 2—8 天日均工作量乘以 30，即 $421{,}287{,}756.644638$ core-hour。
7. 输出累计可执行工作量上界 $A_t$ 和必须完成工作量下界 $D_t$；二者由同一归一化释放轨迹和固定 $H$ 派生，不得独立扰动。调度满足 $D_t\le\sum_{\tau\le t}u_\tau\le A_t$。末端完成时刻截断到第 720 小时，保证总工作量闭合。
8. `baseline_cores` 是“释放即执行”在 1 h 时段内的平均核数，数值与 `baseline_energy_core_hours` 相同；`flexible_window_energy_core_hours` 是仍处于窗口内的工作量，不是瞬时核需求。
9. 后续 SAA、RO、DRO 如需训练样本，使用 `--training-scenario-count N` 另行生成同源场景池；该池只用于不确定性标定，不能替代公共名义负荷，也不能由不同方法分别挑选。

## 训练与测试边界

目标流程是从第 2—8 天生成训练场景池，用于经验分布和不确定集标定；`nominal_workload_30d.csv` 是所有方法共用的日前名义输入；最终还需用与训练种子不重叠、且所有方法共享的算力回放池评价不确定性下的违反率。只在名义负荷上比较不能验证 SAA/RO/DRO 的可靠性。当前默认 `training_scenario_count=0`，训练场景池、回放池及 SAA/RO/DRO 均尚未接入；现有四窗口只是在公共 30 天负荷上运行确定性基准。

训练池、公共 30 天负荷和未来回放池都来自相同的 7 个完整源日。它们是文件和预注册种子隔离的同源样本，不是独立年份或独立月份的算力数据。因此，30 天负荷应称为“公共名义输入”，未来回放称为“同源、种子隔离的重采样评价”；不能称为“30 天真实样本”或“算力侧独立时间外推”。完整数据可行性结论见 `docs/data_feasibility.md`。

## 复现命令

```powershell
python scripts/analyze_workload.py
python scripts/audit_workload_days.py
python scripts/generate_workload.py
python scripts/plot_aggregate_workload.py
python scripts/plot_resampled_workload.py
```

默认命令使用 `seed=0`、平衡两日块和 $H=6$ h，只生成一条所有方法共用的 30 天名义负荷。该序列含 720 小时，逐日总量为 10.67–18.01 million core-hour，日总量 CV 为 0.1425。训练样本数仍需比较 $N\in\{20,50,100,200\}$ 后确定，不在数据定版阶段预设为 20。论文复现必须同时记录审计文件、生成清单和 Git 提交。
