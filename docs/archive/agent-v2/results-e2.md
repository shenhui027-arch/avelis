# E2 结果：2×2 对照（B0 / +R / +H / +R+H）

## 0. 运行信息与限定

- **运行**：2026-10-31 12:36 起，代码 `66668ce`（工作区干净，开关值逐次核对）。16 个开发集场景 × 4 个条件 × 2 次 = 128 次，种子 20261101。
- **环境**：私有库 `avelis_e1`；权限快照在运行前后都是 `8c9e6c60…`，没有变化。
- **数据**：
  - 原始结果：本 worktree 的 `.demo/experiments/e2/20261031-123611.json`；
  - 摘要：同目录的 `.summary.json`，用 `experiments/agent-v2/e2_summary.py` 生成。
- **判分**：评分器 v2.1.1（运行前冻结）。之后用 v2.1.3 统一重判这 128 次，判定变化 0 次（见 `docs/archive/agent-v2/scorer-v2_1.md` 第 6 节）。
- **冒烟**：运行前另跑了 3 次（h10 × R / H / RH），不计入 128 次，结果文件是 `20261031-123130.json`。
- **说明：这些都是开发题，结果不是盲测，也不代表产品性能。每格只有 2 次，差 1–2 次的结果在噪声范围内。**

## 1. 每种条件的 v2.1.1 结果

| 条件 | 通过 | 首要原因命中 | 关键数值命中 | 待澄清 | 修订 | 模型读了口径闭包以外的表 | 用到了 R 列出的参考表 | 费用（元） | 每次通过的费用 | 完成运行耗时 中位 / P90（秒） |
|---|---|---|---|---|---|---|---|---|---|---|
| B0 | 12/32 | 10 | 19 | 8 | 18 | 1 | — | 1.522 | 0.127 | 77 / 111 |
| +R | 10/32 | 8 | 18 | 9 | 16 | 11 | 11 | 1.488 | 0.149 | 78 / 98 |
| +H | 10/32 | 9 | 18 | 8 | 16 | 1 | — | 1.672 | 0.167 | 88 / 117 |
| +R+H | 13/32 | 11 | 19 | 9 | 18 | 12 | 12 | 1.841 | 0.142 | 100 / 124 |

- **主效应**（通过数，每臂 64 次）：R +1，H +1；交互项 +5，即 (RH − H) − (R − B0)。都在噪声范围内。
- **按场景配对，与 B0 相比**：
  - +R：改善 0 个场景，退化 g01、d03（各少 1 次）；
  - +H：改善 h09、h07，退化 d06、c02；
  - +R+H：改善 h09、d02，退化 g01。
- **排除 4 个全部待澄清的场景后**（每条件 24 次）：通过数分别是 B0 12、R 10、H 10、RH 13，结论不变。

## 2. 第一处偏离（每次运行）

逐次结果见附录。主类按固定规则从轨迹特征初判（B，推断），规则写在 `e2_summary.py` 的头部。首要原因、读了哪些表、状态、费用都是运行记录里的事实（V）。

| 条件 | 通过 | 范围（待澄清） | 证据未发现 | 证据解释 | 报告绑定 |
|---|---|---|---|---|---|
| B0 | 12 | 8 | 11 | 1 | 0 |
| +R | 10 | 9 | 6 | 7 | 0 |
| +H | 10 | 8 | 12 | 1 | 1 |
| +R+H | 13 | 9 | 1 | 9 | 0 |

R 把“证据未发现”转成了“证据解释”：B0 是 11 次和 1 次，RH 是 1 次和 9 次。也就是说，模型读到了口径闭包以外的表，但首要原因仍然没有指向机制。这和 E1 中 D2 → D3 的现象相同。

## 3. 按任务书第 14 节的解释

