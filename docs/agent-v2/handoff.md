# 分析智能体 v2 · P0 交接

分支 `claude/agent-v2-p0-4de0ae`，基于 `main` = `475c738`（B_raw）。本会话没有调用真实模型，也没有连接或重置任何数据库。

## 1. 交付内容

| 交付 | 位置 | 提交 |
|---|---|---|
| B_raw / B0 / E1 manifest | `experiments/agent-v2/manifest.yaml`；可重算字段：`fingerprint.py` | `b999dd5` 起 |
| 评分器 v2（旧 `scoring.py` 与题目文件未改） | `backend/tests/eval/scoring_v2.py`、`analysis_questions_v2.yaml` | `7f6f8e4`、`2f55cca` |
| 全部已保存评测的旧 / 新重评 | `experiments/agent-v2/rescore/`（`rescore_v2.py` 生成） | 同上 |
| 轨迹诊断 | `docs/agent-v2/diagnosis.md` | 本文所在提交 |
| 修复 A 澄清 | `agent.py`、`tools.py`、`service.py`、分析详情页横幅 | `0378526` |
| 修复 E 模型接入协议 | `backend/src/avelis/llm_protocol.py`、`modelcfg.py`、`agent.py`、`tools.Budget` | `156a2dd`、`0c7506c` |
| 修复 F 复用语义 | `tools.py` | `711ed85` |
| 评测运行器记录代码版本与 v2 分数 | `scripts/eval_analysis.py` | `ac4eee9` |
| E1 注入框架与材料 | `experiments/agent-v2/e1.py`、`e1_injections.yaml` | `96e1aa6` |
| E0 协议冒烟、权限快照脚本（未运行） | `experiments/agent-v2/e0_smoke.py`、`permission_snapshot.py` | 本文所在提交 |

B0 = B_raw + 评分器 v2 + A、E、F。系统提示、工具 schema、依赖锁文件与 B_raw 相同（fingerprint 重算一致）。

## 2. 复现（离线，不需要模型与数据库）

```bash
cd backend && uv run pytest tests/unit -q
```

```bash
uv run --project backend python experiments/agent-v2/fingerprint.py --truth <主检出>/.demo/ec-truth.json --env <主检出>/.env
```

```bash
uv run --project backend python experiments/agent-v2/rescore_v2.py --eval-dir <主检出>/.demo/eval --truth <主检出>/.demo/ec-truth.json
```

```bash
uv run --project backend python experiments/agent-v2/rescore_v2_1.py --eval-dir <主检出>/.demo/eval --truth <主检出>/.demo/ec-truth.json     --e1 .demo/experiments/e1/20261031-015646.json --audit experiments/agent-v2/e1/human_audit.yaml
```

```bash
uv run --project backend python experiments/agent-v2/e1.py --plan
```

本会话的结果：单元测试 503 个通过；fingerprint 输出与 manifest 一致；重评结果见 `experiments/agent-v2/rescore/summary.md`；
`--plan` 输出 48 次运行。worktree 里没有 `.demo/`，`ec-truth.json` 由生成器重算，哈希与主检出相同（`41647a6b…`）。

## 3. E0 与 E1 运行前的准备（需要授权，见第 5 节）

1. 共用演示环境：从 worktree 起环境必须带 `--no-recreate`；不要重置共用元数据库 `avelis`。B0 没有新增迁移，但为了不影响其他会话，
   E1 应指向私有元数据库（例如 `avelis_agent_v2`），在子进程环境里设置 `AVELIS_DATABASE_URL`，并按协调会话的排期占用。
2. 权限快照：运行前后各一次，记哈希进 manifest。
   ```bash
   uv run --project backend python experiments/agent-v2/permission_snapshot.py wang sun
   ```
3. 核对 E1 材料里的表名、列名（只读元数据，不调用模型）：`e1_injections.yaml` 的 SQL 只用了历史轨迹里通过校验的表与列，
   `ec_product.price_change` 的列（effective_date、reason、old_price、new_price）来自一次未执行成功的查询，**未验证**；
   运行时若被校验拒绝，框架记为注入失败（不算模型失败），需要先修材料再跑，不能跑一半改。
4. 协调会话决定 `manifest.b0.token_cap_decision`（累计 token 上限的口径），写入 manifest 后再运行。

## 4. 执行命令与计数

E0（最多 7 次请求，任务书上限 12 次；无业务数据）：

```bash
uv run --project backend python experiments/agent-v2/e0_smoke.py --run
```

E0 已运行（8 次请求，约 0.0028 元，结果见 diagnosis.md 第 7 节）；需要指定主检出的 .env：

