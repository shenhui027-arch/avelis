"""保留期清理（ADR 0008 第 5 点）。

- 截止时间 = 现在 − AVELIS_HISTORY_RETENTION_DAYS（默认 1095 天；0 表示不清理）。
- 不删当前生效的版本、不删仍被非终态口径版本引用的版本、不删未处理的变更集。
- 专用路径：同一事务内 SET LOCAL avelis.history_purge = on，只追加触发器只在此标志下放行 DELETE（TRUNCATE 一律拒绝）。
- 先核验、后清理：链不完整就不清理，如实报告。
- 清理锚点：链上追加一条 history_anchor，登记本次形成的序号空洞及两端哈希，剩余的链仍可核验；清理写审计。
"""

from __future__ import annotations

import bisect
import logging
import time
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from avelis import audit
from avelis.config import get_settings
from avelis.db import advisory_lock, session_factory, utcnow
from avelis.history import integrity
from avelis.models import HistoryAnchor, User

log = logging.getLogger(__name__)

ACTIVE_METRIC_STATUSES = ("draft", "pending_review", "published", "affected")


class PurgeRefused(Exception):
    """链核验未通过：不清理。report 为核验结果。"""

    def __init__(self, report: dict[str, Any]):
        super().__init__("历史哈希链核验未通过，未执行清理")
        self.report = report


def _ids(session: Session, sql: str, **params: Any) -> list[str]:
    return list(session.execute(text(sql), params).scalars())


def _delete(session: Session, table: str, ids: list[str], key: str = "id") -> list[tuple[str, int | None]]:
    """删除并返回 (id, chain_seq)；非链上表 chain_seq 为空。"""
    if not ids:
        return []
    chained = table in integrity.BY_NAME
    returning = "id, chain_seq" if chained else "id, NULL"
    out = []
    for start in range(0, len(ids), 5000):
        out += [tuple(r) for r in session.execute(
            text(f"DELETE FROM {table} WHERE {key} = ANY(:ids) RETURNING {returning}"),
            {"ids": ids[start:start + 5000]})]
    return out


