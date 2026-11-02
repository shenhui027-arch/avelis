# ADR 0017：口径按 Apache Ossie 导入导出，并为口径补充给模型用的语义提示

- 状态：部分采纳（2026-11-02，用户确定 1.1 顺序）。§3 口径语义提示作为 1.1 第一项实施；§1、§2 的 Ossie 导入导出排在 1.1 最后，等规范稳定后定稿。
- 相关：ADR 0005（问数表达力）、0010（业务中立）、0013（准确来自受治理的元数据）、0014（口径时间与量纲）；参考 Apache Gravitino 语义模型设计（`design-docs/gravitino-semantic-model-design.md`，2026-10-30 版）

## 背景

Avelis 的口径（`metric` / `metric_version`）只能在 Avelis 里创建和使用。企业通常已经在 dbt、Cube、BI 工具或 Gravitino 里维护了一部分指标定义，迁移时只能手工重录；反过来，Avelis 审核通过的口径也没法交给别的工具复用。

Apache Ossie（原 Open Semantic Interchange，OSI）定义了与厂商无关的分析语义模型：数据集、字段、关系、指标，以及给 AI 用的 `ai_context`（说明、同义词、示例）。Gravitino 已按它建模，并采用以下做法：

- 每个版本锁定一个 Ossie 规范提交，升级规范要改代码、补测试，不在每个对象上存规范版本；
- 表达式可按方言分别给出写法，未知方言原样保留，不做翻译；
- 写入前做三层校验（结构 → 模型内部 → 对照目录），任一层失败整体不写；
- 明确不做：源表变化后自动重新校验已存的模型。

另一方面，问数代理目前检索口径只用 `name`、`aliases`、`description`（`ask/agent.py`），没有“什么时候该用 / 不该用这个口径”的提示，也没有示例问法。

## 决定

### 1. 不改口径的内部模型，只做双向映射

`metric_version` 仍是唯一的事实来源（依赖指纹、审核流转、时间与量纲语义都不变）。Ossie 只是交换格式：

| Avelis | Ossie |
| --- | --- |
| 口径所在资产（`base_asset_id`）及其字段 | `datasets[]`，`source` 为三段名 |
| `dimensions`、`time_column`（标记 `dimension.is_time`） | `fields[]` |
| 已批准的 `join_path` | `relationships[]`（`from_columns` / `to_columns`） |
| `expression`（数据源方言） | `metrics[].expression.dialects[]`，方言名用 sqlglot 方言名 |
| `unit`、`time_aggregation`、`data_grain`、`polarity` 等 Ossie 没有的字段 | `custom_extensions[]`，`vendor_name = "avelis"` |
| 新增的语义提示（见 §3） | `ai_context` |

一个业务域导出为一个 Ossie 语义模型文件。按 `0.2.0.dev0` 规范，单个文件没有 `semantic_model` 包装，`version`、`name`、`datasets`、`relationships`、`metrics` 等直接放在根部；指标可以放在根部，也可以挂在某个数据集下，Avelis 导出时统一放在根部。

### 2. 导入只生成草稿，必须走现有审核

- 导入时依次做：结构校验 → 模型内部校验（重名、关系两端、列数一致）→ 对照已采集资产校验（源表、列必须存在，按导入人的业务域权限查找，不暴露看不到的资产）。任一层失败整体不写入，并逐条列出原因。
- 通过后生成 `draft` 口径版本，照常算依赖指纹、走 `pending_review → published`。**导入不能直接发布。**
- 表达式只取与数据源方言一致的那一条写法；没有时尝试用 sqlglot 从 Ossie 规范定义的通用方言 `OSSIE_SQL_2026` 转写，转写结果标为“待人工确认”。Ossie 的 `dialect` 是自由字符串，其他未知方言原样保存、不转写。
- `custom_extensions` 里非 avelis 的内容原样保存，导出时原样带回，保证往返不丢信息。

### 3. 口径语义提示（`ai_context`）

`metric` 增加可选的 `ai_context`：

```text
instructions?: string    什么时候该用 / 不该用，例如“退款不计入，需要含退款请用 gmv_gross”
synonyms?: string[]      与现有 aliases 合并检索
examples?: string[]      典型问法，用于检索与评测
```

- 只影响检索与澄清，不进入编译出的 SQL，也不计入依赖指纹。
- 由口径负责人维护，修改写审计。
- 内容来自用户和口径负责人，产品代码不预置任何业务词（ADR 0010）。
- 字段与 Ossie 的 `ai_context` 对象一致（`instructions` / `synonyms` / `examples`），导出时原样映射；Ossie 也允许 `ai_context` 是一段字符串，导入时把字符串当作 `instructions`。

### 4. 版本锁定

每个 Avelis 版本锁定一个 Ossie 规范提交，写在代码常量和文档里。升级规范需要：改映射、补往返测试、在 CHANGELOG 中写明。

2026-11-02 调研时规范版本为 `0.2.0.dev0`（草案，项目 2026 年 7 月进入 Apache 孵化器，仍在活跃变动），仓库内 `core-spec/spec.yaml`、`ossie-schema.json` 为机器可读定义。

### 5. 实施顺序（2026-11-02 定）

1. 1.1 第一项：只做 §3 口径语义提示（数据模型、编辑界面、问数检索与澄清使用它、审计），不依赖 Ossie。
2. Ossie 导入导出排在 1.1 最后（MCP 服务与治理事件之后）。届时先按当时锁定的规范版本做导出，导入在规范到 0.2.0 正式版后再做。

## 不做

- 不把 Ossie 当内部存储格式，也不在对象上存规范版本。
- 不导入 dbt、Cube 等厂商的原生格式（可由社区先转成 Ossie）。
- 不做 Ontology（概念与映射），等 Ossie 的 Ontology 规范稳定后另议。
- 不在导入时校验表达式的执行语义；执行前的检查仍由发布校验（`publish_check`）负责。

## 后果

- 好处：口径可以和外部工具互通，是开源项目进企业的入口；`ai_context` 直接提高问数检索与澄清的质量。
- 代价：要维护一份映射和一组往返测试；Ossie 仍在 0.x 阶段，规范变动时需要跟进。
- 风险：导入的表达式可能在方言转写上出错。所以转写结果默认待确认，并且必须经过审核才能发布。

## 待讨论

1. 导出粒度按业务域还是按选中的口径？
2. 派生口径（`kind = derived`）的 `components` 在 Ossie 里怎么表达：展开成表达式，还是放进 `custom_extensions`？
3. `ai_context.examples` 是否同时作为评测题的来源？
