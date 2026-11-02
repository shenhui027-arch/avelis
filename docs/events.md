# 治理事件与 Webhook

治理事件是 [ADR 0018](decisions/0018-governance-events-and-webhooks.md) 规定的稳定对外契约。后端从审计记录投影事件，在同一事务中写入匹配订阅的发件箱，再由 worker 发送通用 JSON。首批不提供企业微信、钉钉、飞书格式，也不提供前置事件或进程内第三方监听器。

本文对应 [`EventType` 与 action 映射](../backend/src/avelis/events/__init__.py)、[订阅接口](../backend/src/avelis/api/events.py) 和[投递实现](../backend/src/avelis/events/delivery.py)。其他语言：[English](events.en.md) · [繁體中文](events.zh-TW.md)。

## 事件类型与审计映射

类型只追加，不改名。下表的 outcome 是正常触发路径的值；被失败守卫覆盖的写操作发生异常时，仍使用该操作类型，但 outcome 为 `failure`。未映射的审计 action 继续留底，不生成外部事件；每个枚举值本身也映射到同名事件，供明确记录和失败留底使用。

| 事件类型 | 审计 action | 正常 outcome | 触发条件 |
|---|---|---|---|
| `metric_version.submitted` | `metric_version.submit` | `success` | 口径版本成功提交复核。提交写操作受失败守卫保护。 |
| `metric_version.published` | `metric_version.approve` | `success` | 口径版本复核通过并发布。发布写操作受失败守卫保护。 |
| `metric_version.rejected` | `metric_version.reject` | `success` | 口径版本被驳回。驳回写操作受失败守卫保护。 |
| `metric_version.affected` | `metric_version.affected` | `success` | 采集变更分析使已发布口径或受其组成版本影响的派生口径进入受影响状态。 |
| `metric_version.invalidated` | `metric_version.invalidated` | `success` | 复核以 `new_version` 或 `retired` 处理受影响口径，旧版本失效。对应写操作受失败守卫保护。 |
| `join_path.approved` | `join_path.decide` | `success` | 本次决定为 `approved`，且路径最终状态为 `approved`，即当前所需业务域全部批准。单个业务域批准但路径尚未满足条件时不发事件。批准写操作受失败守卫保护。 |
| `join_path.affected` | `join_path.reapproval_required` | `success` | 路径依据变化，已有批准被清除，需要按当前业务域重新批准。 |
| `collect.completed` | `collect` | `success` | 一次数据源采集成功结束。 |
| `collect.failed` | `collect.failed` | `failure` | 采集抛出异常，或 worker 回收运行超时的采集任务。 |
| `collect.schema_changed` | `collect.schema_changed` | `success` | 采集发现变化并建立变更集；detail 带变更计数，不含 DDL 原文。 |
| `lineage_import.completed` | `import.completed` | `success` | 血缘导入的确定性解析处理结束，批次状态可能为 `done`、`needs_params` 或 `needs_review`；不表示全部边已批准或已经发布。辅助模型失败保留既有处理语义。 |
| `lineage_import.failed` | `import.failed`；同名 action 用于失败守卫 | `failure` | 创建导入、补参或导入后台任务抛出异常，或 worker 回收运行超时的导入任务。 |
| `attribution.unavailable` | `attribution.unavailable` | `success` | 采集归因不可用，且原因是 `no_permission` 或 `not_enabled`。这里的 success 表示成功记录了“归因不可用”这一事实，不表示归因成功。其他归因不可用原因不产生此类型。 |
| `auth.login_failure_threshold` | `auth.login_failure_threshold` | `failure` | 按“用户名 + 来源地址”计数，失败次数首次达到配置阈值。锁定期间后续请求不重复发阈值事件；账号不存在时 object.id 可为 `null`。 |

## 事件契约与脱敏

请求体只有以下契约字段；所有时间均为 UTC。`id` 是稳定的事件 ID，正常事件取审计行 ID，同一事件投向多个订阅时保持一致。`X-Avelis-Delivery` 是某个订阅的发件箱 ID，与事件 ID 不同。

```json
{
  "id": "event_001",
  "type": "metric_version.published",
  "outcome": "success",
  "at": "2026-11-03T08:00:00Z",
  "actor": {"id": "user_001", "name": "admin"},
  "object": {"type": "metric_version", "id": "version_001"},
  "detail": {"dependency_fingerprint": "example-fingerprint"}
}
```