def _plan(session: Session, cutoff: datetime) -> dict[str, list[str]]:
    """按 ADR 0008 的规则挑出超期的历史。先挑血缘边、粒度，再挑版本（版本仍被剩余的边或粒度引用时保留）。"""
    p = {"cutoff": cutoff, "statuses": list(ACTIVE_METRIC_STATUSES)}
    plan: dict[str, list[str]] = {}
    plan["lineage_edge"] = _ids(session, "SELECT id FROM lineage_edge WHERE status = 'superseded' "
                                         "AND superseded_at < :cutoff", **p)
    plan["grain"] = _ids(session, "SELECT id FROM grain WHERE superseded_at < :cutoff", **p)
    # 受保护的资产版本：非终态口径版本自身的 asset_versions，以及派生口径在 components 中固定引用的
    # 组成版本的 asset_versions（组成版本可能已归档，但派生口径仍按它发布）
    plan["asset_version"] = _ids(session, """
        WITH active AS (SELECT id, asset_versions, components FROM metric_version WHERE status = ANY(:statuses)),
             pinned AS (SELECT mv.asset_versions FROM metric_version mv
                        WHERE mv.id IN (SELECT c->>'version_id' FROM active a, jsonb_array_elements(a.components) c
                                        WHERE jsonb_typeof(a.components) = 'array')),
             protected_versions AS (
               SELECT e.value->>'version_id' AS version_id
               FROM (SELECT asset_versions FROM active UNION ALL SELECT asset_versions FROM pinned) r,
                    jsonb_each(r.asset_versions) e
               WHERE jsonb_typeof(r.asset_versions) = 'object' AND e.value->>'version_id' IS NOT NULL)
        SELECT v.id FROM asset_version v
        WHERE v.superseded_at < :cutoff
          AND NOT EXISTS (SELECT 1 FROM asset a WHERE a.current_version_id = v.id)
          AND v.id NOT IN (SELECT version_id FROM protected_versions)
          AND NOT EXISTS (SELECT 1 FROM lineage_edge l WHERE l.target_asset_version_id = v.id
                          AND NOT (l.status = 'superseded' AND l.superseded_at < :cutoff))
          AND NOT EXISTS (SELECT 1 FROM grain g WHERE g.asset_version_id = v.id
                          AND (g.superseded_at IS NULL OR g.superseded_at >= :cutoff))""", **p)
    versions = plan["asset_version"]
    plan["column_version"] = _ids(session, "SELECT id FROM column_version WHERE asset_version_id = ANY(:v)", v=versions)
    plan["definition"] = _ids(session, "SELECT id FROM definition WHERE asset_version_id = ANY(:v)", v=versions)
    plan["parse_result"] = _ids(session, "SELECT id FROM parse_result WHERE definition_id = ANY(:d)",
                                d=plan["definition"])
    plan["change_set"] = _ids(session, """
        SELECT cs.id FROM change_set cs WHERE cs.status = 'resolved' AND cs.window_end < :cutoff
          AND NOT EXISTS (SELECT 1 FROM review_task t WHERE t.change_set_id = cs.id AND t.status <> 'resolved')""", **p)
    sets = plan["change_set"]
    plan["change_event"] = _ids(session, "SELECT id FROM change_event WHERE change_set_id = ANY(:s)", s=sets)
    plan["impact"] = _ids(session, "SELECT id FROM impact WHERE change_set_id = ANY(:s)", s=sets)
    plan["review_task"] = _ids(session, "SELECT id FROM review_task WHERE change_set_id = ANY(:s)", s=sets)
    plan["audit_event"] = _ids(session, "SELECT id FROM audit_event WHERE at < :cutoff", **p)
    plan["metric_version_transition"] = _ids(session, """
        SELECT t.id FROM metric_version_transition t WHERE t.at < :cutoff
          AND t.id <> (SELECT l.id FROM metric_version_transition l WHERE l.metric_version_id = t.metric_version_id
                       ORDER BY l.at DESC, l.chain_seq DESC NULLS LAST LIMIT 1)""", **p)
    plan["column_fingerprint"] = _ids(session, "SELECT id FROM column_fingerprint WHERE superseded_at < :cutoff", **p)
    plan["identity_link"] = _ids(session, "SELECT id FROM identity_link WHERE superseded_at < :cutoff", **p)
    plan["collection_run"] = _ids(session, """
        SELECT r.id FROM collection_run r WHERE r.finished_at < :cutoff
          AND NOT EXISTS (SELECT 1 FROM asset_version v WHERE v.observed_run_id = r.id AND NOT (v.id = ANY(:v)))
          AND NOT EXISTS (SELECT 1 FROM change_set cs WHERE cs.run_id = r.id AND NOT (cs.id = ANY(:s)))
          AND r.id <> (SELECT l.id FROM collection_run l WHERE l.datasource_id = r.datasource_id
                       ORDER BY l.started_at DESC LIMIT 1)
          AND r.id NOT IN (SELECT l.id FROM collection_run l WHERE l.datasource_id = r.datasource_id
                           AND l.status = 'succeeded' ORDER BY l.started_at DESC LIMIT 1)""",
                                  v=versions, s=sets, **p)
    return plan


# 删除顺序：先删引用方
ORDER = ("parse_result", "definition", "column_version", "lineage_edge", "grain", "asset_version", "change_event",
         "impact", "review_task", "change_set", "audit_event", "metric_version_transition", "column_fingerprint",
         "identity_link", "collection_run")


def _new_gaps(chain: integrity.Chain, deleted: set[int],
              registered: dict[tuple[int, int], dict[str, Any]]) -> list[dict[str, Any]]:
    """本次清理形成（或扩大）的序号空洞。两端哈希取自清理前已核验的链。"""
    if not chain.rows:
        return []
    deleted_sorted = sorted(deleted)
    remaining = [s for s in sorted(chain.rows) if s not in deleted]
    max_seq = max(chain.rows)
    gaps = []
    prev_seq, prev_hash = 0, integrity.GENESIS
    for seq in [*remaining, None]:
        end = seq - 1 if seq is not None else max_seq
        if end > prev_seq:
            i = bisect.bisect_left(deleted_sorted, prev_seq + 1)
            touched = i < len(deleted_sorted) and deleted_sorted[i] <= end
            if touched and (prev_seq + 1, end) not in registered:
                last_hash = chain.rows[seq].prev_hash if seq is not None else chain.rows[max_seq].row_hash
                gaps.append({"from_seq": prev_seq + 1, "to_seq": end, "before_hash": prev_hash, "last_hash": last_hash})
        if seq is not None:
            prev_seq, prev_hash = seq, chain.rows[seq].row_hash
    return gaps


