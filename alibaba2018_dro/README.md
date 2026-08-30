# 代码模块说明

`alibaba2018_dro/` 是项目主包。模块职责：

| 模块 | 职责 |
| --- | --- |
| `config.py` | 窗口、预测、固定资源参考值、有效容量和主线场景常量 |
| `inputs.py` | 对齐预测/实际能源、有效回放容量和 workload 包络为小时输入（MW） |
| `eia_history.py` | 读 EIA-930 XLSX、ERCOT DAM 价（标准库） |
| `forecasting.py` | 48h 保护的 Ridge 预测器（numpy） |
| `energy.py` | 构造 1062h 论文窗口输入与清单 |
| `residuals.py` | 生成风光碳联合残差、可用日块和季节平衡折 |
| `scenarios.py` | 从 manifest 重建 SAA 训练、验证和回放场景 |
| `scheduler.py` | SCIP 确定性/SAA 日前模型、SCIP/Gurobi 可选有限批处理追索、三类运行风险和实际回放；碳割仅供历史诊断复现 |

数据流：

```text
原始能源/EIA（data/raw/energy）
  → eia_history → forecasting → energy → 共享年度表 + 1062h 窗口（data/processed/energy）

batch_task（data/raw/workload）
  → analyze_workload / generate_workload
  → 聚合工作量场景 + 累计柔性包络 + 在线核数（data/processed/workload）

窗口 CSV + 所有方法共用的 30 天名义累计柔性包络 + 在线核数 → inputs.HourlyInput → scheduler → 结果（data/results）
```

确定性入口是 `scripts/run_four_windows.py`；SAA 校准、验证和回放入口是 `scripts/run_uncertainty_methods.py`。当前主线不循环碳预算，碳排放只在求解后核算。

运行环境：

- `scheduler.py` 需 `scip_env`（PySCIPOpt）；仅复现历史碳对偶割时还需 SciPy/HiGHS；
- 其余模块 numpy / 标准库即可。
