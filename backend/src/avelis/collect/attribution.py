"""把采集发现的变更关联到数据库里做改动的用户（ADR 0008 第 2 点）。不猜测：

- matched        候选审计记录只来自一个数据库用户，且至少一条确定发生在两次快照之间 → 记为该用户
- ambiguous      候选来自多个用户，或候选只在窗口边界附近（无法确认是否在本窗口内生效）→ 不记用户，列出候选
- unmatched      窗口内没有该对象（字段级变化：没有点名该列或整体重定义）的审计记录 → 不记用户，说明原因
- unavailable    引擎声明了审计能力，但本次读取失败；cause 区分无权读取 / 未开启 / 来源结构不符 / 其他
- unsupported    引擎未接入审计来源
- not_applicable 推导出的变化（不是对该对象执行的 DDL），或首次采集

窗口：上次快照读取期间 [P_start, P_done] 与本次 [C_start, C_done]。
确定在窗口内 = 生效时间在 (P_done + 容差, C_start − 容差)；
落在两次快照读取期间（± 容差）的语句只算“边界候选”；
可能异步生效的语句（may_lag）在上次快照前 lag 秒内也算。连接器能按事务可见性判断的（PostgreSQL 记录了
两次快照），以连接器的判断为准。对象名按数据库目录的真实写法精确匹配（区分大小写）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from avelis.config import get_settings
from avelis.connectors.ddl import AuditUnavailable, DdlEvent, column_refs, redefines, same_identifier
from avelis.engines.base import DDL_AUDIT, Engine
from avelis.i18n import gettext
from avelis.models import Asset

DERIVED = {"derivation_changed", "upstream_broken", "identity_link_changed", "server_identity_changed"}
STATUSES = ("matched", "ambiguous", "unmatched", "unavailable", "unsupported", "not_applicable")
CANDIDATE_LIMIT = 10
CAUSE_LABEL = {"no_permission": "采集账号无权读取 DDL 审计", "not_enabled": "数据源未开启 DDL 审计",
               "incompatible": "DDL 审计来源的结构与连接器要求不符",
               "instance_changed": "数据源实例已切换，本轮重新建立归因基线，不做归因", "error": "读取 DDL 审计失败"}


@dataclass
class Window:
    """两次快照读取的时间（Avelis 时钟，UTC 不带时区）与连接器的审计游标。"""
    previous_start: datetime
    previous_done: datetime
    current_start: datetime
    current_done: datetime
    previous_cursor: dict[str, Any] | None = None
    current_cursor: dict[str, Any] | None = None
    instance_changed: bool = False  # 两次快照不是同一个实例（身份世代变化）：本轮不归因，重新建立基线


def _names_of(event: dict[str, Any], asset: Asset | None) -> set[tuple[str, str]]:
    names = set()
    if asset is not None:
        names.add((asset.db_name, asset.name))
    for side in ("before", "after"):
        qualified = (event.get(side) or {}).get("name")
        if isinstance(qualified, str) and qualified.count(".") >= 2:
            _, db, name = qualified.split(".", 2)
            names.add((db, name))
    return names


def _candidate(e: DdlEvent, certain: bool) -> dict[str, Any]:
    # 只保存审计来源的结构化信息，不保存任何语句原文或摘要（对象已在 basis.objects）
    out = {"at": e.at.isoformat(), "db_user": e.db_user, "statement_type": e.statement_type, "certain": certain}
    if e.xid:
        out["xid"] = e.xid
    return out


def attribute(session: Session, engine: Engine, connector: Any, events: list[dict[str, Any]],
              window: Window | None) -> dict[str, Any]:
    """为 events（尚未写库的变更事件字典）补上 actor_db_user / attribution_status / attribution。
    返回本次审计读取的概况（写入变更集摘要）。归因中的任何异常都不外抛：受影响的事件记为未知并写明原因，采集照常完成。"""
    try:
        return _attribute(session, engine, connector, events, window)
    except Exception as exc:  # noqa: BLE001 - 归因出错不能让采集失败
        reason = f"归因时出错，无法归因（{type(exc).__name__}: {exc}）"[:500]
        for e in events:
            if "attribution_status" not in e:
                _set(e, "not_applicable" if e["kind"] in DERIVED else "unavailable", cause="error", reason=reason)
        return {"status": "error", "error": reason}


def _attribute(session: Session, engine: Engine, connector: Any, events: list[dict[str, Any]],
               window: Window | None) -> dict[str, Any]:
    source = getattr(connector, "ddl_audit_source", None)
    if window is None:
        for e in events:
            _set(e, "not_applicable", reason="首次采集：对象在开始观察之前已存在，不对应本窗口内的 DDL")
        return {"status": "not_applicable", "reason": "首次采集"}
    unbound_reason = getattr(connector, "ddl_audit_unbound_reason", None)
    if not engine.supports(DDL_AUDIT) or not hasattr(connector, "ddl_events") or unbound_reason:
        reason = unbound_reason or f"{engine.label} 适配器未接入 DDL 审计来源"
        for e in events:
            _set(e, "not_applicable" if e["kind"] in DERIVED else "unsupported",
                 reason=reason if e["kind"] not in DERIVED else "推导出的变化，不是对该对象执行的 DDL")
        return {"status": "unsupported", "reason": reason}
    if window.instance_changed:
        reason = CAUSE_LABEL["instance_changed"]
        for e in events:
            _set(e, "not_applicable" if e["kind"] in DERIVED else "unavailable", source=source,
                 cause="instance_changed", reason=reason)
        return {"status": "unavailable", "source": source, "cause": "instance_changed", "reason": reason}
    slack = timedelta(seconds=get_settings().ddl_audit_slack_seconds)
    lag = timedelta(seconds=getattr(connector, "ddl_effect_lag_seconds", 0))
    since, until = window.previous_start - slack - lag, window.current_done + slack
    certain_from, certain_to = window.previous_done + slack, window.current_start - slack
    basis_window = {"since": since.isoformat(), "until": until.isoformat(), "certain_from": certain_from.isoformat(),
                    "certain_to": certain_to.isoformat(), "slack_seconds": int(slack.total_seconds()),
                    "effect_lag_seconds": int(lag.total_seconds()), "source": source,
                    "by_transaction": bool(window.previous_cursor and window.current_cursor)}
    try:
        audit_events = connector.ddl_events(since, until, cursors=(window.previous_cursor, window.current_cursor))
    except Exception as exc:  # noqa: BLE001 - 读取审计失败只影响归因，不让采集失败
        cause = exc.cause if isinstance(exc, AuditUnavailable) else "error"
        message = str(exc) if isinstance(exc, AuditUnavailable) else f"{type(exc).__name__}: {exc}"
        for e in events:
            if e["kind"] in DERIVED:
                _set(e, "not_applicable", reason="推导出的变化，不是对该对象执行的 DDL")
            else:
                _set(e, "unavailable", source=source, window=basis_window, cause=cause,
                     reason=f"{CAUSE_LABEL[cause]}，无法归因（数据库报错：{message}）")
        return {"status": "unavailable", "source": source, "cause": cause, "error": message[:500],
                "window": basis_window}

    def placement(a: DdlEvent) -> str | None:
        """certain 确定在窗口内；boundary 边界候选；None 确定在上次快照之前（不参与）。"""
        if a.certain is not None:
            return "certain" if a.certain else "boundary"
        if a.at < window.previous_start - slack:
            return "boundary" if a.may_lag else None
        return "certain" if certain_from < a.at < certain_to else "boundary"

    audit_events = [a for a in audit_events if placement(a) is not None]

    def certain(a: DdlEvent) -> bool:
        return placement(a) == "certain"

    fold = getattr(connector, "ddl_identifier_fold", "exact")
    by_object: dict[tuple[str, str], list[DdlEvent]] = {}
    for a in audit_events:
        for db, name in a.objects:
            by_object.setdefault((db or "", name or ""), []).append(a)
    for e in events:
        if e["kind"] in DERIVED:
            _set(e, "not_applicable", reason="推导出的变化，不是对该对象执行的 DDL")
            continue
        try:
            # 只声明某个 Catalog 的来源不能给其他 Catalog 的同名对象归因。
            audit_catalog = getattr(connector, "ddl_audit_catalog", None)
            asset = session.get(Asset, e["asset_id"]) if audit_catalog and e.get("asset_id") else None
            if asset is not None and asset.catalog_name != audit_catalog:
                _set(e, "unsupported", source=source,
                     reason=gettext("DDL 审计来源不覆盖此 Catalog，无法归因"))
                continue
            _attribute_one(session, e, by_object, source, basis_window, fold, certain)
        except Exception as exc:  # noqa: BLE001 - 单个事件归因出错：如实记为未知，不影响其他事件与采集
            _set(e, "unavailable", source=source, window=basis_window, cause="error",
                 reason=f"归因时出错，无法归因（{type(exc).__name__}: {exc}）"[:500])
    return {"status": "ok", "source": source, "window": basis_window, "audit_events": len(audit_events)}


def _attribute_one(session: Session, e: dict[str, Any], by_object: dict[tuple[str, str], list[DdlEvent]],
                   source: str | None, basis_window: dict[str, Any], fold: str, certain) -> None:
    """一个直接变更事件的归因。"""
    asset = session.get(Asset, e["asset_id"]) if e.get("asset_id") else None
    names = _names_of(e, asset)
    candidates = sorted({id(a): a for n in names for a in by_object.get(n, [])}.values(), key=lambda a: a.at)
    basis: dict[str, Any] = {"source": source, "window": basis_window,
                             "objects": sorted(".".join(n) for n in names)}
    if not candidates:
        _set(e, "unmatched", **basis,
             reason=f"审计窗口内没有 {'、'.join(basis['objects'])} 的 DDL 记录"
                    "（可能是审计日志尚未落盘、改动发生在审计开启之前，或经由未审计的路径）")
        return
    column = e.get("column_name")
    unknown: list[DdlEvent] = []
    if column:
        # 字段级变化的候选（取并集）：在列的语法位置上点名了该列的语句、整体重定义（不一定逐列点名）、
        # 针对该对象但识别不出涉及哪些列的语句（可能涉及任何一列，只能得到 ambiguous）。
        # 确定只改了别的列（或不改列结构，如改属主、加 CHECK 约束）的语句不算
        # 同一条记录里“点名了该列”和“有认不出的部分”各自累积：认得出的列不能掩盖认不出的部分
        by_column, whole = [], []
        for a in candidates:
            if redefines(a.statement):
                whole.append(a)
                continue
            refs = [column_refs(a.statement, obj, fold, schema=db) for db, obj in names]
            if any(same_identifier(t, column, fold) for r in refs for t in r.columns):
                by_column.append(a)
            if any(r.unknown for r in refs):
                unknown.append(a)
        basis.update(column=column, column_match=bool(by_column), redefinitions=len(whole),
                     unrecognized=len(unknown))
        candidates = sorted({id(a): a for a in [*by_column, *whole, *unknown]}.values(), key=lambda a: a.at)
        if not candidates:
            _set(e, "unmatched", **basis,
                 reason=f"窗口内有 {'、'.join(basis['objects'])} 的 DDL，但没有一条点名列 {column} 或整体重定义"
                        "（该列的改动可能经由未审计的路径）")
            return
    users = sorted({a.db_user for a in candidates})
    boundary = [a for a in candidates if not certain(a)]
    statements = [_candidate(a, certain(a)) for a in candidates[-CANDIDATE_LIMIT:]]
    if len(users) > 1:
        _set(e, "ambiguous", **basis, candidates=users, statements=statements,
             reason=f"窗口内该对象的 DDL 来自多个用户（{'、'.join(users)}），无法确定是谁")
    elif unknown:
        _set(e, "ambiguous", **basis, candidates=users, statements=statements,
             reason=f"候选语句都来自 {users[0]}，但有 {len(unknown)} 条识别不出涉及哪些列，"
                    f"不能确定就是它改了列 {column}")
    elif boundary:
        # 只要有边界候选就不确定：边界语句可能属于上个窗口，本窗口的变化也可能来自未审计的路径
        _set(e, "ambiguous", **basis, candidates=users, statements=statements,
             reason=f"候选语句都来自 {users[0]}，但有 {len(boundary)} 条在快照读取期间、窗口边界附近，"
                    "或无法按事务确认属于本窗口，不能确定归因")
    else:
        _set(e, "matched", actor=users[0], **basis, statements=statements,
             reason=f"窗口内该对象的 {len(candidates)} 条 DDL 都来自 {users[0]}，且都确定在本窗口内")


def _set(event: dict[str, Any], status: str, actor: str | None = None, **detail: Any) -> None:
    event["attribution_status"] = status
    event["actor_db_user"] = actor
    event["attribution"] = detail