| 字段 | 含义 |
|---|---|
| `id` | 稳定字符串，接收方据此去重。 |
| `type` | 上表的有限枚举值。 |
| `outcome` | `success` 或 `failure`，应与 type 一起判断。 |
| `at` | 事件发生时间，带 UTC 时区。 |
| `actor` | `{id, name}`；系统操作为字符串 `"system"`。 |
| `object` | `{type, id, name?}`；无法确定对象 ID 时 `id` 为 `null`，当前审计投影不补充 name。 |
| `detail` | 已脱敏的结构化字段，缺省为 `{}`。 |
| `error` | 仅失败时必需：`{code, reason}`。成功事件通常省略。 |

失败示例中，`reason` 是服务端允许的简短安全原因，既不是原始异常正文，也不是数据库或远端响应的副本：

```json
{
  "id": "event_002",
  "type": "collect.failed",
  "outcome": "failure",
  "at": "2026-11-03T08:01:00Z",
  "actor": "system",
  "object": {"type": "datasource", "id": "source_001"},
  "detail": {"trigger": "scheduled"},
  "error": {"code": "CollectionFailed", "reason": "操作失败"}
}
```

审计写入会递归移除口令、凭据、令牌、授权头、API key、SQL 原文、数据行等敏感键，识别到敏感文本时替换为 `[redacted]`。外部事件另用允许列表投影 detail，只保留运行/批次/任务 ID、状态、决定、计数、原因码等结构化字段；自由文本 note、reason、warnings、解析器输出和源端报错不对外发送。订阅不会扩大数据权限，也不发送业务数据行或查询结果。

## 事务、历史与权限

成功审计和匹配订阅的 `event_outbox` 在业务事务中一起提交，回滚时一起撤销。失败守卫应包在写操作或后台任务的业务事务及 commit 外层：先让业务事务回滚，再用单独短事务写失败审计和匹配的发件箱。失败审计仍使用 [ADR 0008](decisions/0008-history-integrity.md) 的全局哈希链。失败事件写入本身再失败时，只记不含异常正文的日志，并原样重新抛出最初的业务异常，不改原有错误响应。只记录首批相关写操作和后台任务的失败，不记录普通查询失败。

口径提交被阻断的 `submit_blocked` 路径是上述业务回滚规则的明确例外：沿用原 API 行为，先提交检查结果与阻断审计，再抛出原冲突异常；失败事件仍在另一个短事务中写入。

事件元数据复用 `audit_event.detail._governance` 保存 `type`、`outcome` 和可选 `error`，不为 audit_event 增加列，也不回写已有历史行。新审计内容按原有封链机制参与摘要，已有历史行的哈希定义保持不变。

订阅、事件类型和发件箱接口全部要求管理员权限，已认证的非管理员返回 `403`。现有 `GET /api/audit` 权限继续适用：管理员可见全部审计；非管理员须有对象可见权限，且始终过滤 `outcome=failure` 的审计。未带治理元数据的历史审计按 success 处理。订阅创建、修改、删除、轮换密钥、测试和 dead 重投都会写审计，不记录签名密钥。

## 投递与签名验证

worker 在 `jobs.py` 的定时扫描中处理到期且订阅启用的 `pending` 条目。每条请求使用 httpx，超时参数为 10 秒，不跟随重定向，不使用环境代理；这是 httpx 的各阶段超时语义，并非严格的 10 秒总墙钟上限。只有 `2xx` 算成功。失败按 1、2、4、8……分钟退避，默认间隔上限 60 分钟、最多尝试 8 次；达到上限后为 `dead`，可由管理员重投。重投保留事件 ID 和发件箱 ID，把 attempts 清零并改回 pending。

审计匹配订阅时，按订阅 ID 顺序以 `FOR KEY SHARE` 读取启用的订阅，防止并发删除订阅造成发件箱外键失败并回滚业务。投递事务使用 `FOR NO KEY UPDATE SKIP LOCKED` 锁住选中的行，逐条提交：投递 worker 与订阅管理操作仍互斥，同时兼容业务事务的 KEY SHARE 锁。本次锁变更目前只有 PostgreSQL SQL 编译与静态检查证据，尚未通过真实 PostgreSQL 并发验证。

远端已经接收、本地提交前进程退出时仍可能重复发送，因此语义是至少一次，接收方必须按事件 `id` 去重。不保证顺序；需要顺序时应结合 `at` 和业务对象版本判断。投递失败不回滚已经提交的业务或审计。`last_error` 只存安全错误码，例如 `timeout`、`network_error`、`url_rejected` 或 `http_500`，不保存响应正文。

每次发送带以下请求头：

```text
Content-Type: application/json
X-Avelis-Event: metric_version.published
X-Avelis-Delivery: delivery_001
X-Avelis-Timestamp: 1793692800
X-Avelis-Signature: sha256=<hex digest>
```

