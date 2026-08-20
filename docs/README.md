# 文档索引与分工

各文档职责与“给谁看”：

| 文档 | 职责 | 给谁看 |
| --- | --- | --- |
| `../README.md` | 入口：研究问题、定位、创新点、数据方案、红线、里程碑 | 所有人 |
| `design.md` | 实现设计：数据接口、功率模型、不确定集标定、主实验配置、关键决策 | 复现 / 维护 |
| `model.md` | 数学公式与各类映射（LaTeX） | 写论文 / 复现 |
| `results.md` | 实验结果：所有表格与图表 | 写论文 |
| `paper_tables_figures.md` | 论文图表清单：表/图编号与论文结构映射 | 写论文 |
| `forecasting.md` | 无泄漏时序预测设计 | 预测部分 |
| `../data/energy/README.md` | 能源数据来源与哈希 | 复现 |
| `../data/workload/README.md` | Alibaba v2018 下载与字段 | 复现 |
| `../alibaba2018_dro/README.md` | 代码模块职责与数据流 | 读代码 |

分工原则：

- `../README.md` = “做什么、为什么”；
- `design.md` = “怎么实现、为什么这么定”；
- `model.md` = “数学公式与映射”；
- `results.md` = “跑了什么、得到什么”。

结果只放 `results.md`，不在 `design.md` 重复；公式只放 `model.md`。
