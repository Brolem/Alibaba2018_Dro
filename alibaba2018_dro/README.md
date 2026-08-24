# 代码模块说明

`alibaba2018_dro/` 是项目主包。模块职责：

| 模块 | 职责 |
| --- | --- |
| `config.py` | 窗口、预测、固定资源参考值、有效容量和主线场景常量 |
| `inputs.py` | 对齐预测/实际能源、有效回放容量和 workload 包络为小时输入（MW） |
| `eia_history.py` | 读 EIA-930 XLSX、ERCOT DAM 价（标准库） |
| `forecasting.py` | 48h 保护的 Ridge 预测器（numpy） |
| `energy.py` | 构造 1062h 论文窗口输入与清单 |
| `scheduler.py` | 主线日前 MILP：风光+BESS+有效容量+预测碳预算，以及实际风光碳回放 |

数据流：

```text
原始能源/EIA（data/raw/energy）
  → eia_history → forecasting → energy → 共享年度表 + 1062h 窗口（data/processed/energy）

batch_task（data/raw/workload）
  → analyze_workload / build_uncertainty_envelope / generate_workload
  → 柔性包络 + 在线核数（data/processed/workload）

窗口 CSV + 柔性包络 + 在线核数 → inputs.HourlyInput → scheduler → 结果（data/results）
```

当前实验入口是 `scripts/run_four_windows.py`，生成四窗口 × 碳预算强度的确定性主线基准结果。

运行环境：

- `scheduler.py` 需 `scip_env`（PySCIPOpt）；
- 其余模块 numpy / 标准库即可。