签名输入为 `timestamp + "." + body`，其中 timestamp 是请求头里的 Unix 秒数字符串，body 是收到的原始请求字节，不能先解析 JSON 再序列化。下面是接收方验证示例；300 秒窗口是接收方的防重放建议，不是服务端设置。仍需在持久存储中记录已处理的事件 ID，并让去重与业务处理原子完成。

```python
import hashlib
import hmac
import time


def verify_webhook(secret: str, headers, raw_body: bytes) -> bool:
    timestamp = headers.get("X-Avelis-Timestamp", "")
    signature = headers.get("X-Avelis-Signature", "")
    try:
        issued_at = int(timestamp)
        timestamp_bytes = timestamp.encode("ascii")
        signature_bytes = signature.encode("ascii")
    except (ValueError, UnicodeEncodeError):
        return False
    if abs(int(time.time()) - issued_at) > 300:
        return False
    expected = "sha256=" + hmac.new(
        secret.encode("utf-8"),
        timestamp_bytes + b"." + raw_body,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected.encode("ascii"), signature_bytes)
```

签名密钥由服务端生成，只在创建或轮换响应中返回一次；列表和修改响应不返回密钥。存储调用与数据源口令相同的 `encrypt_secret` / `decrypt_secret`，使用由 `AVELIS_SECRET` 经 HKDF 派生密钥的 Fernet 加密。轮换不会改写已有事件快照，未投递或重试条目在投递时使用订阅当前密钥；接收端应协调切换。

## URL 与配置

URL 只接受 http/https，不接受 URL 用户名/口令、片段、控制字符或无效端口。创建、修改、测试、重投及每次实际发送都会验证目标。默认要求 DNS 解析出的全部地址都是允许的公网地址，拒绝内网、回环、链路本地、组播、保留地址和受限 IPv6 转换地址。实际连接钉住本次已验证的 IP，同时保留原 HTTP Host、TLS SNI 和证书域名校验，避免验证后再次解析造成 DNS 重绑定。仅 `ConnectError` 或 `ConnectTimeout` 时尝试其余已验证地址，并按已用时间扣减 10 秒连接重试预算；读超时、写入异常或 HTTP 失败不切换地址，交给发件箱后续重试。

| 环境变量 | 配置文件键 | 默认值 | 作用 |
|---|---|---|---|
| `AVELIS_WEBHOOK_ALLOW_PRIVATE_URLS` | `webhook.allow_private_urls` | `false` | 本地开发可显式允许内网地址；生产默认保持关闭。 |
| `AVELIS_WEBHOOK_MAX_ATTEMPTS` | `webhook.max_attempts` | `8` | 单轮投递的最大尝试次数，必须为正整数。 |
| `AVELIS_WEBHOOK_RETRY_MAX_MINUTES` | `webhook.retry_max_minutes` | `60` | 指数退避的分钟上限，必须为正整数。 |
| `AVELIS_WORKER_SCHEDULE_SECONDS` | `worker.schedule_seconds` | `60` | worker 定时扫描间隔；发件箱的实际处理时间还受扫描和其他任务耗时影响。 |

## 管理接口与示例

以下接口沿用现有 Bearer 身份认证，全部要求管理员。示例 ID 与密钥均为占位值。

| 方法与路径 | 请求 | 成功响应 |
|---|---|---|
| `GET /api/events/types` | 无 | `200`，`[{"type":"metric_version.submitted"}, ...]`，返回全部 14 种。 |
| `GET /api/webhook-subscriptions` | 无 | `200`，订阅数组，不含 secret。 |
| `POST /api/webhook-subscriptions` | name、url、非空 event_types；可选 outcomes、enabled | `201`，订阅对象及一次性 secret。 |
| `PATCH /api/webhook-subscriptions/{id}` | 部分更新同上字段；outcomes=null 清除 outcome 过滤 | `200`，订阅对象，不含 secret。 |
| `DELETE /api/webhook-subscriptions/{id}` | 无 | `204`，无响应体；删除该订阅全部发件箱条目。 |
| `POST /api/webhook-subscriptions/{id}/rotate-secret` | 无 | `200`，订阅对象及一次性新 secret。 |
| `POST /api/webhook-subscriptions/{id}/test` | 无 | `202`，新建的 OutboxOut；异步发送，不代表远端已收到。 |
| `GET /api/event-outbox` | 可选 subscription_id、status、limit、offset | `200`，分页对象。status 为 pending/delivered/dead；limit 默认 50、范围 1–200，offset 默认 0。 |
| `POST /api/event-outbox/{id}/redeliver` | 无 | `200`，重置后的 OutboxOut；仅 dead 可重投。 |

创建订阅：

