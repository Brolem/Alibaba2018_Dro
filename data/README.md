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
- `workload/`：算力侧派生结果——`compute_uncertainty.json`、`hourly_flexibility_envelope.csv`、`generated_envelope_30d.csv`、`workload_stats.json`。

这些是调度器的直接输入，纳入版本控制以保证复现。

## results/（实验结果）

- `four_windows_summary.csv`：四窗口确定性结果（成本/碳/尖峰/爬坡/PV 自用率）。
- `baseline_comparison.csv`：确定性 vs 逐小时鲁棒 vs SAA 的对照。
- `baseline_backtest_four_windows.csv`：四窗口对照基线的样本外 PV 越限率。
- `backtest_results.json`：名义/鲁棒方案随 Γ 的违约率—收益折中。

复现命令见 `data/results/README.md`。
