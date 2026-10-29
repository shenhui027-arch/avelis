"""把采集发现的变更关联到数据库里做改动的用户（ADR 0008 第 2 点）。不猜测：

- matched       候选审计记录只来自一个数据库用户 → 记为该用户
- ambiguous     候选来自多个用户 → 不记用户，列出全部候选
- unmatched     窗口内没有该对象的审计记录 → 不记用户，说明原因
- unavailable   引擎声明了审计能力，但本次读取失败（附数据库报错）
- unsupported   引擎未接入审计来源
- not_applicable 推导出的变化（不是对该对象执行的 DDL），或首次采集
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from avelis.config import get_settings
from avelis.connectors.ddl import AuditUnavailable, DdlEvent, mentions_column, redefines
from avelis.engines.base import DDL_AUDIT, Engine
from avelis.models import Asset

DERIVED = {"derivation_changed", "upstream_broken", "identity_link_changed", "server_identity_changed"}
STATUSES = ("matched", "ambiguous", "unmatched", "unavailable", "unsupported", "not_applicable")
CANDIDATE_LIMIT = 10


def _names_of(event: dict[str, Any], asset: Asset | None) -> set[tuple[str, str]]:
    names = set()
    if asset is not None:
        names.add((asset.db_name.lower(), asset.name.lower()))
    for side in ("before", "after"):
        qualified = (event.get(side) or {}).get("name")
        if isinstance(qualified, str) and qualified.count(".") >= 2:
            _, db, name = qualified.split(".", 2)
            names.add((db.lower(), name.lower()))
    return names


def _candidate(e: DdlEvent) -> dict[str, Any]:
    return {"at": e.at.isoformat(), "db_user": e.db_user, "statement_type": e.statement_type, "summary": e.summary}


def attribute(session: Session, engine: Engine, connector: Any, events: list[dict[str, Any]],
              since: datetime | None, until: datetime) -> dict[str, Any]:
    """为 events（尚未写库的变更事件字典）补上 actor_db_user / attribution_status / attribution。
    返回本次审计读取的概况（写入变更集摘要）。"""
    source = getattr(connector, "ddl_audit_source", None)
    if since is None:
        for e in events:
            _set(e, "not_applicable", reason="首次采集：对象在开始观察之前已存在，不对应本窗口内的 DDL")
        return {"status": "not_applicable", "reason": "首次采集"}
    if not engine.supports(DDL_AUDIT) or not hasattr(connector, "ddl_events"):
        for e in events:
            _set(e, "not_applicable" if e["kind"] in DERIVED else "unsupported",
                 reason=f"{engine.label} 适配器未接入 DDL 审计来源" if e["kind"] not in DERIVED
                 else "推导出的变化，不是对该对象执行的 DDL")
        return {"status": "unsupported", "reason": f"{engine.label} 适配器未接入 DDL 审计来源"}
    slack = timedelta(seconds=get_settings().ddl_audit_slack_seconds)
    window = {"since": (since - slack).isoformat(), "until": (until + slack).isoformat(),
              "slack_seconds": int(slack.total_seconds()), "source": source}
    try:
        audit_events = connector.ddl_events(since - slack, until + slack)
    except Exception as exc:  # noqa: BLE001 - 读取审计失败只影响归因，不让采集失败
        if not isinstance(exc, AuditUnavailable):
            exc = AuditUnavailable(f"{type(exc).__name__}: {exc}")
        cause, label = unavailable_cause(str(exc))
        for e in events:
            if e["kind"] in DERIVED:
                _set(e, "not_applicable", reason="推导出的变化，不是对该对象执行的 DDL")
            else:
                _set(e, "unavailable", source=source, window=window, cause=cause,
                     reason=f"{label}，无法归因（数据库报错：{exc}）")
        return {"status": "unavailable", "source": source, "cause": cause, "error": str(exc)[:500], "window": window}
    by_object: dict[tuple[str, str], list[DdlEvent]] = {}
    for a in audit_events:
        for db, name in a.objects:
            by_object.setdefault(((db or "").lower(), (name or "").lower()), []).append(a)
    for e in events:
        if e["kind"] in DERIVED:
            _set(e, "not_applicable", reason="推导出的变化，不是对该对象执行的 DDL")
            continue
        asset = session.get(Asset, e["asset_id"]) if e.get("asset_id") else None
        names = _names_of(e, asset)
        candidates = sorted({id(a): a for n in names for a in by_object.get(n, [])}.values(), key=lambda a: a.at)
        basis = {"source": source, "window": window, "objects": sorted(".".join(n) for n in names)}
        if not candidates:
            _set(e, "unmatched", **basis,
                 reason=f"审计窗口内没有 {'、'.join(basis['objects'])} 的 DDL 记录"
                        "（可能是审计日志尚未落盘、改动发生在审计开启之前，或经由未审计的路径）")
            continue
        if e.get("column_name"):
            # 字段级变化只认点名了该列的语句；整体重定义（CREATE、ALTER VIEW … AS）可能不逐列点名，也算候选。
            # 同一对象上只改了别的列的语句不算：那是猜测
            by_column = [a for a in candidates if mentions_column(a.statement, e["column_name"])]
            basis.update(column=e["column_name"], column_match=bool(by_column))
            candidates = by_column or [a for a in candidates if redefines(a.statement)]
            if not candidates:
                _set(e, "unmatched", **basis,
                     reason=f"窗口内有 {'、'.join(basis['objects'])} 的 DDL，但没有一条涉及列 {e['column_name']}"
                            "（该列的改动可能经由未审计的路径）")
                continue
        users = sorted({a.db_user for a in candidates})
        statements = [_candidate(a) for a in candidates[-CANDIDATE_LIMIT:]]
        if len(users) == 1:
            _set(e, "matched", actor=users[0], **basis, statements=statements,
                 reason=f"窗口内该对象的 {len(candidates)} 条 DDL 都来自 {users[0]}")
        else:
            _set(e, "ambiguous", **basis, candidates=users, statements=statements,
                 reason=f"窗口内该对象的 DDL 来自多个用户（{'、'.join(users)}），无法确定是谁")
    return {"status": "ok", "source": source, "window": window, "audit_events": len(audit_events)}


_NO_PERMISSION = ("permission denied", "access denied", "command denied", "not enough privileges", "code: 497")
_NOT_ENABLED = ("does not exist", "unknown table", "code: 60.", "doesn't exist")


def unavailable_cause(error: str) -> tuple[str, str]:
    """把审计来源的读取报错归为：采集账号无权读取 / 数据源未开启 DDL 审计 / 其他。是否开启、是否授权由数据源方负责，
    这里只如实说明为什么不可用，不做变通。"""
    text = error.lower()
    if any(k in text for k in _NO_PERMISSION):
        return "no_permission", "采集账号无权读取 DDL 审计"
    if any(k in text for k in _NOT_ENABLED):
        return "not_enabled", "数据源未开启 DDL 审计（审计来源不存在）"
    return "error", "读取 DDL 审计失败"


def _set(event: dict[str, Any], status: str, actor: str | None = None, **detail: Any) -> None:
    event["attribution_status"] = status
    event["actor_db_user"] = actor
    event["attribution"] = detail
