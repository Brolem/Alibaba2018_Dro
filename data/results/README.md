# 实验结果（results）

本目录保存调度与实际回放的指标输出，并与 `docs/results.md`、`docs/paper_tables_figures.md` 对应。当前主线为风光储—柔性算力协同调度，在预测侧施加碳预算，并用实际风光和消费侧碳强度回放；旧 PV+BESS 原型结果仅保留为档案，不作为论文主结论。

| 文件 | 生成脚本 | 内容 |
| --- | --- | --- |
| `four_windows_mainline_summary.csv` | `scripts/run_four_windows.py` | 四窗口、所有方法共用的 0/2.5/5% 碳预算收紧下的风光储日前调度与实际回放；含调度相关运行成本、BESS 吞吐衰减成本、预测/实际碳排和违约 |
| `four_windows_summary.csv` | 历史原型脚本（已清理） | 旧 PV+BESS 词典序原型档案，不用于主线比较 |
| `baseline_comparison.csv` | 历史原型脚本（已清理） | 旧 Jan 不确定性对照档案，不用于主线比较 |
| `baseline_backtest_four_windows.csv` | 历史原型脚本（已清理） | 旧 PV 回测档案，不用于主线比较 |
| `backtest_results.json` | 历史原型脚本（已清理） | 旧随机场景回测档案，不用于主线比较 |

`operating_cost_usd` 与 `actual_operating_cost_usd` 仅含购电成本和 BESS 吞吐衰减成本；风光与储能固定运维成本及年化投资成本不在该 CSV 中。主比较固定资产配置，故这些成本为各方法相同常数，也不参与 `cost_reduction`。

## 复现命令

```powershell
# 需 scip_env（PySCIPOpt）
conda run -n scip_env python scripts/run_four_windows.py
```

当前 CSV 是确定性日前基线和实际回放结果；联合残差校准以及 SAA、RO、DRO 的公平比较完成后，再将正式方法对照写入论文结果。
