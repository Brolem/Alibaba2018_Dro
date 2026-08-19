# Alibaba cluster-trace-v2018 下载与字段说明

## 下载

- 仓库：https://github.com/alibaba/clusterdata
- 目录：`cluster-trace-v2018/`
- 官方 `fetchData.sh` 使用的阿里云 OSS 入口：
  `http://aliopentrace.oss-cn-beijing.aliyuncs.com/v2018Traces/<file>.tar.gz`
- 需要下载并解压（CSV，压缩包后缀 `.tar.gz`，**均无表头**，字段顺序以官方 `schema.txt` 为准）：
  - `batch_task.tar.gz`（约 125 MB，解压约 765 MB）：批处理作业任务表
  - `container_meta.tar.gz`（约 2.4 MB，解压约 18 MB）：在线服务（LRA）容器元数据
  - `machine_meta.tar.gz`（约 92 KB）：机器元数据
  - （可选）`batch_instance.tar.gz`（约 20 GB）：批处理实例级起止/状态，用于真实调度延迟与回弹

下载后需核对 SHA-256（与官方 `trace_2018.md` 一致）：

| 文件 | SHA-256 |
| --- | --- |
| `machine_meta.tar.gz` | `B5B1B786B22CD413A3674B8F2EBFB2F02FAC991C95DF537F363EF2797C8F6D55` |
| `container_meta.tar.gz` | `FEBD75E693D1F208A8941395E7FAA7E466E50D21C256EFF12A815B7E2FA2053F` |
| `batch_task.tar.gz` | `7C4B32361BD1EC2083647A8F52A6854A03BC125CA5C202652316C499FBF978C6` |

## 实际表结构（来自官方 schema.txt，CSV 无表头）

| 表 | 字段（按列顺序） | 用途 |
| --- | --- | --- |
| `batch_task` | `task_name`, `instance_num`, `job_name`, `task_type`, `status`, `start_time`, `end_time`, `plan_cpu`, `plan_mem` | 批处理作业：release/deadline、资源需求、DAG 依赖、任务类型 |
| `container_meta` | `container_id`, `machine_id`, `time_stamp`, `app_du`, `status`, `cpu_request`, `cpu_limit`, `mem_size` | 在线服务（LRA）容器：必须满足的在线负载 |
| `machine_meta` | `machine_id`, `time_stamp`, `failure_domain_1`, `failure_domain_2`, `cpu_num`, `mem_size`, `status` | 服务器 CPU 核数与归一化内存容量 |

## 关键纠正（相对旧文档）

- **在线 vs 批处理按“表”划分，不是 `task_type` 字段**：`batch_task`/`batch_instance` 是批处理（可延迟候选），`container_meta`/`container_usage` 是在线服务（LRA，必须满足）。
- `container_meta` 没有 `task_type`/`job_name`/`task_name` 字段；它只有 `container_id, machine_id, time_stamp, app_du, status, cpu_request, cpu_limit, mem_size`。
- `batch_task.task_type` 是 12 种批处理任务类型之一（观测值 1–12），不是 batch/service 标志；DAG 信息在 `task_name` 的 `M/R + 数字` 结构里。
- `machine_meta` 的容量字段是 `cpu_num` 与 `mem_size`（归一化 [0,100]），不是 `capacity_cpu`/`capacity_memory`。

## 派生规则（供实现参考）

1. 可延迟批处理作业 = `batch_task`（或 `batch_instance`）的全部记录；`container_meta` 为在线服务负载。
2. `release = start_time`；`deadline = end_time`（或 `start_time + 观测时长 + 标定 slack`）。
3. 用 `plan_cpu` / 在线 `cpu_request` 与服务器 `cpu_num` → 服务器功率（利用率相关功率 + base/idle 功率）。
4. 作业到达率与执行时长的经验分布 → 算力侧不确定集。
5. 先统计 `batch_task.task_type`（1–12）与 `task_name` 的 DAG 结构，确定可延迟能量占比，再决定柔性包络与基线。
