# 文档索引与分工

各文档职责与“给谁看”：

| 文档 | 职责 | 给谁看 |
| --- | --- | --- |
| `../README.md` | 已确认研究主线、数据边界、实验设计与当前实现状态 | 所有人 |
| `design.md` | 目标模型的实现边界、数据接口与迁移顺序 | 复现 / 维护 |
| `model.md` | 风光储、碳预算、有效回放容量和联合不确定性的目标公式 | 写论文 / 实现 |
| `results.md` | 旧 PV+BESS 开发原型结果档案，不是主线论文结果 | 回归对照 |
| `paper_tables_figures.md` | 主线完成后应生成的论文图表与统一报告口径 | 写论文 |
| `forecasting.md` | 无泄漏预测、联合残差和实际回放口径 | 预测 / 回测 |
| `../data/README.md` | 原始/处理/结果三层数据边界 | 复现 |
| `../data/raw/energy/README.md` | 能源原始数据来源与哈希 | 复现 |
| `../data/raw/workload/README.md` | Alibaba v2018 下载与字段 | 复现 |
| `../data/processed/energy/README.md` | 能源派生输入（年度表 + 窗口） | 复现 |
| `../data/processed/workload/README.md` | 算力侧派生输入（包络/不确定集） | 复现 |
| `../data/results/README.md` | 实验结果文件与复现命令 | 写论文 / 复现 |
| `../alibaba2018_dro/README.md` | 代码模块职责与数据流 | 读代码 |

分工原则：

- `../README.md` = “做什么、为什么、研究边界和当前状态”；
- `design.md` = “目标如何实现、当前差距和实施顺序”；
- `model.md` = “目标数学模型与不确定性定义”；
- `results.md` = “旧原型跑了什么，仅可作回归对照”。

当前确定性主线的机器可读结果保存在 `../data/results/four_windows_mainline_summary.csv`；联合不确定性方法完成同口径比较后，再将正式论文结果写入 `results.md`。公式只放 `model.md`。