```http
POST /api/webhook-subscriptions
Authorization: Bearer <admin-access-token>
Content-Type: application/json

{
  "name": "Governance alerts",
  "url": "https://hooks.example.com/avelis",
  "event_types": ["metric_version.published", "collect.failed"],
  "outcomes": ["success", "failure"],
  "enabled": true
}
```

```json
{
  "id": "subscription_001",
  "name": "Governance alerts",
  "url": "https://hooks.example.com/avelis",
  "event_types": ["metric_version.published", "collect.failed"],
  "outcomes": ["success", "failure"],
  "enabled": true,
  "created_by": "user_001",
  "created_at": "2026-11-03T08:00:00Z",
  "updated_at": "2026-11-03T08:00:00Z",
  "secret": "<save-this-secret-once>"
}
```

`event_types` 非空且去重，outcomes 省略或为 null 表示不过滤结果。修改示例为 `PATCH /api/webhook-subscriptions/subscription_001`，请求 `{"enabled":false,"outcomes":null}`；返回上述订阅对象的新值，但无 secret。轮换接口返回相同形状且带新 secret。订阅不存在为 `404`，非法 URL 或字段为 `422`，停用订阅的测试或重投、非 dead 条目的重投为 `409`。

测试事件选用订阅 event_types 的第一个类型，以及 outcomes 的第一个值；未指定 outcomes 时使用该类型的默认 outcome。它使用新的事件 ID，object 指向订阅，detail 为 `{"test":true}`。失败测试另带 `{"code":"TEST","reason":"测试事件"}`。测试响应和重投响应都是单个发件箱对象；列表用相同对象组成 items，例如 `GET /api/event-outbox?subscription_id=subscription_001&status=pending&limit=50&offset=0`：

```json
{
  "items": [{
    "id": "delivery_001",
    "event_id": "event_test_001",
    "subscription_id": "subscription_001",
    "payload": {
      "id": "event_test_001",
      "type": "metric_version.published",
      "outcome": "success",
      "at": "2026-11-03T08:02:00Z",
      "actor": {"id":"user_001","name":"admin"},
      "object": {"type":"webhook_subscription","id":"subscription_001","name":null},
      "detail": {"test":true},
      "error": null
    },
    "status": "pending",
    "attempts": 0,
    "next_attempt_at": "2026-11-03T08:02:00Z",
    "last_error": null,
    "created_at": "2026-11-03T08:02:00Z",
    "delivered_at": null
  }],
  "total": 1,
  "limit": 50,
  "offset": 0
}
```

## FINDINGS：迁移、生命周期与验收边界

- 新增表为 `webhook_subscription` 与 `event_outbox`。后者存事件 JSON 快照、订阅 ID、状态、次数、下次尝试时间、安全错误码及完成时间；`(subscription_id, event_id)` 唯一，避免同一订阅重复入箱。event_id 不对 audit_event 建外键，审计按保留期清理不会阻断剩余快照投递。
- 迁移文件为 `0020_governance_events`，当前 `down_revision = "0018_import_batches"`。PR #81 已占用 `0019_metric_ai_context`；合并前 Claude 必须按实际 head 调整衔接。本迁移不增加审计列、不重算旧哈希。
- 删除订阅会级联删除其所有发件箱条目，包括 pending、delivered 和 dead；原审计不删除。若要暂停并保留投递记录，应修改 enabled=false。停用不会取消已有 pending 条目，重新启用后可继续投递。
- 发件箱保存的是事件快照；目标 URL、启用状态和签名密钥在投递时读取当前订阅。修改类型或 outcome 过滤只影响随后入箱，不删除已有快照；轮换密钥也用于旧快照的后续尝试。
- 失败 error 只承诺安全错误码和预定义短原因，不承诺提供原始异常诊断。归因不可用不使采集失败；导入辅助模型失败也不改变既有确定性结果保留语义。
- 兼容变化：统一审计脱敏使新写入的 `analysis.explore` 审计不再包含原先记录的 SQL 原文；旧回归断言原本要求 `log.sql`，现已按用户明确的“SQL 不落审计”约束调整。权限范围内的探索执行与数据集证据保留原行为，审计仍保留安全的行数计数及 `digest`；已封链历史行不改写。
- 验证入口包括 `backend/tests/unit/test_governance_events*.py`、`test_governance_event_business.py`、`backend/tests/db/test_governance_events_migration.py`、完整 unit 套件、Ruff、业务中立检查，以及 `cd frontend && pnpm gen:api && pnpm typecheck`。单元测试和静态检查不能替代真实 PostgreSQL 迁移/哈希链、多 worker 行锁或外部 HTTPS 接收端验收；实际执行结果以本次交付报告为准，本文不预先声明通过。