RETRIES = 5
# 写入会让“已计划删除的行”重新获得保留资格的表：删除事务对它们加 SHARE 锁（挡住并发写入），再按同样的规则重新判定
ELIGIBILITY_TABLES = ("asset", "metric_version", "lineage_edge", "grain", "review_task")
LOCK_TIMEOUT = "5s"  # 删除事务里后续每次等锁的上限
STATEMENT_TIMEOUT = "120s"  # 删除事务里每条语句的上限


class _PlanChanged(Exception):
    """计划之后有行重新获得了保留资格：整轮重跑。"""


def purge(*, retention_days: int | None = None, now: datetime | None = None, actor: User | None = None,
          dry_run: bool = False) -> dict[str, Any]:
    """执行一次保留期清理并返回结果。retention_days 缺省取配置；now 只用于验收模拟时间推移。

    两段：① REPEATABLE READ 只读快照里核验全链、挑出超期的行、算好空洞；② 删除事务先对决定保留资格的表
    （ELIGIBILITY_TABLES）加 SHARE 锁（NOWAIT，拿不到就稍后整轮重跑，不与其他事务互等），在锁下按同样的规则重新挑一遍：
    计划里有行重新获得保留资格（如被驳回的口径改回草稿、仍引用旧版本）时整轮重跑；否则按 ID 删除并写锚点。
    删除数与计划不符（有人绕过触发器动过）时整个回滚。同一时刻只允许一个清理（会话级咨询锁）。"""
    days = get_settings().history_retention_days if retention_days is None else retention_days
    if days <= 0:
        return {"status": "disabled", "retention_days": days}
    now = now or utcnow()
    cutoff = now - timedelta(days=days)
    with advisory_lock("history_purge"):
        for attempt in range(1, RETRIES + 1):
            try:
                return _purge_once(cutoff, days, actor, dry_run)
            except _PlanChanged as exc:
                if attempt == RETRIES:
                    raise RuntimeError(f"保留期清理连续 {RETRIES} 次遇到保留资格变化，本次放弃：{exc}") from exc
                log.warning("保留期清理第 %s 次：计划之后有行重新获得保留资格（%s），整轮重跑", attempt, exc)
            except OperationalError as exc:
                state = getattr(exc.orig, "sqlstate", None)
                if state not in ("40001", "40P01", "55P03", "57014") or attempt == RETRIES:
                    raise
                log.warning("保留期清理第 %s 次遇到并发冲突（%s），稍后整轮重跑", attempt, state)
                time.sleep(min(2 * attempt, 10))
    raise AssertionError("unreachable")


