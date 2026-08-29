# 实验结果（results）

本目录保存调度与实际回放的指标输出，并与 `docs/results.md`、`docs/paper_tables_figures.md` 对应。当前主线优化运行成本并评价算力包络、并网和爬坡三类风险；实际碳排放只作环境绩效。旧 PV+BESS 与碳预算结果仅保留为档案。

| 文件 | 生成脚本 | 内容 |
| --- | --- | --- |
| `four_windows_mainline_summary.csv` | `scripts/run_four_windows.py` | 当前文件仍是目标切换前的确定性历史结果；按新入口重跑后将变为每窗口一行，报告成本、三类运行指标和预测/实际碳排，不含碳预算字段 |
| `four_windows_summary.csv` | 历史原型脚本（已清理） | 旧 PV+BESS 词典序原型档案，不用于主线比较 |
| `baseline_comparison.csv` | 历史原型脚本（已清理） | 旧 Jan 不确定性对照档案，不用于主线比较 |
| `baseline_backtest_four_windows.csv` | 历史原型脚本（已清理） | 旧 PV 回测档案，不用于主线比较 |
| `backtest_results.json` | 历史原型脚本（已清理） | 旧随机场景回测档案，不用于主线比较 |

### `calibration/` 预检目录

| 目录 | 用途与结论 |
| --- | --- |
| `saa_three_risk_preflight/` | 当前三风险主线预检；`fold_1/window=0/N=20` 最优，训练及验证的算力包络、并网、爬坡均无实质违反 |
| `saa_three_fold/` | 当前三折正式运行目录；逐组合保存检查点，全部 144 个组合完成后生成汇总和样本量选择 |

当前只保留三风险 SAA 主线结果。八个已停止的碳预算/碳割预检目录已于 2026-08-29 删除；失败原因的文字摘要保留在 `docs/implementation_log.md`，需要时可从 Git 历史恢复原始文件。每个现行目录的 `run_config.json` 固定输入哈希和求解参数。

`operating_cost_usd` 与 `actual_operating_cost_usd` 仅含购电成本和 BESS 吞吐衰减成本；风光与储能固定运维成本及年化投资成本不在该 CSV 中。主比较固定资产配置，故这些成本为各方法相同常数，也不参与 `cost_reduction`。

## 复现命令

```powershell
# 需 scip_env（PySCIPOpt）
conda run -n scip_env python scripts/run_four_windows.py
```

当前 CSV 是确定性日前基线和实际回放结果；联合残差校准以及 SAA、RO、DRO 的公平比较完成后，再将正式方法对照写入论文结果。
