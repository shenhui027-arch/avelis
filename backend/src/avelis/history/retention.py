"""保留期清理（ADR 0008 第 5 点）。

- 截止时间 = 现在 − AVELIS_HISTORY_RETENTION_DAYS（默认 1095 天；0 表示不清理）。
- 不删当前生效的版本、不删仍被非终态口径版本引用的版本、不删未处理的变更集。
- 专用路径：同一事务内 SET LOCAL avelis.history_purge = on，只追加触发器只在此标志下放行 DELETE。
- 先核验、后清理：链不完整就不清理，如实报告。
- 清理锚点：链上追加一条 history_anchor，登记本次形成的序号空洞及两端哈希，剩余的链仍可核验；清理写审计。
"""

from __future__ import annotations

import bisect
import logging
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from avelis import audit
from avelis.config import get_settings
from avelis.db import session_factory, utcnow
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
    plan["asset_version"] = _ids(session, """
        SELECT v.id FROM asset_version v
        WHERE v.superseded_at < :cutoff
          AND NOT EXISTS (SELECT 1 FROM asset a WHERE a.current_version_id = v.id)
          AND v.id NOT IN (SELECT e.value->>'version_id' FROM metric_version mv, jsonb_each(mv.asset_versions) e
                           WHERE mv.status = ANY(:statuses) AND e.value->>'version_id' IS NOT NULL)
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


def purge(*, retention_days: int | None = None, now: datetime | None = None, actor: User | None = None,
          dry_run: bool = False) -> dict[str, Any]:
    """执行一次保留期清理并返回结果。retention_days 缺省取配置；now 只用于验收模拟时间推移。"""
    days = get_settings().history_retention_days if retention_days is None else retention_days
    if days <= 0:
        return {"status": "disabled", "retention_days": days}
    now = now or utcnow()
    cutoff = now - timedelta(days=days)
    session = session_factory()()
    try:
        session.connection(execution_options={"isolation_level": "REPEATABLE READ"})
        session.execute(text("SELECT pg_advisory_xact_lock(hashtextextended('avelis:history_purge', 0))"))
        actor = session.get(User, actor.id) if actor else None
        chain = integrity.load_chain(session)
        registered = integrity.registered_gaps(session)
        issues = integrity.check(chain, registered)
        if issues:
            report = {"ok": False, "first_break": issues[0], "issue_count": len(issues),
                      "issues": issues[:integrity.ISSUE_LIMIT]}
            session.rollback()
            _record_refusal(actor, cutoff, days, report)
            raise PurgeRefused(report)
        plan = _plan(session, cutoff)
        counts = {t: len(ids) for t, ids in plan.items() if ids}
        if dry_run:
            session.rollback()
            return {"status": "dry_run", "cutoff": cutoff.isoformat(), "retention_days": days, "counts": counts}
        if not counts:  # 没有超期历史：只记审计（worker 据此判断上次清理时间），不留锚点
            audit.record(session, actor, "history.purge", "history", None, cutoff=cutoff.isoformat(),
                         retention_days=days, counts={}, gaps=0)
            session.commit()
            return {"status": "nothing", "cutoff": cutoff.isoformat(), "retention_days": days, "counts": {}}
        session.execute(text("SET LOCAL avelis.history_purge = 'on'"))
        deleted_seq: set[int] = set()
        for table in ORDER:
            removed = _delete(session, table, plan.get(table, []))
            ids = [r[0] for r in removed]
            deleted_seq.update(r[1] for r in removed if r[1] is not None)
            if table in integrity.BY_NAME and ids:
                mark_ids = _ids(session, "SELECT id FROM history_mark WHERE table_name = :t AND row_id = ANY(:ids)",
                                t=table, ids=ids)
                marks = _delete(session, "history_mark", mark_ids)
                deleted_seq.update(r[1] for r in marks if r[1] is not None)
                if marks:
                    counts["history_mark"] = counts.get("history_mark", 0) + len(marks)
        session.execute(text("SET LOCAL avelis.history_purge = 'off'"))
        gaps = _new_gaps(chain, deleted_seq, registered)
        anchor = HistoryAnchor(kind="purge", cutoff=cutoff, retention_days=days, counts=counts, gaps=gaps,
                               actor_name=actor.username if actor else audit.SYSTEM)
        session.add(anchor)
        session.flush()
        audit.record(session, actor, "history.purge", "history_anchor", anchor.id, cutoff=cutoff.isoformat(),
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