def _purge_once(cutoff: datetime, days: int, actor: User | None, dry_run: bool) -> dict[str, Any]:
    snap = session_factory()()
    try:
        snap.connection(execution_options={"isolation_level": "REPEATABLE READ"})
        snap.execute(text("SET TRANSACTION READ ONLY"))
        chain = integrity.load_chain(snap)
        registered = integrity.registered_gaps(snap)
        issues = integrity.check(chain, registered)
        if issues:
            report = {"ok": False, "first_break": issues[0], "issue_count": len(issues),
                      "issues": issues[:integrity.ISSUE_LIMIT]}
            snap.rollback()
            _record_refusal(actor, cutoff, days, report)
            raise PurgeRefused(report)
        plan = _plan(snap, cutoff)
        marks: dict[str, list[str]] = {}
        for table in ORDER:
            if table in integrity.BY_NAME and plan.get(table):
                marks[table] = _ids(snap, "SELECT id FROM history_mark WHERE table_name = :t AND row_id = ANY(:ids)",
                                    t=table, ids=plan[table])
        plan["history_mark"] = [m for ids in marks.values() for m in ids]
    finally:
        snap.rollback()
        snap.close()
    counts = {t: len(ids) for t, ids in plan.items() if ids}
    if dry_run:
        return {"status": "dry_run", "cutoff": cutoff.isoformat(), "retention_days": days, "counts": counts}
    by_row = {(r.table, r.row_id): seq for seq, r in chain.rows.items()}
    planned_seq = {by_row[(t, i)] for t, ids in plan.items() if t in integrity.BY_NAME for i in ids}
    gaps = _new_gaps(chain, planned_seq, registered)
    session = session_factory()()
    try:
        actor_row = session.get(User, actor.id) if actor else None
        if not counts:  # 没有超期历史：只记审计（worker 据此判断上次清理时间），不留锚点
            audit.record(session, actor_row, "history.purge", "history", None, cutoff=cutoff.isoformat(),
                         retention_days=days, counts={}, gaps=0)
            session.commit()
            return {"status": "nothing", "cutoff": cutoff.isoformat(), "retention_days": days, "counts": {}}
        # 等待与执行都有上限：拿到 SHARE 锁后会短暂挡住这些表的写入，超时就回滚、稍后整轮重跑
        session.execute(text(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'"))
        session.execute(text(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT}'"))
        session.execute(text(f"LOCK TABLE {', '.join(ELIGIBILITY_TABLES)} IN SHARE MODE NOWAIT"))
        current = _plan(session, cutoff)
        lost = {t: len(set(ids) - set(current.get(t, []))) for t, ids in plan.items() if t != "history_mark"}
        lost = {t: n for t, n in lost.items() if n}
        if lost:
            raise _PlanChanged("、".join(f"{t} {n} 行" for t, n in lost.items()))
        chained = {t: ids for t, ids in plan.items() if t in integrity.BY_NAME and t != "history_mark" and ids}
        marks_now: set[str] = set()
        for t, ids in chained.items():  # 没有链上候选（只有超期的 grain 等）时为空集合
            marks_now.update(_ids(session, "SELECT id FROM history_mark WHERE table_name = :t AND row_id = ANY(:ids)",
                                  t=t, ids=ids))
        if marks_now != set(plan.get("history_mark", [])):
            raise _PlanChanged("待删除行的状态标记有变化")
        session.execute(text("SET LOCAL avelis.history_purge = 'on'"))
        deleted_seq: set[int] = set()
        for table in (*ORDER, "history_mark"):
            removed = _delete(session, table, plan.get(table, []))
            if len(removed) != len(plan.get(table, [])):
                raise RuntimeError(f"{table} 实际删除 {len(removed)} 行，与计划的 {len(plan.get(table, []))} 行不符；"
                                   "可能有人绕过触发器改动了历史，已整体回滚，请先核验")
            deleted_seq.update(r[1] for r in removed if r[1] is not None)
        session.execute(text("SET LOCAL avelis.history_purge = 'off'"))
        if deleted_seq != planned_seq:
            raise RuntimeError("删除的链上序号与核验时不一致，已整体回滚，请先核验")
        anchor = HistoryAnchor(kind="purge", cutoff=cutoff, retention_days=days, counts=counts, gaps=gaps,
                               actor_name=actor_row.username if actor_row else audit.SYSTEM)
        session.add(anchor)
        session.flush()
        audit.record(session, actor_row, "history.purge", "history_anchor", anchor.id, cutoff=cutoff.isoformat(),
                     retention_days=days, counts=counts, gaps=len(gaps))
        session.commit()
        log.info("保留期清理完成：截止 %s，%s", cutoff.isoformat(), counts)
        return {"status": "purged", "cutoff": cutoff.isoformat(), "retention_days": days, "counts": counts,
                "anchor_id": anchor.id, "gaps": gaps}
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _record_refusal(actor: User | None, cutoff: datetime, days: int, report: dict[str, Any]) -> None:
    session = session_factory()()
    try:
        audit.record(session, actor, "history.purge_refused", "history", None, cutoff=cutoff.isoformat(),
                     retention_days=days, first_break=report["first_break"], issue_count=report["issue_count"])
        session.commit()
    finally:
        session.close()


def purge_due() -> bool:
    """worker 用：保留期开启，且距上次清理（以审计为准，含被拒绝的）已超过 AVELIS_HISTORY_PURGE_INTERVAL_HOURS。"""
    settings = get_settings()
    if settings.history_retention_days <= 0:
        return False
    session = session_factory()()
    try:
        last = session.execute(text("SELECT max(at) FROM audit_event WHERE action IN "
                                    "('history.purge', 'history.purge_refused')")).scalar()
    finally:
        session.close()
    return last is None or utcnow() - last >= timedelta(hours=settings.history_purge_interval_hours)
