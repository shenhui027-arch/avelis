# Governance events and webhooks

Governance events are the stable external contract defined by [ADR 0018](decisions/0018-governance-events-and-webhooks.md). The backend projects audit records into events, writes an outbox item for each matching subscription in the same transaction, and lets the worker send generic JSON. The first release provides no WeCom, DingTalk, or Feishu formats, pre-events, or in-process third-party listeners.

This document follows the [`EventType` and action mapping](../backend/src/avelis/events/__init__.py), [subscription API](../backend/src/avelis/api/events.py), and [delivery implementation](../backend/src/avelis/events/delivery.py). Other languages: [简体中文](events.md) · [繁體中文](events.zh-TW.md).

## Event types and audit mapping

Types are append-only and must not be renamed. The table shows the outcome on the normal trigger path. A guarded write that raises an exception keeps its operation type with outcome `failure`. Unmapped audit actions remain in the audit log without producing external events. Every subscribable enum value also maps to the identically named event for explicit recording and failure capture. `webhook.test` is excluded from audit projection and cannot be subscribed to.

| Event type | Audit action | Normal outcome | Trigger |
|---|---|---|---|
| `metric_version.submitted` | `metric_version.submit` | `success` | A metric version is submitted for review. The submission write has a failure guard. |
| `metric_version.published` | `metric_version.approve` | `success` | A metric version is approved and published. The publication write has a failure guard. |
| `metric_version.rejected` | `metric_version.reject` | `success` | A metric version is rejected. The rejection write has a failure guard. |
| `metric_version.affected` | `metric_version.affected` | `success` | Collection change analysis marks a published metric, or a derived metric affected by its component version, as affected. |
| `metric_version.invalidated` | `metric_version.invalidated` | `success` | Review resolves an affected metric with `new_version` or `retired`, invalidating the old version. This write has a failure guard. |
| `join_path.approved` | `join_path.decide` | `success` | The decision is `approved` and the final path status is `approved`: all currently required business domains have approved. A single domain approval that leaves the path pending emits no event. The approval write has a failure guard. |
| `join_path.affected` | `join_path.reapproval_required` | `success` | Changed path evidence clears existing approvals and requires approval from the current business domains. |
| `collect.completed` | `collect` | `success` | A datasource collection finishes successfully. |
| `collect.failed` | `collect.failed` | `failure` | Collection raises an exception, or the worker recovers a timed-out collection job. |
| `collect.schema_changed` | `collect.schema_changed` | `success` | Collection detects changes and creates a change set. Detail contains change counts, never raw DDL. |
| `lineage_import.completed` | `import.completed` | `success` | Deterministic lineage parsing finishes. Batch status may be `done`, `needs_params`, or `needs_review`; this does not mean every edge is approved or published. Auxiliary model failures retain the existing handling semantics. |
| `lineage_import.failed` | `import.failed`; the same-name action is used by failure guards | `failure` | Import creation, parameter updates, or a background import raises an exception, or the worker recovers a timed-out import job. |
| `attribution.unavailable` | `attribution.unavailable` | `success` | Collection attribution is unavailable with cause `no_permission` or `not_enabled`. Success means the unavailability fact was recorded, not that attribution succeeded. Other unavailable causes do not emit this type. |
| `auth.login_failure_threshold` | `auth.login_failure_threshold` | `failure` | The failure count for “username + source address” first reaches the configured threshold. Further requests during lockout do not repeat the threshold event. object.id can be `null` for an unknown account. |
| `webhook.test` | None (queued directly; the operation is audited as `webhook_subscription.test`) | `success` | Produced only by “Send test” and delivered only to the tested subscription, bypassing its event_types and outcomes filters. It cannot be subscribed to; receivers can ignore test events by this type. |

## Contract and redaction

The request body contains only the contract fields below. All timestamps are UTC. `id` is the stable event ID, normally the audit row ID, and remains the same across subscriptions. `X-Avelis-Delivery` is an outbox ID for one subscription, distinct from the event ID.

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

| Field | Meaning |
|---|---|
| `id` | Stable string used by the receiver for deduplication. |
| `type` | One of the finite enum values above. |
| `outcome` | `success` or `failure`; interpret it together with type. |
| `at` | Event time with a UTC timezone. |
| `actor` | `{id, name}`, or the string `"system"` for a system operation. |
| `object` | `{type, id, name?}`; id is `null` when unknown. The current audit projection does not enrich name. |
| `detail` | Redacted structured fields, defaulting to `{}`. |
| `error` | Required for failure: `{code, reason}`. Usually omitted on success. |

