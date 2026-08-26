# 算力侧派生输入（processed/workload）

本目录保存由 `data/raw/workload` 确定性生成的、纳入版本控制的可复现输入。

| 文件 | 生成脚本 | 用途 |
| --- | --- | --- |
| `workload_stats.json` | `scripts/analyze_workload.py` | 任务/时长/到达统计、在线静态预留核数、可延迟能量占比代理 |
| `generated_envelope_30d.csv` | `scripts/generate_workload.py` | 场景 0 的 30 天聚合工作量与累计柔性包络，供当前确定性调度器读取 |
| `compute_scenarios_30d.csv` | `scripts/generate_workload.py` | 20 个候选算力场景的长表，供后续 SAA/RO/DRO 共用 |
| `compute_scenarios_manifest.json` | `scripts/generate_workload.py` | 源哈希、参数、随机种子、抽样日块与输出哈希 |

## 派生规则

1. `batch_task` 提供批处理到达时刻代理、观测持续时间、实例数与计划 CPU；`container_meta` 提供确定性在线负载代理。
2. 每条正工作量记录转换为 $w_i=instance\_num_i(plan\_cpu_i/100)\max(end_i-start_i,0)/3600$ core-hour，再按到达小时聚合；负持续时间、零工作量与 8 天核心期外记录写入清单审计。
3. 原始持续时间只用于计算 $w_i$，不被人为拉长。最大可延迟窗口 $H$ 是独立的反事实参数：主值 6 h，敏感性 2/6/12/24 h。
4. 30 天场景以两日循环移动块重采样 8 天逐小时工作量，并统一缩放到 $W^*=(30/8)W^{trace}$，避免把总工作量差异混入算法成本比较。
5. 输出累计已到达工作量 $A_t$ 和累计到期工作量 $D_t$；调度满足 $D_t\le\sum_{\tau\le t}u_\tau\le A_t$。末端到期时刻截断到第 720 小时，保证场景总工作量闭合。
6. `baseline_cores` 是“到达即执行”在 1 h 时段内的平均核数，数值与 `baseline_energy_core_hours` 相同；`flexible_window_energy_core_hours` 是仍处于窗口内的工作量，不是瞬时核需求。

## 复现命令

```powershell
python scripts/analyze_workload.py
python scripts/generate_workload.py
```

默认命令使用 `seed=0`、两日块、$H=6$ h，并生成 20 个候选场景。20 只是实现与初步收敛检查的起点，最终 SAA 样本数仍需比较 $N\in\{20,50,100,200\}$ 后确定。论文复现必须同时记录清单文件和 Git 提交。
