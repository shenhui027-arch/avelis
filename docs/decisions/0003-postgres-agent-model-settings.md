# ADR 0003：元数据库收敛为 PostgreSQL；问数改为 LangGraph 智能体；模型在页面配置

- 状态：已采纳（取代 ADR 0002 第 2 条中“同时兼容 MySQL”的部分，以及 PLAN 决定 11）
- 日期：2026-10-29

## 背景

用户希望问数“更智能、能思考起来”。我们评估了 LangChain 与 LangGraph：

- LangGraph 的价值在于持久化的多步状态、中途暂停等人回答、断点续跑。
- 它的官方状态存储只支持 PostgreSQL。

用户决定：产品依赖 PostgreSQL，去掉 MySQL 元数据库支持。
被采集的 MySQL 数据源（电商维表库）不受影响。

另外，模型需要在页面上配置，并参考开源项目的模型配置页。

## 决定

1. **元数据库只支持 PostgreSQL 18。**
   - 移除演示环境的 MySQL 元数据库容器，以及代码中的 MySQL 分支。
   - JSON 字段改为 JSONB（迁移 0002 带 `USING` 转换）。
   - `AVELIS_DATABASE_URL` 必须是 `postgresql` 开头。
2. **问数编排用 LangGraph 1.2.15。** 流程是：
   - 理解（`understand`）
   - 追问（`clarify`，interrupt / resume）
   - 治理与执行（`govern`）
   - 解释（`explain`）

   会话状态用 `langgraph-checkpoint-postgres` 存在独立模式 `avelis_agent`，服务重启后能续接追问。
   边界不变：
   - 模型只能通过工具（`search_metrics`、`match_dimension_values`、`submit_intent`、`ask_user`）提交结构化意图；
   - 编造的维度取值会被工具拒绝；
   - 口径状态、依赖指纹、权限、SQL 编译与校验、只读执行全部是确定性代码。
3. **模型接入用 LangChain 的聊天模型适配器**（`langchain-openai` 1.7.1、`langchain-anthropic` 1.7.6），不使用 LangChain 的链与代理抽象：
   - OpenAI 兼容接口覆盖 DeepSeek、通义千问等。
   - 为兼容 DeepSeek 思考模式，不强制 `tool_choice`。
4. **LangSmith 等外部追踪默认关闭**：`LANGSMITH_TRACING=false`，避免把问题和元数据发到第三方。
5. **模型配置存元数据库，在页面维护。** 页面参考了：

   | 来源 | 借鉴的做法 |
   |---|---|
   | LobeChat、Cherry Studio | 提供方列表、检测、获取模型列表 |
   | Open WebUI | `/models` 验证、模型白名单 |
   | Dify | 按用途设置默认模型 |

   具体规则：
   - 密钥用 Fernet 加密存储，只返回末四位掩码。
   - “检测”实际发起一次带工具定义的对话，记录是否支持工具调用；问数理解只能指定通过检测的模型。
   - 按用途指定模型：问数理解、结果解释、导入血缘推断。
   - 数据外发策略需要管理员显式开启，“发送查询结果”和“发送代码片段（已脱敏）”默认都关闭。
   - `.env` 的 `AVELIS_MODEL_*` 只作为开发便利（`bootstrap model`），不是运行时配置。
6. **前端选型**：
   - TypeScript 改用 6.0.3：openapi-typescript 7.13.0 依赖 TypeScript 的 JS API，TypeScript 7（原生实现）没有这套 API。
   - React Router 用 7.18.4，因为 7.18.5 发布不足两周。
   - 设计系统暂缺开关、抽屉、提示组件，在 `frontend/src/styles/app.css` 中按令牌扩展，后续回收进 Avelis UI。

## 后果

- 部署必须提供 PostgreSQL。旧的 MySQL 元数据卷 `avelis-demo_meta-mysql-data` 不再使用，可手动删除。
- 真实模型的行为不确定：验收用 `-m llm` 标记，在有密钥时运行；确定性的治理边界由脚本化假模型的用例覆盖。
- 导入血缘的边是设计时血缘，不参与字段推导指纹与影响分析；与观察到的定义冲突时只提示，不覆盖。