**R（证据发现）**
- **发现率确实提高了**：读了闭包外的表，B0 1/32，R 11/32，RH 12/32。11 次和 12 次里，用到的都是 R 列出的参考表。模型几乎不调用 find / neighbors / path（R 0 次，RH 1 次），基本只靠预检放进上下文的参考表。
- **自动推断的召回有限**（决定 ADR 0013 的“模型提议”值不值得做）。对机制在口径闭包以外的场景：
  - 投放表（d02、h07）：8/8 次在参考表里，模型查到了 7/8；
  - 营销活动表（h10、h05、h12）：0/12。它和口径来源之间没有同名的键，也没有已知的共同取值；
  - 调价表（e01）：没有测到，e01 全部停在待澄清。但它在 h10 的参考表里出现过，说明可以经商品编号这个共享键找到。
  - 机制在闭包以内的场景（d03、d06、g01），血缘本身就能覆盖，不需要 R。
- **没有转化为解释成功**：d02、h07 在 R 臂查到了投放表，首要原因命中仍然是 1/8，和非 R 臂相同。
- **结论**：自动推断在“有共享键”的地方有效，但发现之后还卡在解释这一步。没有共享键的事件表（营销活动）找不到，需要用户告知，或者靠经验积累。这支持 ADR 0013 的方向：模型提议、用户确认。

**H（证据解释）**
- **没有可测量的效果**：H 主效应 +1。改善的场景（h09、h07）与退化的场景（d06、c02）各两个；看轨迹，退化来自修订失败、首要原因选择的波动等，不是系统性的。
- **触发条件很少满足**。H 的修订要求上下文里有“没被引用的事件类数据集”，但不开 R 时，模型几乎不读闭包以外的表（1/32），所以条件基本不成立。修订次数 H 16、B0 18，没有增加。
- **H 的修订反馈没有记成步骤**，触发次数不能从运行记录直接统计，这一项**未验证**（已记为后续修正：反馈要作为步骤留痕）。
- **交互**：RH 是四种条件里通过最多的（13），可能说明 H 只有在 R 带来事件证据之后才有作用对象。但差值在噪声范围内，不能认定存在交互作用。
- **结论**：按第 14 节的规则，“R 有效、H 无效就保留 R，不为保住架构叙事强行上 H”。目前 R 只在发现率上有效，端到端没有改善；H 没有证明有效。两者都**不改变默认路径**（开关保持关闭）。

**比 R、H 都大的因素：待澄清**
- e01、e02、f02、a02 在全部 4 个条件下 8/8 停在待澄清，g01 另有 2 次，合计 34/128（27%）。这些运行在理解阶段就停了，R 和 H 都没有机会起作用。
- 追问的内容都是合理的范围确认：
  - “均价”是件单价还是客单价；
  - “客单价”有两个口径，而且都没有类目维度；
  - “前后几天”指哪几天；
  - 发起人看不到门店维度。
- 现在的评测把待澄清一律判为不通过。按 ADR 0013 的方向，这正是“理解检查点”：应该由用户回答，然后继续分析。下一阶段要靠交互模拟评测才能衡量这部分。
- a02（权限对抗题）停在待澄清而不是结构化拒绝：没有越权，但 v2.1.1 要求 `permission.lost`，所以不通过。这一点在交互模拟里也要单独衡量。

**成本与耗时**
- 每次通过的费用：B0 0.127 元，R 0.149 元，H 0.167 元，RH 0.142 元。RH 约为 B0 的 1.1 倍，在第 14 节“不超过 1.5 倍”以内。
- 完成运行的耗时中位：B0 77 秒，RH 100 秒。都超过了“快路径中位 ≤40 秒”，在“深调查中位 ≤90 秒”附近，未达标就如实标示。
- 费用：E2 128 次 6.52 元，冒烟 0.22 元，合计 6.74 元（授权 20 元，剩余约 13.26 元）。

## 4. 下一步优先级（详见 `docs/archive/agent-v2/next-phase-plan.md`）

1. ADR 0013 第一版：先做对话式检查点（理解、假设），再做经验闭环。E2 显示待澄清和“没有共享键的事件表”是两个最大的缺口，正好分别对应理解确认和假设确认。
2. R 降级为“提议”的来源之一。它对有共享键的事件表有效，可以保留；但它不能单独解决发现问题。
3. H 暂不扩展。等假设检查点能把事件证据带进上下文以后，再在交互模拟里重新衡量。
4. 思考强度对照（B0 @ 最高）：区分问题在模型能力还是系统设计，按计划在剩余预算内运行。