In this failure example, reason is a predefined safe short reason, never the raw exception, database error, or remote response:

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

Audit writes recursively remove sensitive keys for passwords, credentials, tokens, authorization headers, API keys, raw SQL, and data rows, and replace recognized sensitive text with `[redacted]`. External events additionally project detail through an allowlist of structured identifiers, statuses, decisions, counts, and cause codes. Free-form note, reason, warnings, parser output, and source errors are not delivered. Subscriptions do not extend data permissions or send business rows or query results.

## Transactions, history, and access

Successful audit records and matching event_outbox items commit with the business transaction and roll back with it. A failure guard wraps the write or background job outside its business transaction and commit: it rolls back that work, then records the failure audit and matching outbox items in a separate short transaction. Failure audits use the global hash chain from [ADR 0008](decisions/0008-history-integrity.md). If writing the failure itself also fails, the code logs without exception text and re-raises the original business exception, preserving the existing error response. Failure capture covers the initial related writes and background jobs, not ordinary read failures.

The blocked metric submission path, `submit_blocked`, is an explicit exception to that business rollback rule. It preserves the existing API behavior: commit the check results and blocking audit, then raise the original conflict. The failure event still uses a separate short transaction.

Event metadata is stored in `audit_event.detail._governance` as type, outcome, and optional error. There are no new audit_event columns and no updates to historical rows. New audits use the existing sealing mechanism; the hash definition of old rows is unchanged.

Subscriptions, event types, and outbox endpoints require administrator access; authenticated non-administrators receive `403`. Existing `GET /api/audit` permissions remain: administrators see all audits, while other users need object visibility and always have `outcome=failure` records filtered out. Historical audits without governance metadata are treated as success. Creating, updating, deleting, rotating, testing, and redelivering dead items write audit records without signing secrets.

## Delivery and signature verification

Each worker runs one independent delivery loop in `jobs.py`, scheduled by `worker.schedule_seconds`, without waiting for long business jobs or history cleanup to finish. Each round processes at most 50 due `pending` items whose subscription is enabled, sharing a fixed 5-second delivery budget. Subscriptions take turns: each receives one item before the next pass processes another item for each subscription. An older backlog for one subscription therefore cannot consume the entire round before other subscriptions get a turn.

Network waits are cancelled at the total request deadline: at most 5 seconds per request and 3 seconds for connecting. Each request is also capped at a fair share of the round's remaining budget, reserving time for subscriptions still awaiting their turn. Once exhausted, the round stops; unattempted items keep their state for the next round. Synchronous DNS validation, database transactions, and local scheduling add overhead outside this budget. One timeout skips that subscription's remaining items for the round. Other failures retain the two-consecutive-failures rule; success resets that count, and each round starts fresh. Requests follow no redirects and ignore environment proxies. Only `2xx` is success. Failures back off by 1, 2, 4, 8… minutes, capped at 60 minutes by default, with at most 8 attempts per item. The item becomes `dead` at the limit and an administrator may redeliver it. Redelivery preserves event and outbox IDs, resets attempts to zero, and restores pending.

When a collection or import Job times out, the same transaction sets `result.abandoned_governance` to `pending`. Later scans retry unfinished governance finalization. Under a Job row lock, the related object's terminal state, failure audit, event outbox, and `done` marker commit together. A finalization error rolls everything back; retries cannot duplicate audits or events and do not rerun business work.

When matching subscriptions, the audit path reads enabled subscriptions in ID order with `FOR KEY SHARE`. This prevents concurrent subscription deletion from making the outbox foreign key fail and rolling back business work. Delivery selects rows with `FOR NO KEY UPDATE SKIP LOCKED` and commits each item separately. Delivery workers and subscription management operations still exclude each other, while remaining compatible with the business transaction's KEY SHARE locks.

A crash after the receiver accepts a request but before local commit can cause duplicates: delivery is at least once, and receivers must deduplicate by event id. Ordering is not guaranteed; use at and business object versions when needed. Delivery failure cannot roll back committed business work or audits. last_error stores only safe codes such as timeout, network_error, url_rejected, or http_500, never response bodies.

