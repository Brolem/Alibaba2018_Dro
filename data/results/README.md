# 实验结果（results）

本目录保存调度与回测的指标输出，与 `docs/results.md`、`docs/paper_tables_figures.md` 对应。除特别说明外，均为四窗口主实验配置（G_max=1.0×峰值、R_max=0.1×峰值/h、BESS 0.5×峰值/2h、PV 1.0×P_must，词典序 cost→carbon）。

| 文件 | 生成脚本 | 内容 |
| --- | --- | --- |
| `four_windows_summary.csv` | `scripts/run_four_windows.py` | 四窗口成本/碳/尖峰/爬坡下降与 PV 自用率 |
| `baseline_comparison.csv` | `scripts/compare_baselines.py` | Jan 确定性 / SAA / 逐小时鲁棒（多档 Γ_pv）对照 |
| `baseline_backtest_four_windows.csv` | `scripts/backtest_baselines.py` | 四窗口对照基线的样本外 PV 越限率 |
| `backtest_results.json` | `scripts/backtest.py` | 名义/鲁棒方案随 Γ 的违约率—收益折中 |

## 复现命令

```powershell
# 需 scip_env（PySCIPOpt）
conda run -n scip_env python scripts/run_four_windows.py
conda run -n scip_env python scripts/compare_baselines.py
conda run -n scip_env python scripts/backtest_baselines.py
conda run -n scip_env python scripts/backtest.py
```

> `backtest*.py` 用固定随机种子生成场景，重跑可复现；`generated_envelope_30d.csv` 若被重新生成，则结果会随新包络变化，论文结果以当前提交版本为准。