```bash
AVELIS_ENV_FILE=<主检出>/.env uv run --project backend python experiments/agent-v2/e0_smoke.py --run
```

E1（48 次 = 6 场景 × 4 条件 × 2 次；固定种子 20261031 打乱顺序；判分用 v2；费用上限 30 元，已扣除 E0 的花费）：

```bash
AVELIS_ENV_FILE=<主检出>/.env uv run --project backend python experiments/agent-v2/e1.py --run --cap 30 --spent 0.0028
```

账本每次运行前预留 1.5 元，预留不足就停止；运行详情或用量不完整时按预留额保守入账。

- 每次运行的上限：墙钟 300 秒、模型请求 16 次（含重试）、工具 30 次（修订 +6）、查询 20 次、探索 8 次；
  注入的查询与元数据读取不占这些额度，另记在 inject 步骤。
- 停止边界：48 × 16 = 768 次模型请求。按 232512 的均值估计约 9.2M 输入 token（约 8.5M 为缓存命中）、0.64M 输出 token；
  价格请按提供方当前价目核算（本会话没有核实价格）。
- 真实网络故障不在 E1 里自动重跑（流程内部对传输层临时错误重试一次，计入请求数）；超时、未完成、澄清都保留计分。
- 可以先只跑一个场景验证框架：`--only h10 --conditions D0,D3 --repeat 1`（4 次以内，也需要授权）。

## 5. 未运行与受阻

| 项 | 状态 | 需要 |
|---|---|---|
| E0 协议冒烟 | 已运行（8 次请求，0.0028 元）；发现并修复 max_tokens 字段不兼容 | — |
| E1 | 已完成 48 次，实际 3.52 元（结果见 `docs/agent-v2/results-e1.md`）；已用评分器 v2.1 重判（`docs/agent-v2/scorer-v2_1.md`） | 是否出 v2.1.1 修正跨维度成员拆分（3 次历史假阴性） |
| B0 缺陷（传输错误重试、token 上限预估） | 已修复并有回归测试（`57896ef`），未用真实模型复测 | 下次授权运行时观察 |
| 与真实数据库的集成测试（澄清状态写库、版本变更后的复用） | 未运行 | 私有元数据库；本会话只有单元测试 |
| 前端待澄清横幅 | 只改代码，未在浏览器里看过 | 演示环境 |

## 6. 回滚

- 每项修复是独立提交，可以单独 `git revert`：A `0378526`、E `156a2dd` + `0c7506c`、F `711ed85`；评分器与实验脚本不影响产品。
- 回到 B_raw 的产品行为：依次撤销上面四个提交（`ac4eee9` 只影响评测运行器）。
- 撤销 A 之后，已经写入的 `status = clarify` 的运行仍可读取（界面按普通状态显示），不需要数据迁移。
- 撤销 E 之后，DeepSeek 恢复用通用 ChatOpenAI，`reasoning_content` 不再回传（与 B_raw 相同的协议风险）。

## 7. 已知局限与下一步

- 物理 SQL 条数（第 13 节的 80 条上限）没有单独计数；受治理查询一次逻辑查询对应两期最多两条。
- 理解阶段收到的候选口径列表没有写进步骤记录，“候选漏召回”只能推断（建议 P1 记下候选编码，不影响行为）。
- 待澄清只停下与保存上下文；恢复入口（用户回答后接着跑）与界面留 P1。
- `ReasoningRoundTripChatOpenAI` 只覆盖非流式调用；产品没有用流式。
- v2 不认派生（derive）与探索数据集上的口径类关键数值；g01 的个数需人工审定；d06 的禁止关键字、001339 e02 的手填期望结论
  是否调整，需协调会话按设计理由裁定（本会话没有改题）。
- B0 下理解阶段写了追问的运行会停在待澄清（历史 26 次里 6 次），E0/E1 要把“必要澄清 / 不必要澄清”单列。

**为什么值得进入 E0 / E1**：诊断把 17 次失败的主因落在范围（4）、证据未发现（4）、报告绑定（3）、证据未进入上下文（2）、
证据解释（2）等不同环节，而这些判断大多依赖“模型看到了什么”的推断；E1 的四个条件正好逐级补齐范围、资产定位与原始证据，
能用 48 次运行区分“找不到”和“找到了也不会用”，决定 P1 先做 R（发现）还是 H（调查），避免按架构清单盲目开发。
E0 先验证 B0 的协议修复在真实服务上成立（尤其是 reasoning_content 往返），否则 E1 的结果会混入接入问题。
