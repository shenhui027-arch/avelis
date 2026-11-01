# 归档：开发过程记录

这里保存 v0.1 开发过程中的计划、里程碑验收流水与实验记录。它们是当时的工作记录，作为设计决定与验收结论的依据保留，**不再维护**：其中的命令、路径、分支名与数字反映的是当时的状态，可能与当前代码不一致。

使用与部署请看 [docs/README.md](../README.md) 中的现行文档；设计决定以 [ADR](../decisions/) 为准。

| 内容 | 说明 |
|---|---|
| [PLAN.md](PLAN.md) | v0.1 开发计划：范围、设计决定、里程碑与当时的“下一步” |
| [acceptance/](acceptance/) | 各里程碑的验收记录（[汇总](acceptance/acceptance.md)、[M1](acceptance/acceptance-m1.md)–[M8](acceptance/acceptance-m8.md)、[变更留底](acceptance/acceptance-history.md)）与验收截图 |
| [agent-v2/](agent-v2/) | 分析智能体 v2 的诊断、设计、实验结果（E1、E2、E2b）、评分器 v2.1 与审查记录；对应的实验脚本与重判数据在 [experiments/agent-v2](../../experiments/agent-v2/) |
| [mfg-demo-delivery.md](mfg-demo-delivery.md) | 制造 · 生产运营演示数据集的交付说明 |

里程碑代号：M0 演示环境与 StarRocks 核验，M1 后端闭环，M2 问数智能体与导入血缘，M3 工作台界面，M4 异常与加固，M5 交付，M6 问数表达能力，M7 更多引擎，M8 分析智能体。
