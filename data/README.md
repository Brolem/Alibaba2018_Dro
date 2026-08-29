# 数据分层说明

本项目数据分为三层，边界如下：

| 目录 | 含义 | 是否入库 | 是否可重新生成 |
| --- | --- | --- | --- |
| `raw/` | 公开来源的原始文件，不可变 | 大文件否，小文件部分入库 | 可重新下载 |
| `processed/` | 由原始数据确定性生成的派生输入 | 是 | 是（按脚本重跑） |
| `results/` | 调度与回测产出的实验指标 | 是 | 是（按脚本重跑） |

## raw/（原始数据）

- `energy/`：EIA-930 ERCO 全历史工作簿、ERCOT DAM 年度归档。
- `workload/`：Alibaba cluster-trace-v2018 的 `batch_task` / `container_meta` / `machine_meta` 及其压缩包。

原始大文件（EIA 工作簿、`batch_task.csv`、`*.tar.gz` 等）由 `.gitignore` 排除，复现时重新下载并核对各 `README.md` 里的 SHA-256。

## processed/（处理后/派生输入）

- `energy/`：共享 2025 年度表 `ercot_2025_houston_hourly.csv`，以及四个 1062 小时无泄漏窗口与 `inputs_manifest.json`。
- `workload/`：逐日审计、原始 8 天聚合、公共 30 天名义负荷及其生成清单。
- `scenarios/`：2024 校准日块、输入清单及训练/验证/回放场景 manifest；用于按需重建嵌套场景，不手工修改。

这些是调度器的直接输入，纳入版本控制以保证复现。

## results/（实验结果）

- `four_windows_mainline_summary.csv`：目标切换前的确定性历史结果，待按当前三风险口径重跑。
- `calibration/`：当前 SAA 三风险预检及三折校准检查点；已停止的碳预算诊断结果已删除。
- 其余 `four_windows_summary.csv`、`baseline_*` 和 `backtest_results.json`：旧 PV+BESS 原型档案。

复现命令见 `data/results/README.md`。
