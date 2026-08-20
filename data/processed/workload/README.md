# 算力侧派生输入（processed/workload）

本目录保存由 `data/raw/workload` 确定性生成的、纳入版本控制的可复现输入。

| 文件 | 生成脚本 | 用途 |
| --- | --- | --- |
| `workload_stats.json` | `scripts/analyze_workload.py` | 任务/时长/到达统计、在线静态预留核数、可延迟能量占比代理 |
| `compute_uncertainty.json` | `scripts/build_uncertainty_envelope.py` | 到达率/时长的算力侧不确定集 |
| `hourly_flexibility_envelope.csv` | `scripts/build_uncertainty_envelope.py` | 观测基准能量 + 带 slack 的柔性窗口能量上界 |
| `generated_envelope_30d.csv` | `scripts/generate_workload.py` | 由 8 天 trace 蒙特卡洛扩展为 30 天的柔性包络（调度器输入） |

## 派生规则

1. 可延迟批处理作业 = `batch_task`（或 `batch_instance`）的全部记录；`container_meta` 为在线服务负载。
2. `release = start_time`；`deadline = end_time`（或 `start_time + 观测时长 + 标定 slack`）。
3. 用 `plan_cpu` / 在线 `cpu_request` 与服务器 `cpu_num` → 服务器功率（利用率相关功率 + base/idle 功率）。
4. 作业到达率与执行时长的经验分布 → 算力侧不确定集。
5. 先统计 `batch_task.task_type`（1–12）与 `task_name` 的 DAG 结构，确定可延迟能量占比，再决定柔性包络与基线。

## 复现命令

```powershell
python scripts/analyze_workload.py
python scripts/build_uncertainty_envelope.py
python scripts/generate_workload.py
```

> `generated_envelope_30d.csv` 用随机种子与蒙特卡洛扩展，重跑会得到新的实现；论文结果以当前提交版本为准。