Every request carries these headers:

```text
Content-Type: application/json
X-Avelis-Event: metric_version.published
X-Avelis-Delivery: delivery_001
X-Avelis-Timestamp: 1793692800
X-Avelis-Signature: sha256=<hex digest>
```

The signed input is `timestamp + "." + body`, where timestamp is the Unix-seconds header string and body is the original received bytes. Do not parse and reserialize the JSON before verification. This receiver example recommends a 300-second replay window; it is not a server setting. Persist processed event IDs and make deduplication atomic with business handling.

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
    expected = (
        "sha256="
        + hmac.new(
            secret.encode("utf-8"),
            timestamp_bytes + b"." + raw_body,
            hashlib.sha256,
        ).hexdigest()
    )
    return hmac.compare_digest(expected.encode("ascii"), signature_bytes)
```

The server generates the secret and returns it once on creation or rotation. Listing and updating return no secret. Storage uses the same encrypt_secret/decrypt_secret functions as datasource passwords: Fernet with a key derived from AVELIS_SECRET through HKDF. Rotation does not rewrite event snapshots; pending and retried deliveries use the subscription's current secret at send time. Coordinate the receiver's key change.

## URLs and configuration

URLs must use http/https, with no URL credentials, fragments, control characters, or invalid ports. Validation runs on creation, update, test, redelivery, and every actual send. By default, every DNS result must be an allowed public address; private, loopback, link-local, multicast, reserved, and restricted IPv6 transition addresses are rejected. The connection pins a verified IP while preserving the original HTTP Host, TLS SNI, and certificate hostname validation, preventing a second DNS lookup from bypassing validation. Only ConnectError or ConnectTimeout permits trying another verified address. All addresses share the remaining request deadline of at most 5 seconds, with at most 3 seconds for connecting, also bounded by a fair share of the round's remaining budget. Read timeouts, write errors, and HTTP failures leave retries to the outbox instead of switching addresses.

| Environment variable | Configuration key | Default | Effect |
|---|---|---|---|
| `AVELIS_WEBHOOK_ALLOW_PRIVATE_URLS` | `webhook.allow_private_urls` | `false` | Explicitly allow private addresses for local development; keep the production default disabled. |
| `AVELIS_WEBHOOK_MAX_ATTEMPTS` | `webhook.max_attempts` | `8` | Maximum attempts per outbox item; positive integer. |
| `AVELIS_WEBHOOK_RETRY_MAX_MINUTES` | `webhook.retry_max_minutes` | `60` | Exponential backoff cap in minutes; positive integer. |
| `AVELIS_WORKER_SCHEDULE_SECONDS` | `worker.schedule_seconds` | `60` | Interval in seconds for business scans and the independent delivery loop; long business jobs do not block delivery. |
| `AVELIS_EVENTS_DELIVERED_RETENTION_DAYS` | `events.delivered_retention_days` | `30` | Days to retain delivered snapshots from `delivered_at`; nonnegative integer, with `0` disabling cleanup. |

Successful delivery snapshots are retained for 30 days by default, balancing recent troubleshooting needs and storage growth. Each worker scan deletes at most 500 expired `delivered` records alongside OAuth cleanup, independently of the history cleanup interval, using `SKIP LOCKED` to defer rows currently in use. Unexpired records and `pending` items remain. `dead` items are not purged automatically, allowing administrators to investigate, repair, and redeliver them. There is no reliable timestamp for entering the dead state, so creation time cannot substitute for the retention starting point; no day-based dead-record cleanup setting is provided. Deleting a subscription still cascades to all its outbox items.

## Administrative API and examples

All endpoints use existing Bearer authentication and require administrators. Example IDs and secrets are placeholders.

| Method and path | Request | Success response |
|---|---|---|
| `GET /api/events/types` | None | `200`, `[{"type":"metric_version.submitted"}, ...]`, only the 14 subscribable types, excluding `webhook.test`. |
| `GET /api/webhook-subscriptions` | None | `200`, subscription array without secret. |
| `POST /api/webhook-subscriptions` | name, url, nonempty event_types; optional outcomes and enabled | `201`, subscription and one-time secret. |
| `PATCH /api/webhook-subscriptions/{id}` | Partial update of the same fields; outcomes=null clears filtering | `200`, subscription without secret. |
| `DELETE /api/webhook-subscriptions/{id}` | None | `204`, empty body; deletes all of the subscription's outbox items. |
| `POST /api/webhook-subscriptions/{id}/rotate-secret` | None | `200`, subscription and one-time new secret. |
| `POST /api/webhook-subscriptions/{id}/test` | None | `202`, newly queued OutboxOut; asynchronous, not confirmation of receipt. |
| `GET /api/event-outbox` | Optional subscription_id, status, limit, offset | `200`, page object. status is pending/delivered/dead; limit defaults to 50, range 1–200; offset defaults to 0. |
| `POST /api/event-outbox/{id}/redeliver` | None | `200`, reset OutboxOut; only dead items qualify. |

Create a subscription:

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

event_types must be nonempty and is deduplicated. Missing or null outcomes disables outcome filtering. For example, `PATCH /api/webhook-subscriptions/subscription_001` with `{"enabled":false,"outcomes":null}` returns updated subscription fields without secret. Rotation returns the same shape with a new secret. Missing subscriptions return `404`; invalid URLs or fields return `422`; testing or redelivering a disabled subscription, or redelivering a non-dead item, returns `409`.

A test event always uses `type="webhook.test"`, `outcome="success"`, and a new event ID. Its object contains the subscription's type, id, and name, and detail is `{"test":true}`. It bypasses event_types and outcomes filters and is delivered only to the tested subscription; receivers can ignore test events by type. Creating or updating a subscription with `webhook.test` in event_types returns `422`. Test and redelivery responses are single outbox objects. A list returns the same objects in items, for example `GET /api/event-outbox?subscription_id=subscription_001&status=pending&limit=50&offset=0`:

```json
{
  "items": [{
    "id": "delivery_001",
    "event_id": "event_test_001",
    "subscription_id": "subscription_001",
    "payload": {
      "id": "event_test_001",
      "type": "webhook.test",
      "outcome": "success",
      "at": "2026-11-03T08:02:00Z",
      "actor": {"id":"user_001","name":"admin"},
      "object": {"type":"webhook_subscription","id":"subscription_001","name":"Governance alerts"},
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

## FINDINGS: migration, lifecycle, and validation boundaries

- New tables are webhook_subscription and event_outbox. The latter stores the event JSON snapshot, subscription ID, status, attempts, next attempt time, safe error code, and completion time. `(subscription_id, event_id)` is unique. event_id deliberately has no audit_event foreign key, so audit retention cleanup does not block remaining snapshot deliveries.
- Migration `0020_governance_events` follows `0019_metric_ai_context`, adds no audit columns, and does not recalculate old hashes. Upgrades must also apply `0023_event_outbox_cleanup_index` after `0022_oauth_cleanup_indexes`. Its partial index on `(delivered_at, id) WHERE status = 'delivered'` supports only delivered-snapshot retention cleanup; the existing `(status, next_attempt_at)` index continues to support due-delivery queries.
- Deleting a subscription cascades to all its pending, delivered, and dead outbox items, while original audits remain. Use enabled=false to pause while preserving delivery records. Disabling leaves pending items in place; enabling again lets delivery resume.
- The outbox stores an event snapshot. URL, enabled state, and signing secret are read from the current subscription at send time. Changes to type or outcome filters affect future enqueueing only, without deleting existing snapshots. Rotated secrets also apply to later attempts for old snapshots.
- Failure error only promises a safe code and predefined short reason, not raw exception diagnostics. Unavailable attribution does not fail collection, and auxiliary import model failures preserve existing deterministic-result semantics.
- Compatibility change: shared audit redaction removes the previously recorded raw SQL from newly written `analysis.explore` audits. The old regression assertion expected `log.sql`; it now follows the user's explicit requirement that SQL must not be stored in audits. Exploration execution and dataset evidence within existing permissions retain their behavior. Audits still retain safe row counts and `digest`; sealed historical rows are not rewritten.
- Validation entry points include `backend/tests/unit/test_governance_events*.py`, `test_governance_event_business.py`, `backend/tests/db/test_governance_events_migration.py`, the complete unit suite, Ruff, business-neutrality checks, and `cd frontend && pnpm gen:api && pnpm typecheck`. Unit and static checks do not replace real PostgreSQL migration/hash-chain, multi-worker row-lock, or external HTTPS receiver acceptance. Actual results belong in the delivery report; this document does not claim they have passed.
