# 实验结果（results）

本目录保存调度与实际回放的指标输出，并与 `docs/results.md`、`docs/paper_tables_figures.md` 对应。当前主线为风光储—柔性算力协同调度，在预测侧施加碳预算，并用实际风光和消费侧碳强度回放；旧 PV+BESS 原型结果仅保留为档案，不作为论文主结论。

| 文件 | 生成脚本 | 内容 |
| --- | --- | --- |
| `four_windows_mainline_summary.csv` | `scripts/run_four_windows.py` | 四窗口、所有方法共用的 0/2.5/5% 碳预算收紧下的风光储日前调度与实际回放；含调度相关运行成本、BESS 吞吐衰减成本、预测/实际碳排和违约 |
| `four_windows_summary.csv` | 历史原型脚本（已清理） | 旧 PV+BESS 词典序原型档案，不用于主线比较 |
| `baseline_comparison.csv` | 历史原型脚本（已清理） | 旧 Jan 不确定性对照档案，不用于主线比较 |
| `baseline_backtest_four_windows.csv` | 历史原型脚本（已清理） | 旧 PV 回测档案，不用于主线比较 |
| `backtest_results.json` | 历史原型脚本（已清理） | 旧随机场景回测档案，不用于主线比较 |

### `calibration/` 预检目录

| 目录 | 用途与结论 |
| --- | --- |
| `saa_decomposed_restore_preflight/` | 活动场景、恢复与并行回放基线；碳违反保持 `11/20` |
| `saa_carbon_dual_cuts_preflight/` | 首版碳 LP 割；第 2 轮 120 秒无 incumbent |
| `saa_carbon_tight_cuts_preflight/` | 收紧 Big-M 并延长到 300 秒；第 2 轮仍无 incumbent |
| `saa_carbon_cut_restore_preflight/` | 碳割恢复 38 秒不可行，完整第 2 轮 269 秒不可行 |
| `saa_carbon_cut_lower_bound_preflight/` | 首次下界诊断因互补性审计停止；只有配置文件，不是结果 |
| `saa_carbon_cut_lower_bound_lexicographic_preflight/` | 两阶段诊断正式证据：最小碳违约下界为 `6/20` |

这些目录是算法可行性预检，不是完整三折校准或论文方法比较结果。每个目录的 `run_config.json` 固定输入哈希和求解参数；逐次失败分析见 `docs/implementation_log.md`。

`operating_cost_usd` 与 `actual_operating_cost_usd` 仅含购电成本和 BESS 吞吐衰减成本；风光与储能固定运维成本及年化投资成本不在该 CSV 中。主比较固定资产配置，故这些成本为各方法相同常数，也不参与 `cost_reduction`。

## 复现命令

```powershell
# 需 scip_env（PySCIPOpt）
conda run -n scip_env python scripts/run_four_windows.py
```

当前 CSV 是确定性日前基线和实际回放结果；联合残差校准以及 SAA、RO、DRO 的公平比较完成后，再将正式方法对照写入论文结果。