## 附录：逐次运行

| 场景 | 条件 · 次 | 状态 | 通过 | 首因 | 数值 | 首要原因（V） | 读了闭包外的表（V） | 第一处偏离（B） | 费用 | 秒 |
|---|---|---|---|---|---|---|---|---|---|---|
| h10 | B0 · 1 | succeeded | ✗ | ✗ | ✓ | member:{"member": "华南", "dimension": "region", "metric_code"… | — | 证据未发现 | 0.075 | 83.8 |
| h10 | B0 · 2 | succeeded | ✗ | ✗ | ✓ | member:{"member": "华南", "dimension": "region", "metric_code"… | — | 证据未发现 | 0.061 | 80.0 |
| h10 | R · 1 | succeeded | ✗ | ✗ | ✓ | member:{"member": "华南", "dimension": "region", "metric_code"… | — | 证据未发现 | 0.067 | 77.9 |
| h10 | R · 2 | succeeded | ✗ | ✗ | ✓ | member:{"member": "华南", "dimension": "region", "metric_code"… | — | 证据未发现 | 0.029 | 42.5 |
| h10 | H · 1 | succeeded | ✗ | ✗ | ✓ | member:{"member": "华南", "dimension": "region", "metric_code"… | — | 证据未发现 | 0.053 | 74.6 |
| h10 | H · 2 | succeeded | ✗ | ✗ | ✓ | member:{"member": "华南", "dimension": "region", "metric_code"… | — | 证据未发现 | 0.069 | 87.5 |
| h10 | RH · 1 | succeeded | ✗ | ✗ | ✓ | calendar:{"evidence_id": "D6#1"}:本期 | ad_spend_daily | 证据解释 | 0.078 | 98.3 |
| h10 | RH · 2 | succeeded | ✗ | ✗ | ✓ | member:{"member": "华南", "dimension": "region", "metric_code"… | ad_spend_daily、coupon_usage | 证据解释 | 0.084 | 99.8 |
| e01 | B0 · 1 | clarify | ✗ | ✗ | ✗ | — | — | 范围（待澄清） | 0.001 | 1.7 |
| e01 | B0 · 2 | clarify | ✗ | ✗ | ✗ | — | — | 范围（待澄清） | 0.001 | 1.6 |
| e01 | R · 1 | clarify | ✗ | ✗ | ✗ | — | — | 范围（待澄清） | 0.001 | 1.6 |
| e01 | R · 2 | clarify | ✗ | ✗ | ✗ | — | — | 范围（待澄清） | 0.001 | 1.4 |
| e01 | H · 1 | clarify | ✗ | ✗ | ✗ | — | — | 范围（待澄清） | 0.001 | 1.5 |
| e01 | H · 2 | clarify | ✗ | ✗ | ✗ | — | — | 范围（待澄清） | 0.001 | 1.5 |
| e01 | RH · 1 | clarify | ✗ | ✗ | ✗ | — | — | 范围（待澄清） | 0.001 | 1.5 |
| e01 | RH · 2 | clarify | ✗ | ✗ | ✗ | — | — | 范围（待澄清） | 0.001 | 1.4 |
| h07 | B0 · 1 | succeeded | ✗ | ✗ | ✓ | member:{"member": "华东", "dimension": "region", "metric_code"… | — | 证据未发现 | 0.095 | 111.0 |
| h07 | B0 · 2 | succeeded | ✗ | ✗ | ✓ | member:{"member": "线上", "dimension": "channel", "metric_code… | — | 证据未发现 | 0.078 | 106.3 |
| h07 | R · 1 | succeeded | ✗ | ✗ | ✓ | member:{"member": "线上", "dimension": "channel", "metric_code… | ad_spend_daily、coupon_usage | 证据解释 | 0.057 | 72.3 |
| h07 | R · 2 | succeeded | ✗ | ✗ | ✓ | member:{"member": "线上", "dimension": "channel", "metric_code… | ad_spend_daily | 证据解释 | 0.094 | 113.1 |
| h07 | H · 1 | succeeded | ✗ | ✗ | ✓ | member:{"member": "线上", "dimension": "channel", "metric_code… | — | 证据未发现 | 0.044 | 60.0 |
| h07 | H · 2 | succeeded | ✓ | ✓ | ✓ | member:{"evidence_id": "D18#1"}:douyin · A_20260601_20260621 | — | — | 0.095 | 115.8 |
| h07 | RH · 1 | succeeded | ✗ | ✗ | ✓ | member:{"member": "华东", "dimension": "region", "metric_code"… | ad_spend_daily | 证据解释 | 0.099 | 123.7 |
| h07 | RH · 2 | succeeded | ✗ | ✗ | ✓ | member:{"member": "线上", "dimension": "channel", "metric_code… | ad_spend_daily | 证据解释 | 0.084 | 106.3 |
| f02 | B0 · 1 | clarify | ✗ | ✗ | ✗ | — | — | 范围（待澄清） | 0.002 | 5.2 |
| f02 | B0 · 2 | clarify | ✗ | ✗ | ✗ | — | — | 范围（待澄清） | 0.002 | 4.8 |
| f02 | R · 1 | clarify | ✗ | ✗ | ✗ | — | — | 范围（待澄清） | 0.002 | 5.2 |
| f02 | R · 2 | clarify | ✗ | ✗ | ✗ | — | — | 范围（待澄清） | 0.002 | 5.9 |
| f02 | H · 1 | clarify | ✗ | ✗ | ✗ | — | — | 范围（待澄清） | 0.002 | 5.6 |
| f02 | H · 2 | clarify | ✗ | ✗ | ✗ | — | — | 范围（待澄清） | 0.002 | 4.8 |
| f02 | RH · 1 | clarify | ✗ | ✗ | ✗ | — | — | 范围（待澄清） | 0.002 | 5.9 |
| f02 | RH · 2 | clarify | ✗ | ✗ | ✗ | — | — | 范围（待澄清） | 0.002 | 4.7 |
| h09 | B0 · 1 | succeeded | ✗ | ✗ | ✓ | factor:{"factor_metric_code": "ec_visitors"}:进店客流 | — | 证据未发现 | 0.061 | 74.8 |
| h09 | B0 · 2 | succeeded | ✗ | ✗ | ✓ | factor:{"factor_metric_code": "ec_visitors"}:进店客流 | — | 证据未发现 | 0.063 | 83.5 |
| h09 | R · 1 | succeeded | ✗ | ✗ | ✓ | factor:{"factor_metric_code": "ec_visitors"}:进店客流 | — | 证据未发现 | 0.065 | 89.4 |
| h09 | R · 2 | succeeded | ✗ | ✗ | ✓ | factor:{"factor_metric_code": "ec_visitors"}:进店客流 | ad_spend_daily | 证据解释 | 0.069 | 89.0 |
| h09 | H · 1 | succeeded | ✓ | ✓ | ✓ | calendar:{"evidence_id": "D22#3"}:高位日部分订单差额 · h_cur - h_prev | — | — | 0.087 | 103.4 |
| h09 | H · 2 | succeeded | ✗ | ✗ | ✓ | factor:{"factor_metric_code": "ec_visitors"}:进店客流 | — | 证据未发现 | 0.049 | 79.0 |
| h09 | RH · 1 | succeeded | ✗ | ✗ | ✓ | factor:{"factor_metric_code": "ec_visitors"}:进店客流 | coupon_usage | 证据解释 | 0.089 | 106.7 |
| h09 | RH · 2 | succeeded | ✓ | ✓ | ✓ | calendar:{"evidence_id": "D13#1"}:本期 | — | — | 0.085 | 119.0 |
| d02 | B0 · 1 | succeeded | ✗ | ✗ | ✗ | member:{"member": "线上", "dimension": "channel", "metric_code… | — | 证据未发现 | 0.064 | 49.3 |
| d02 | B0 · 2 | succeeded | ✗ | ✗ | ✓ | member:{"member": "线上", "dimension": "channel", "metric_code… | — | 证据未发现 | 0.081 | 115.1 |
| d02 | R · 1 | succeeded | ✗ | ✗ | ✗ | — | ad_spend_daily | 证据解释 | 0.147 | 184.2 |
| d02 | R · 2 | succeeded | ✗ | ✗ | ✓ | member:{"member": "线上", "dimension": "channel", "metric_code… | — | 证据未发现 | 0.047 | 67.8 |
| d02 | H · 1 | succeeded | ✗ | ✗ | ✗ | member:{"member": "线上", "dimension": "channel", "metric_code… | — | 证据未发现 | 0.082 | 96.0 |
| d02 | H · 2 | succeeded | ✗ | ✗ | ✓ | member:{"member": "线上", "dimension": "channel", "metric_code… | ad_spend_daily、campaign | 证据解释 | 0.109 | 125.7 |
| d02 | RH · 1 | succeeded | ✗ | ✗ | ✗ | member:{"member": "线上", "dimension": "channel", "metric_code… | ad_spend_daily | 证据解释 | 0.082 | 102.1 |
| d02 | RH · 2 | succeeded | ✓ | ✓ | ✓ | member:{"evidence_id": "D13#5"}:douyin | ad_spend_daily | — | 0.088 | 98.3 |
| e02 | B0 · 1 | clarify | ✗ | ✗ | ✗ | — | — | 范围（待澄清） | 0.001 | 1.5 |
| e02 | B0 · 2 | clarify | ✗ | ✗ | ✗ | — | — | 范围（待澄清） | 0.001 | 1.6 |
| e02 | R · 1 | clarify | ✗ | ✗ | ✗ | — | — | 范围（待澄清） | 0.001 | 1.9 |
| e02 | R · 2 | clarify | ✗ | ✗ | ✗ | — | — | 范围（待澄清） | 0.001 | 1.7 |
| e02 | H · 1 | clarify | ✗ | ✗ | ✗ | — | — | 范围（待澄清） | 0.001 | 1.5 |
| e02 | H · 2 | clarify | ✗ | ✗ | ✗ | — | — | 范围（待澄清） | 0.001 | 1.7 |
| e02 | RH · 1 | clarify | ✗ | ✗ | ✗ | — | — | 范围（待澄清） | 0.001 | 1.7 |
| e02 | RH · 2 | clarify | ✗ | ✗ | ✗ | — | — | 范围（待澄清） | 0.001 | 1.4 |
| h05 | B0 · 1 | succeeded | ✗ | ✗ | ✓ | member:{"member": "华北", "dimension": "region", "metric_code"… | — | 证据未发现 | 0.035 | 36.8 |
| h05 | B0 · 2 | succeeded | ✗ | ✗ | ✓ | member:{"member": "华北", "dimension": "region", "metric_code"… | campaign | 证据解释 | 0.083 | 104.7 |
| h05 | R · 1 | succeeded | ✗ | ✗ | ✓ | member:{"member": "华北", "dimension": "region", "metric_code"… | ad_spend_daily | 证据解释 | 0.075 | 82.9 |
| h05 | R · 2 | succeeded | ✗ | ✗ | ✓ | member:{"member": "华北", "dimension": "region", "metric_code"… | ad_spend_daily、coupon_usage | 证据解释 | 0.059 | 75.5 |
| h05 | H · 1 | succeeded | ✗ | ✗ | ✓ | calendar:{"evidence_id": "D15#1"}:A 8月12-19日 | — | 证据未发现 | 0.082 | 90.7 |
| h05 | H · 2 | succeeded | ✗ | ✗ | ✓ | factor:{"evidence_id": "D12#3"}:8月12-19日 | — | 证据未发现 | 0.076 | 93.4 |
| h05 | RH · 1 | succeeded | ✗ | ✗ | ✓ | member:{"member": "华北", "dimension": "region", "metric_code"… | ad_spend_daily、coupon_usage、price_change | 证据解释 | 0.102 | 114.5 |
| h05 | RH · 2 | succeeded | ✗ | ✗ | ✓ | member:{"member": "华北", "dimension": "region", "metric_code"… | ad_spend_daily、coupon_usage | 证据解释 | 0.117 | 152.9 |
| d06 | B0 · 1 | succeeded | ✓ | ✓ | ✓ | member:{"member": "西南", "dimension": "region", "metric_code"… | — | — | 0.083 | 90.9 |
| d06 | B0 · 2 | succeeded | ✓ | ✓ | ✓ | member:{"member": "物流", "dimension": "ticket_category", "met… | — | — | 0.058 | 71.7 |
| d06 | R · 1 | succeeded | ✓ | ✓ | ✓ | member:{"member": "物流", "dimension": "ticket_category", "met… | shipment、refund | — | 0.065 | 66.2 |
| d06 | R · 2 | succeeded | ✓ | ✓ | ✓ | member:{"member": "物流", "dimension": "ticket_category", "met… | shipment | — | 0.087 | 95.6 |
| d06 | H · 1 | succeeded | ✗ | ✗ | ✗ | — | — | 证据未发现 | 0.076 | 85.9 |
| d06 | H · 2 | succeeded | ✗ | ✓ | ✓ | member:{"member": "西南", "dimension": "region", "metric_code"… | — | 报告绑定 | 0.106 | 129.8 |
| d06 | RH · 1 | succeeded | ✓ | ✓ | ✓ | member:{"member": "物流", "dimension": "ticket_category", "met… | shipment | — | 0.089 | 89.5 |
| d06 | RH · 2 | succeeded | ✓ | ✓ | ✓ | member:{"member": "物流", "dimension": "ticket_category", "met… | shipment | — | 0.093 | 106.8 |
| f01 | B0 · 1 | succeeded | ✓ | ✓ | ✓ | factor:{"factor_metric_code": "ec_orders"}:订单数 | — | — | 0.037 | 50.7 |
| f01 | B0 · 2 | succeeded | ✓ | ✓ | ✓ | factor:{"factor_metric_code": "ec_orders"}:订单数 | — | — | 0.034 | 51.6 |
| f01 | R · 1 | succeeded | ✓ | ✓ | ✓ | factor:{"factor_metric_code": "ec_orders"}:订单数 | — | — | 0.049 | 47.7 |
| f01 | R · 2 | succeeded | ✓ | ✓ | ✓ | factor:{"factor_metric_code": "ec_orders"}:订单数 | — | — | 0.037 | 42.9 |
| f01 | H · 1 | succeeded | ✓ | ✓ | ✓ | factor:{"factor_metric_code": "ec_orders"}:订单数 | — | — | 0.020 | 24.8 |
| f01 | H · 2 | succeeded | ✓ | ✓ | ✓ | factor:{"factor_metric_code": "ec_orders"}:订单数 | — | — | 0.027 | 33.8 |
| f01 | RH · 1 | succeeded | ✓ | ✓ | ✓ | factor:{"factor_metric_code": "ec_orders"}:订单数 | — | — | 0.038 | 39.3 |
| f01 | RH · 2 | succeeded | ✓ | ✓ | ✓ | factor:{"factor_metric_code": "ec_orders"}:订单数 | — | — | 0.034 | 49.0 |
| c02 | B0 · 1 | succeeded | ✓ | ✓ | ✓ | calendar:{"evidence_id": "D6#1"}:本期 | — | — | 0.062 | 64.8 |
| c02 | B0 · 2 | succeeded | ✓ | ✓ | ✓ | calendar:{"evidence_id": "D6#1"}:本期 | — | — | 0.053 | 69.2 |
| c02 | R · 1 | succeeded | ✓ | ✓ | ✓ | calendar:{"metric_code": "ec_orders"}:None | ad_spend_daily | — | 0.037 | 50.6 |
| c02 | R · 2 | succeeded | ✓ | ✓ | ✓ | calendar:{"evidence_id": "D6#1"}:本期 | — | — | 0.025 | 39.3 |
| c02 | H · 1 | succeeded | ✗ | ✗ | ✓ | member:{"member": "线上", "dimension": "channel", "metric_code… | — | 证据未发现 | 0.068 | 74.5 |
| c02 | H · 2 | succeeded | ✗ | ✗ | ✓ | member:{"member": "线上", "dimension": "channel", "metric_code… | — | 证据未发现 | 0.062 | 84.8 |
| c02 | RH · 1 | succeeded | ✓ | ✓ | ✓ | calendar:{"evidence_id": "D6#1"}:本期 | — | — | 0.068 | 86.5 |
| c02 | RH · 2 | succeeded | ✓ | ✓ | ✓ | calendar:{}:None | — | — | 0.064 | 85.6 |
| g01 | B0 · 1 | succeeded | ✓ | ✓ | — | member:{"member": "北京三号店", "dimension": "store", "metric_cod… | — | — | 0.067 | 83.4 |
| g01 | B0 · 2 | succeeded | ✓ | ✓ | — | member:{"member": "北京三号店", "dimension": "store", "metric_cod… | — | — | 0.086 | 105.4 |
| g01 | R · 1 | clarify | ✗ | ✗ | ✗ | — | — | 范围（待澄清） | 0.004 | 1.9 |
| g01 | R · 2 | succeeded | ✓ | ✓ | — | member:{"member": "北京三号店", "dimension": "store", "metric_cod… | — | — | 0.088 | 97.6 |
| g01 | H · 1 | succeeded | ✓ | ✓ | — | member:{"member": "北京三号店", "dimension": "store", "metric_cod… | — | — | 0.049 | 59.8 |
| g01 | H · 2 | succeeded | ✓ | ✓ | — | member:{"member": "北京三号店", "dimension": "store", "metric_cod… | — | — | 0.079 | 99.5 |
| g01 | RH · 1 | clarify | ✗ | ✗ | ✗ | — | — | 范围（待澄清） | 0.001 | 1.5 |
| g01 | RH · 2 | succeeded | ✓ | ✓ | — | member:{"member": "北京三号店", "dimension": "store", "metric_cod… | — | — | 0.063 | 91.2 |
| d03 | B0 · 1 | succeeded | ✓ | ✓ | ✓ | member:{"member": "成都太古里店", "dimension": "store", "metric_co… | — | — | 0.046 | 58.9 |
| d03 | B0 · 2 | succeeded | ✓ | ✓ | ✓ | member:{"member": "成都太古里店", "dimension": "store", "metric_co… | — | — | 0.032 | 50.7 |
| d03 | R · 1 | succeeded | ✓ | ✓ | ✓ | member:{"member": "成都太古里店", "dimension": "store", "metric_co… | — | — | 0.055 | 56.0 |
| d03 | R · 2 | succeeded | ✗ | ✗ | ✗ | — | — | 证据未发现 | 0.051 | 77.6 |
| d03 | H · 1 | succeeded | ✓ | ✓ | ✓ | member:{"member": "成都太古里店", "dimension": "store", "metric_co… | — | — | 0.037 | 48.6 |
| d03 | H · 2 | succeeded | ✓ | ✓ | ✓ | member:{"member": "成都太古里店", "dimension": "store", "metric_co… | — | — | 0.069 | 88.5 |
| d03 | RH · 1 | succeeded | ✓ | ✓ | ✓ | member:{"member": "成都太古里店", "dimension": "store", "metric_co… | — | — | 0.043 | 54.0 |
| d03 | RH · 2 | succeeded | ✓ | ✓ | ✓ | member:{"member": "成都太古里店", "dimension": "store", "metric_co… | — | — | 0.059 | 83.8 |
| n01 | B0 · 1 | succeeded | ✓ | — | — | member:{"member": "标准店", "dimension": "store_type", "metric_… | — | — | 0.093 | 112.1 |
| n01 | B0 · 2 | succeeded | ✓ | — | — | member:{"member": "标准店", "dimension": "store_type", "metric_… | — | — | 0.029 | 41.8 |
| n01 | R · 1 | succeeded | ✓ | — | — | member:{"member": "北京二号店", "dimension": "store", "metric_cod… | ad_spend_daily | — | 0.078 | 87.3 |
| n01 | R · 2 | succeeded | ✓ | — | — | member:{"member": "北京二号店", "dimension": "store", "metric_cod… | — | — | 0.065 | 86.9 |
| n01 | H · 1 | succeeded | ✓ | — | — | member:{"member": "标准店", "dimension": "store_type", "metric_… | — | — | 0.096 | 116.9 |
| n01 | H · 2 | succeeded | ✓ | — | — | member:{"member": "标准店", "dimension": "store_type", "metric_… | — | — | 0.048 | 60.7 |
| n01 | RH · 1 | succeeded | ✓ | — | — | member:{"member": "北京二号店", "dimension": "store", "metric_cod… | — | — | 0.099 | 112.5 |
| n01 | RH · 2 | succeeded | ✓ | — | — | member:{"member": "标准店", "dimension": "store_type", "metric_… | — | — | 0.057 | 79.5 |
| a02 | B0 · 1 | clarify | ✗ | — | — | — | — | 范围（待澄清） | 0.001 | 1.6 |
| a02 | B0 · 2 | clarify | ✗ | — | — | — | — | 范围（待澄清） | 0.001 | 1.5 |
| a02 | R · 1 | clarify | ✗ | — | — | — | — | 范围（待澄清） | 0.001 | 1.6 |
| a02 | R · 2 | clarify | ✗ | — | — | — | — | 范围（待澄清） | 0.001 | 1.6 |
| a02 | H · 1 | clarify | ✗ | — | — | — | — | 范围（待澄清） | 0.001 | 1.8 |
| a02 | H · 2 | clarify | ✗ | — | — | — | — | 范围（待澄清） | 0.001 | 1.2 |
| a02 | RH · 1 | clarify | ✗ | — | — | — | — | 范围（待澄清） | 0.001 | 1.4 |
| a02 | RH · 2 | clarify | ✗ | — | — | — | — | 范围（待澄清） | 0.001 | 1.5 |
| h12 | B0 · 1 | succeeded | ✗ | ✗ | ✓ | member:{"evidence_id": "D8#1"}:礼盒 | — | 证据未发现 | 0.067 | 74.6 |
| h12 | B0 · 2 | succeeded | ✗ | ✗ | ✓ | member:{"evidence_id": "D16#1"}:线上 · 饮品 | — | 证据未发现 | 0.068 | 81.4 |
| h12 | R · 1 | succeeded | ✗ | ✗ | ✓ | member:{"evidence_id": "D13#1"}:线上 | ad_spend_daily | 证据解释 | 0.074 | 81.9 |
| h12 | R · 2 | succeeded | ✗ | ✗ | ✓ | member:{"evidence_id": "D12#1"}:礼盒 | — | 证据未发现 | 0.055 | 75.4 |
| h12 | H · 1 | succeeded | ✗ | ✗ | ✓ | member:{"evidence_id": "D11#1"}:礼盒 | — | 证据未发现 | 0.083 | 89.4 |
| h12 | H · 2 | succeeded | ✗ | ✗ | ✓ | member:{"evidence_id": "D11#1"}:礼盒 · 本期 | — | 证据未发现 | 0.096 | 116.6 |
| h12 | RH · 1 | succeeded | ✗ | ✗ | ✓ | member:{"evidence_id": "D13#1"}:礼盒 | ad_spend_daily | 证据解释 | 0.122 | 143.7 |
| h12 | RH · 2 | succeeded | ✗ | ✗ | ✓ | member:{"evidence_id": "D11#2"}:线上 | — | 证据未发现 | 0.092 | 108.5 |
