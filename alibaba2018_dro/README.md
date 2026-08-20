# 代码模块说明

`alibaba2018_dro/` 是项目主包。模块职责：

| 模块 | 职责 |
| --- | --- |
| `config.py` | 窗口 / 预测 / 功率场景常量 |
| `inputs.py` | 把能源窗口 + workload 包络对齐成统一小时输入（MW） |
| `eia_history.py` | 读 EIA-930 XLSX、ERCOT DAM 价（标准库） |
| `forecasting.py` | 48h 保护的 Ridge 预测器（numpy） |
| `energy.py` | 构造 1062h 论文窗口输入与清单 |
| `scheduler.py` | 日前调度 LP：批处理平移 + BESS + PV + 双侧 Γ-RO + 各扫参（PySCIPOpt） |

数据流：

```text
原始能源/EIA → eia_history → forecasting → energy → 窗口 CSV
batch_task → build_uncertainty_envelope → 柔性包络 CSV
generated_envelope_30d.csv + 窗口 CSV → inputs.HourlyInput → scheduler → 结果
```

运行环境：

- `scheduler.py` 需 `scip_env`（PySCIPOpt）；
- 其余模块 numpy / 标准库即可。
