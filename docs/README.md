# Avelis 文档

## 使用与运维

| 文档 | 内容 |
|---|---|
| [product-tour.md](product-tour.md) | 界面导览：登录、工作台、问数与分析会话、导出、数据集、个人经验、血缘、目录、设置的截图与说明 |
| [deployment.md](deployment.md) | 部署指南：Docker Compose、外部 PostgreSQL / Redis、不用 Docker 部署、升级、备份、排障 |
| [configuration.md](configuration.md) | 配置参考：全部 `AVELIS_*` 环境变量、默认值与是否必填 |
| [development.md](development.md) | 开发指南：后端与前端开发命令、本地演示环境、一条命令复现验收 |
| [documentation-guide.md](documentation-guide.md) | 文档规范：README 与文档的结构、语言、术语、链接与图片写法 |

## 设计

| 文档 | 内容 |
|---|---|
| [architecture.md](architecture.md) | 架构与治理说明：组成、数据模型、关键机制、治理规则、运行与已知边界 |
| [PLAN.md](PLAN.md) | v0.1 开发计划：范围、设计决定与里程碑 |
| [decisions/](decisions/) | 架构决策记录（ADR 0001–0013） |
| [starrocks-notes.md](starrocks-notes.md) | StarRocks 3.5.14 实机核验记录 |

架构决策记录：

- [ADR 0001](decisions/0001-demo-environment.md) 演示环境的组成与约束
- [ADR 0002](decisions/0002-backend-foundations.md) 后端基础选型与治理模型落地
- [ADR 0003](decisions/0003-postgres-agent-model-settings.md) 元数据库收敛为 PostgreSQL；问数改为 LangGraph 智能体；模型在页面配置
- [ADR 0004](decisions/0004-engine-adapters.md) 引擎适配层：多数据库血缘的扩展方式
- [ADR 0005](decisions/0005-ask-expressiveness.md) 问数表达能力：多指标、排名、对比与派生指标
- [ADR 0006](decisions/0006-column-meaning-from-db-comments.md) 字段含义以数据库列注释为准
- [ADR 0007](decisions/0007-analysis-agent.md) 分析智能体：模型负责多步思考，治理守住每个数的来源
- [ADR 0008](decisions/0008-history-integrity.md) 变更留底：归因到数据库用户、检索导出、防篡改与保留期
- [ADR 0009](decisions/0009-grounded-numbers-and-evaluation.md) 报告数字按引用渲染、自动归因、探索查询与评测集
- [ADR 0010](decisions/0010-business-neutral-product.md) 产品代码与具体业务解耦
- [ADR 0011](decisions/0011-production-scale-context.md) 生产规模下的按需上下文
- [ADR 0012](decisions/0012-analysis-pipeline-system-assembled-context.md) 分析流程改为“系统组装上下文、模型少轮推理”
- [ADR 0013](decisions/0013-accuracy-from-governed-metadata.md) 对话式确认与经验学习

## 验收记录

| 文档 | 内容 |
|---|---|
| [acceptance.md](acceptance.md) | v0.1 验收汇总（一条命令从零复现） |
| [acceptance-m1.md](acceptance-m1.md) – [acceptance-m8.md](acceptance-m8.md) | 各里程碑验收：后端闭环、问数智能体、工作台、异常与加固、交付、问数表达能力、更多引擎、分析智能体 |
| [acceptance-history.md](acceptance-history.md) | 变更留底验收（ADR 0008） |
| [mfg-demo-delivery.md](mfg-demo-delivery.md) | 制造 · 生产运营演示数据集的交付说明 |

## 分析智能体 v2 实验记录

[agent-v2/](agent-v2/) 下是分析智能体 v2 的诊断、设计与实验结果：[P0 诊断](agent-v2/diagnosis.md)、[P0 交接](agent-v2/handoff.md)、[P1 设计](agent-v2/p1-design.md)、[E1 结果](agent-v2/results-e1.md)、[E2 结果](agent-v2/results-e2.md)、[E2b 结果](agent-v2/results-e2b.md)、[评分器 v2.1](agent-v2/scorer-v2_1.md)、[下一阶段方案](agent-v2/next-phase-plan.md)、[PR #11 审查](agent-v2/review-pr11.md)。

## 术语

| 术语 | 含义 |
|---|---|
| 数据源 | 被 Avelis 采集与查询的一个数据库实例（StarRocks、PostgreSQL 等），分别配置采集账号与只读查询账号 |
| 资产 | 数据源中的表、视图、外部表等对象，按物理 ID 识别 |
| 元数据库 | Avelis 自己的 PostgreSQL 库，存放资产、血缘、口径、审计与智能体会话 |
| 血缘 | 资产与字段之间的推导关系，来自视图解析、导入的 SQL / 代码与人工确认 |
| 口径（指标） | 业务上认可的计算定义，有版本与发布 / 复核状态；问数只用已发布的口径 |
| 业务域 | 资产与口径的归属与权限单位，成员分负责人、维护者、可查询 |
| 问数 | 用自然语言提问，由已发布口径确定性生成 SQL 并只读执行，答案带来源与依据 |
| 分析 | 多步分析智能体，回答“为什么变了、什么关系、该怎么做”，报告中的数字逐个核验 |
| 变更留底 | 元数据与血缘的历史版本，只追加并封入哈希链，可核验、检索与导出 |
| worker | 后台进程，执行采集、定时任务与历史清理 |
