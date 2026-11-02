"""采集：快照 → 身份判定 → 版本比较 → 解析血缘 → 外部表身份映射 → 粒度 → 字段推导指纹 → 变更集与影响分析。

身份判定（不按名称相似自动认定）：
- 物理 ID（StarRocks TABLE_ID、PostgreSQL oid 等，由连接器给出）相同即同一张表（改名可确认）；
  同名但物理 ID 不同 = 删除后重建，是新资产；
- 连接器没有给出物理 ID 的对象（StarRocks 视图、外部表、Catalog 表）：按“Catalog + 库 + 名称 + 类型”匹配；
  名称变化只记为“改名候选”。
重复采集幂等：内容哈希不变只更新 last_seen，不产生新版本、不产生变更。
变更事件在写入前按数据库的 DDL 审计记录归因到数据库用户（avelis.collect.attribution，ADR 0008）。
"""

from __future__ import annotations

import logging
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from avelis import audit, events
from avelis.collect.attribution import Window, attribute
from avelis.connectors.base import ObservedAsset, sha256_json
from avelis.db import session_scope, utcnow
from avelis.engines import collect_connector, dialect_of, engine_of
from avelis.engines.base import VIEW_LINEAGE
from avelis.lineage.fingerprint import GraphState, compute_fingerprints
from avelis.lineage.sql import PARSER_VERSION, SqlDialect, canonical_hash_of, parse_query
from avelis.models import (
    Asset,
    AssetColumn,
    AssetVersion,
    ChangeEvent,
    ChangeSet,
    CollectionRun,
    ColumnFingerprint,
    ColumnVersion,
    Datasource,
    Definition,
    EndpointAlias,
    Grain,
    IdentityLink,
    Job,
    LineageEdge,
    ParseResult,
    User,
)

log = logging.getLogger(__name__)


@dataclass
class RunContext:
    run: CollectionRun
    sql: SqlDialect
    events: list[dict[str, Any]] = field(default_factory=list)
    new_versions: list[AssetVersion] = field(default_factory=list)
    stats: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    touched: set[str] = field(default_factory=set)  # 本次出现新身份的对象（新增或同名重建；catalog.db.name）
    connector: Any = None  # 采集连接器：归因时读 DDL 审计来源
    snapshot_window: tuple[datetime, datetime] | None = None  # 本次读快照的开始、结束（归因窗口的边界）
    audit_cursor: dict[str, Any] | None = None  # 连接器在快照里记下的审计游标（如 PostgreSQL 事务快照）
    identity_changed: bool = False  # 本次与上次不是同一个实例：游标不可比，本轮不归因

    def event(self, kind: str, asset_id: str | None, column: str | None = None, before=None, after=None) -> None:
        self.events.append(
            {"kind": kind, "asset_id": asset_id, "column_name": column, "before": before or {}, "after": after or {}}
        )
        self.stats[f"event:{kind}"] += 1


class CollectionFailed(Exception):
    pass


class CollectionAbandoned(CollectionFailed):
    """超时回收已留下失败事件，本次迟到的结果必须回滚。"""


def run_collection(
    datasource_id: str,
    *,
    trigger: str,
    actor_id: str | None = None,
    job_id: str | None = None,
    attempt: int | None = None,
) -> str:
    abandoned = None
    with events.failure_guard("collect.failed", actor_id, "datasource", datasource_id, trigger=trigger):
        try:
            return _run_collection(datasource_id, trigger=trigger, actor_id=actor_id, job_id=job_id, attempt=attempt)
        except CollectionAbandoned as exc:
            abandoned = exc  # 回收事务已经写了失败事件，不为同一次执行再写一条
    raise abandoned


def _run_collection(
    datasource_id: str,
    *,
    trigger: str,
    actor_id: str | None = None,
    job_id: str | None = None,
    attempt: int | None = None,
) -> str:
    """执行一次采集并返回 run_id。失败时 run 记为 failed 并保留原始错误。"""
    with session_scope() as session:
        execution = None
        if job_id:
            execution = session.scalar(select(Job).where(Job.id == job_id).with_for_update())
            if execution is None or execution.status != "running" or execution.attempts != attempt:
                raise CollectionAbandoned("采集任务已被回收或执行权已变更，本次结果未写入")
        ds = session.get(Datasource, datasource_id)
        if ds is None:
            raise CollectionFailed("数据源不存在")
        previous = session.scalar(
            select(CollectionRun)
            .where(CollectionRun.datasource_id == ds.id, CollectionRun.status == "succeeded")
            .order_by(CollectionRun.started_at.desc())
            .limit(1)
        )
        run = CollectionRun(
            datasource_id=ds.id,
            trigger=trigger,
            status="running",
            previous_run_id=previous.id if previous else None,
            triggered_by=actor_id,
        )
        session.add(run)
        session.flush()
        run_id = run.id
        if execution is not None:
            execution.result = {**(execution.result or {}), "run_id": run_id}
    try:
        with session_scope() as session:
            ds = session.get(Datasource, datasource_id)
            run = session.get(CollectionRun, run_id)
            actor = session.get(User, actor_id) if actor_id else None
            connector = collect_connector(session, ds)
            snapshot_started = utcnow()
            snapshot = connector.snapshot()
            # 游标与实例身份绑定：切换实例后旧游标不能用来解释新实例的事务号。连接器给出了实例键时以它为准
            cursor = (
                {"instance": _identity_key(snapshot.server_identity or {}), **snapshot.audit_cursor}
                if snapshot.audit_cursor
                else None
            )
            ctx = RunContext(
                run,
                dialect_of(ds),
                connector=connector,
                snapshot_window=(snapshot_started, utcnow()),
                audit_cursor=cursor,
            )
            # 与超时回收统一为 Job → CollectionRun；快照写入中的 flush 也会锁住 run。
            with session.no_autoflush:
                if job_id:
                    execution = session.scalar(select(Job).where(Job.id == job_id).with_for_update())
                    if execution is None or execution.status != "running" or execution.attempts != attempt:
                        raise CollectionAbandoned("采集任务已被回收或执行权已变更，本次结果未写入")
                status = session.scalar(
                    select(CollectionRun.status).where(CollectionRun.id == run_id).with_for_update()
                )
                if status != "running":
                    raise CollectionAbandoned("采集运行已被回收，本次结果未写入")
            _apply_snapshot(session, ds, snapshot, ctx)
            run.status = "succeeded"
            run.finished_at = utcnow()
            run.stats = {
                **dict(ctx.stats),
                "warnings": snapshot.warnings,
                "snapshot_window": [t.isoformat() for t in ctx.snapshot_window],
                **({"audit_cursor": ctx.audit_cursor} if ctx.audit_cursor else {}),
            }
            audit.record(
                session, actor, "collect", "datasource", ds.id, run_id=run.id, trigger=trigger, stats=run.stats
            )
    except CollectionAbandoned:
        raise
    except Exception as exc:  # 记录失败原因（不含口令：连接器异常只含库返回的错误）
        log.exception("采集失败 %s", datasource_id)
        try:
            with session_scope() as session:
                if job_id:
                    execution = session.scalar(select(Job).where(Job.id == job_id).with_for_update())
                    if execution is None or execution.status != "running" or execution.attempts != attempt:
                        raise CollectionAbandoned("采集任务已被回收或执行权已变更，本次结果未写入") from exc
                run = session.get(CollectionRun, run_id)
                run.status = "failed"
                run.finished_at = utcnow()
                run.error = f"{type(exc).__name__}: {exc}"[:4000]
        except CollectionAbandoned:
            raise
        except Exception:  # 留底不可用不能替换原采集错误；外层 guard 仍会尝试独立审计
            log.error("采集失败状态未能写入 %s", datasource_id)
        raise CollectionFailed(str(exc)) from exc
    return run_id


def _apply_snapshot(session: Session, ds: Datasource, snapshot, ctx: RunContext) -> None:
    run = ctx.run
    now = utcnow()
    old_identity = dict(ds.server_identity or {})
    new_identity = snapshot.server_identity or {}
    # 实例身份确定地变了（不是同一实例换了地址）：开启新的身份世代，旧资产全部删除、按新对象重建，
    # 下游指纹随之变化，依赖它们的口径进入复核，而不是按名称继续沿用旧身份
    new_generation = bool(
        old_identity.get("instance_id")
        and new_identity.get("instance_id")
        and old_identity["instance_id"] != new_identity["instance_id"]
    )
    if old_identity and _identity_key(old_identity) != _identity_key(new_identity):
        ctx.identity_changed = True
        ctx.event(
            "server_identity_changed",
            None,
            before=old_identity,
            after={**new_identity, "new_generation": new_generation},
        )
    ds.server_identity = new_identity

    active = list(session.scalars(select(Asset).where(Asset.datasource_id == ds.id, Asset.status == "active")))
    by_physical = {a.physical_id: a for a in active if a.physical_id}
    by_name = {(a.catalog_name, a.db_name, a.name, a.kind): a for a in active}
    if new_generation:
        by_physical, by_name = {}, {}
    matched: set[str] = set()
    added: list[tuple[Asset, ObservedAsset]] = []

    for obs in snapshot.assets:
        ctx.stats["assets_observed"] += 1
        asset = by_physical.get(obs.physical_id) if obs.physical_id else None
        if asset is None:
            candidate = by_name.get((obs.catalog, obs.db, obs.name, obs.kind))
            if candidate is not None and not (
                candidate.physical_id and obs.physical_id and candidate.physical_id != obs.physical_id
            ):
                asset = candidate
        if asset is not None and asset.id in matched:
            asset = None  # 同一身份在一次快照中出现两次：不合并，作为新资产
        if asset is None:
            asset = Asset(
                datasource_id=ds.id,
                kind=obs.kind,
                catalog_name=obs.catalog,
                db_name=obs.db,
                name=obs.name,
                physical_id=obs.physical_id,
                status="active",
                first_seen_at=now,
                last_seen_at=now,
            )
            session.add(asset)
            session.flush()
            added.append((asset, obs))
            ctx.touched.add(obs.qualified)
            recreated = by_name.get((obs.catalog, obs.db, obs.name, obs.kind))
            ctx.event(
                "added",
                asset.id,
                after={
                    "name": obs.qualified,
                    "kind": obs.kind,
                    "physical_id": obs.physical_id,
                    "recreated_from": recreated.id if recreated else None,
                },
            )
        else:
            if (asset.catalog_name, asset.db_name, asset.name) != (obs.catalog, obs.db, obs.name):
                ctx.event(
                    "renamed",
                    asset.id,
                    before={"name": f"{asset.catalog_name}.{asset.db_name}.{asset.name}"},
                    after={"name": obs.qualified, "basis": f"物理 ID {obs.physical_id} 未变"},
                )
                asset.catalog_name, asset.db_name, asset.name = obs.catalog, obs.db, obs.name
            asset.last_seen_at = now
        matched.add(asset.id)
        _record_version(session, asset, obs, ctx)

    deleted = [a for a in active if a.id not in matched]
    for asset in deleted:
        asset.status = "deleted"
        asset.deleted_at = now
        session.execute(
            update(AssetColumn)
            .where(AssetColumn.asset_id == asset.id, AssetColumn.status == "active")
            .values(status="deleted", deleted_at=now)
        )
        if asset.current_version_id:
            session.execute(
                update(AssetVersion).where(AssetVersion.id == asset.current_version_id).values(superseded_at=now)
            )
        ctx.event(
            "deleted",
            asset.id,
            before={
                "name": f"{asset.catalog_name}.{asset.db_name}.{asset.name}",
                "kind": asset.kind,
                "physical_id": asset.physical_id,
            },
        )
    _view_rename_candidates(session, deleted, added, ctx)
    session.flush()

    _parse_definitions(session, ds, ctx)
    link_changes = relink_all(session)
    for change in link_changes:
        ctx.event("identity_link_changed", change["asset_id"], before=change["before"], after=change["after"])
    session.flush()
    fp_changes = refresh_fingerprints(session, run.id)
    for change in fp_changes:
        kind = "upstream_broken" if change["after"]["status"] == "broken" else "derivation_changed"
        ctx.event(kind, change["asset_id"], change["column"], change["before"], change["after"])

    run.snapshot_hash = sha256_json(sorted(_content_hash(o, ctx.sql) for o in snapshot.assets))
    if ctx.events:
        from avelis.governance.changes import analyze_change_set  # 避免循环导入

        previous = session.get(CollectionRun, run.previous_run_id) if run.previous_run_id else None
        audit_window = attribute(session, engine_of(ds), ctx.connector, ctx.events, _audit_window(previous, ctx))
        if audit_window.get("status") == "unavailable" and audit_window.get("cause") in (
            "no_permission",
            "not_enabled",
        ):
            audit.record(
                session,
                None,
                "attribution.unavailable",
                "datasource",
                ds.id,
                run_id=run.id,
                cause=audit_window["cause"],
                source=audit_window.get("source"),
            )
        change_set = ChangeSet(
            run_id=run.id,
            datasource_id=ds.id,
            status="open",
            window_start=previous.finished_at if previous else None,
            window_end=now,
            summary={**{k: v for k, v in ctx.stats.items() if k.startswith("event:")}, "ddl_audit": audit_window},
        )
        session.add(change_set)
        session.flush()
        for e in ctx.events:
            session.add(ChangeEvent(change_set_id=change_set.id, **e))
        session.flush()
        analyze_change_set(session, change_set, fp_changes)
        audit.record(
            session,
            session.get(User, run.triggered_by) if run.triggered_by else None,
            "collect.schema_changed",
            "datasource",
            ds.id,
            run_id=run.id,
            change_set_id=change_set.id,
            changes={k.removeprefix("event:"): v for k, v in ctx.stats.items() if k.startswith("event:")},
        )


def _audit_window(previous: CollectionRun | None, ctx: RunContext) -> Window | None:
    """归因窗口：上次与本次读快照的时间。旧的采集记录没有快照时间时，用开始与结束时间（更宽，边界候选更多）。"""
    if previous is None:
        return None
    stats = previous.stats or {}
    try:
        p_start, p_done = (datetime.fromisoformat(t) for t in stats["snapshot_window"])
    except (KeyError, TypeError, ValueError):
        p_start = previous.started_at.replace(tzinfo=None)
        p_done = (previous.finished_at or previous.started_at).replace(tzinfo=None)
    c_start, c_done = ctx.snapshot_window or (utcnow(), utcnow())
    p_cursor, c_cursor = stats.get("audit_cursor"), ctx.audit_cursor
    # 两次游标的实例不同，或任一方无法确认实例：按实例已切换处理
    changed = ctx.identity_changed or bool(
        p_cursor
        and c_cursor
        and (p_cursor.get("instance") is None or p_cursor.get("instance") != c_cursor.get("instance"))
    )
    return Window(
        p_start, p_done, c_start, c_done, previous_cursor=p_cursor, current_cursor=c_cursor, instance_changed=changed
    )


def _identity_key(identity: dict[str, Any]) -> Any:
    return identity.get("instance_id") or identity.get("endpoint")


def _content_hash(obs: ObservedAsset, sql: SqlDialect) -> str:
    _, digest = canonical_hash_of(obs.definition, sql) if obs.definition else (None, None)
    return obs.content_hash(digest)


def _record_version(session: Session, asset: Asset, obs: ObservedAsset, ctx: RunContext) -> None:
    run, now = ctx.run, utcnow()
    canonical, def_hash = canonical_hash_of(obs.definition, ctx.sql) if obs.definition else (None, None)
    content_hash = obs.content_hash(def_hash)
    current = session.get(AssetVersion, asset.current_version_id) if asset.current_version_id else None
    if current is not None and current.content_hash == content_hash:
        current.last_seen_run_id = run.id
        current.last_seen_at = now
        ctx.stats["unchanged"] += 1
        return

    version_no = (
        session.scalar(select(func.max(AssetVersion.version_no)).where(AssetVersion.asset_id == asset.id)) or 0
    ) + 1
    version = AssetVersion(
        asset_id=asset.id,
        version_no=version_no,
        observed_run_id=run.id,
        last_seen_run_id=run.id,
        observed_at=now,
        last_seen_at=now,
        kind=obs.kind,
        catalog_name=obs.catalog,
        db_name=obs.db,
        name=obs.name,
        table_model=obs.table_model,
        key_columns=obs.key_columns,
        unique_keys=obs.unique_keys,
        comment=obs.comment,
        properties=obs.properties,
        content_hash=content_hash,
    )
    session.add(version)
    session.flush()

    columns = {
        c.name: c
        for c in session.scalars(
            select(AssetColumn).where(AssetColumn.asset_id == asset.id, AssetColumn.status == "active")
        )
    }
    seen = set()
    for oc in obs.columns:
        col = columns.get(oc.name)
        if col is None:
            col = AssetColumn(asset_id=asset.id, name=oc.name, status="active", first_seen_at=now, last_seen_at=now)
            session.add(col)
            session.flush()
        col.last_seen_at = now
        seen.add(oc.name)
        session.add(
            ColumnVersion(
                asset_version_id=version.id,
                column_id=col.id,
                ordinal=oc.ordinal,
                name=oc.name,
                data_type=oc.data_type,
                nullable=oc.nullable,
                comment=oc.comment,
                is_key=oc.is_key,
            )
        )
    for name, col in columns.items():
        if name not in seen:
            col.status = "deleted"
            col.deleted_at = now
    if obs.definition is not None:
        session.add(
            Definition(
                asset_version_id=version.id,
                dialect=ctx.sql.name,
                raw_sql=obs.definition,
                canonical_sql=canonical,
                canonical_hash=def_hash,
            )
        )

    if current is not None:
        current.superseded_at = now
        _diff_versions(session, asset, current, version, obs, def_hash, ctx)
    asset.current_version_id = version.id
    ctx.new_versions.append(version)
    ctx.stats["new_versions"] += 1

    session.execute(
        update(Grain)
        .where(Grain.asset_id == asset.id, Grain.origin.in_(("observed", "parsed")), Grain.superseded_at.is_(None))
        .values(superseded_at=now)
    )
    for key in _distinct_keys(obs):
        session.add(
            Grain(
                asset_id=asset.id,
                asset_version_id=version.id,
                columns=key,
                origin="observed",
                status="verified",
                verified_at=now,
                verify_detail={"basis": "数据库主键 / 唯一约束", "table_model": obs.table_model},
            )
        )


def _distinct_keys(obs: ObservedAsset) -> list[list[str]]:
    keys, seen = [], set()
    for key in ([obs.key_columns] if obs.key_columns else []) + list(obs.unique_keys):
        if key and tuple(key) not in seen:
            seen.add(tuple(key))
            keys.append(list(key))
    return keys


def _diff_versions(
    session: Session,
    asset: Asset,
    old: AssetVersion,
    new: AssetVersion,
    obs: ObservedAsset,
    new_def_hash: str | None,
    ctx: RunContext,
) -> None:
    old_cols = {
        c.name: c for c in session.scalars(select(ColumnVersion).where(ColumnVersion.asset_version_id == old.id))
    }
    new_cols = {c.name: c for c in obs.columns}
    removed = [n for n in old_cols if n not in new_cols]
    added = [n for n in new_cols if n not in old_cols]
    for name in removed:
        ctx.event("column_removed", asset.id, name, before={"data_type": old_cols[name].data_type})
    for name in added:
        ctx.event("column_added", asset.id, name, after={"data_type": new_cols[name].data_type})
    for name in sorted(set(old_cols) & set(new_cols)):
        if old_cols[name].data_type != new_cols[name].data_type or old_cols[name].nullable != new_cols[name].nullable:
            ctx.event(
                "column_type_changed",
                asset.id,
                name,
                before={"data_type": old_cols[name].data_type, "nullable": old_cols[name].nullable},
                after={"data_type": new_cols[name].data_type, "nullable": new_cols[name].nullable},
            )
        if (old_cols[name].comment or None) != (new_cols[name].comment or None):
            ctx.event(
                "column_comment_changed",
                asset.id,
                name,
                before={"comment": old_cols[name].comment},
                after={"comment": new_cols[name].comment},
            )
    for r in removed:
        for a in added:
            if old_cols[r].data_type == new_cols[a].data_type and old_cols[r].ordinal == new_cols[a].ordinal:
                ctx.event(
                    "column_rename_candidate",
                    asset.id,
                    a,
                    before={"name": r},
                    after={"name": a, "basis": "同位置、同类型；需人工确认"},
                )
    old_def = session.scalar(select(Definition).where(Definition.asset_version_id == old.id))
    old_hash = old_def.canonical_hash if old_def else None
    if old_hash != new_def_hash:
        ctx.event(
            "definition_changed",
            asset.id,
            before={"canonical_sql": old_def.canonical_sql if old_def else None, "hash": old_hash},
            after={
                "canonical_sql": canonical_hash_of(obs.definition, ctx.sql)[0] if obs.definition else None,
                "hash": new_def_hash,
            },
        )
    if (old.table_model, old.key_columns, sorted(old.unique_keys)) != (
        new.table_model,
        new.key_columns,
        sorted(new.unique_keys),
    ):
        ctx.event(
            "grain_changed",
            asset.id,
            before={"table_model": old.table_model, "keys": old.key_columns, "unique": old.unique_keys},
            after={"table_model": new.table_model, "keys": new.key_columns, "unique": new.unique_keys},
        )
    if (old.properties or {}).get("external") != (new.properties or {}).get("external"):
        ctx.event(
            "external_ref_changed",
            asset.id,
            before=(old.properties or {}).get("external") or {},
            after=(new.properties or {}).get("external") or {},
        )
    if old.comment != new.comment:
        ctx.event("comment_changed", asset.id, before={"comment": old.comment}, after={"comment": new.comment})


def _view_rename_candidates(
    session: Session, deleted: list[Asset], added: list[tuple[Asset, ObservedAsset]], ctx: RunContext
) -> None:
    gone = {}
    for asset in deleted:
        if asset.kind != "view" or not asset.current_version_id:
            continue
        d = session.scalar(select(Definition).where(Definition.asset_version_id == asset.current_version_id))
        if d:
            gone[d.canonical_hash] = asset
    for asset, obs in added:
        if obs.kind == "view" and obs.definition:
            _, digest = canonical_hash_of(obs.definition, ctx.sql)
            if digest in gone:
                old = gone[digest]
                ctx.event(
                    "rename_candidate",
                    asset.id,
                    before={"asset_id": old.id, "name": f"{old.catalog_name}.{old.db_name}.{old.name}"},
                    after={"name": obs.qualified, "basis": "定义指纹相同；视图没有物理 ID，需人工确认"},
                )


# --- 解析 -------------------------------------------------------------------------


def build_schema(session: Session, datasource_id: str) -> dict:
    schema: dict = {}
    rows = session.execute(
        select(Asset.catalog_name, Asset.db_name, Asset.name, ColumnVersion.name, ColumnVersion.data_type)
        .join(ColumnVersion, ColumnVersion.asset_version_id == Asset.current_version_id)
        .where(Asset.datasource_id == datasource_id, Asset.status == "active")
    )
    for catalog, db, table, column, data_type in rows:
        schema.setdefault(catalog, {}).setdefault(db, {}).setdefault(table, {})[column] = _sqlglot_type(data_type)
    return schema


def _sqlglot_type(data_type: str) -> str:
    """解析用 schema 的列类型：逐层去掉长度 / 精度参数，保留嵌套结构与后缀（如 struct<a:decimal(10,2),b:int> →
    STRUCT<A:DECIMAL,B:INT>，timestamp(6) with time zone → TIMESTAMP WITH TIME ZONE）。"""
    base = data_type
    while (stripped := re.sub(r"\([^()]*\)", "", base)) != base:
        base = stripped
    return base.strip().upper() or "VARCHAR"


def _parse_definitions(session: Session, ds: Datasource, ctx: RunContext) -> None:
    """解析视图定义：当前版本尚未被本解析器版本解析过的；以及引用的来源在本次采集中出现了新身份的（新增或同名重建）——
    定义没变也要重解析，否则血缘会继续指向已删除的旧对象，来源补齐后也不会恢复。来源只是删除或改列时不重解析：
    旧的边指向已删除的对象或列，指纹如实给出“上游缺失”。"""
    rows = session.execute(
        select(Asset, Definition)
        .join(Definition, Definition.asset_version_id == Asset.current_version_id)
        .where(Asset.datasource_id == ds.id, Asset.status == "active", Asset.kind == "view")
    )
    pending = []
    for asset, definition in rows:
        existing = session.scalar(
            select(ParseResult).where(
                ParseResult.definition_id == definition.id, ParseResult.parser_version == PARSER_VERSION
            )
        )
        if existing is None or set((existing.details or {}).get("tables", [])) & ctx.touched:
            pending.append((asset, definition))
    if not pending:
        return
    engine = engine_of(ds)
    if not engine.supports(VIEW_LINEAGE):
        for asset, definition in pending:
            session.add(
                ParseResult(
                    definition_id=definition.id,
                    parser_version=PARSER_VERSION,
                    status="unsupported",
                    reason=f"{engine.label} 适配器尚未支持视图血缘解析",
                )
            )
        return
    schema = build_schema(session, ds.id)
    index = {
        (a.catalog_name, a.db_name, a.name): a.id
        for a in session.scalars(select(Asset).where(Asset.datasource_id == ds.id, Asset.status == "active"))
    }
    now = utcnow()
    for asset, definition in pending:
        parsed = parse_query(definition.raw_sql, dialect=dialect_of(ds), schema=schema, default_db=asset.db_name)
        # 同一定义、同一解析器版本只保留一条解析结果：重解析时原地更新
        result = session.scalar(
            select(ParseResult).where(
                ParseResult.definition_id == definition.id, ParseResult.parser_version == PARSER_VERSION
            )
        )
        if result is None:
            result = ParseResult(definition_id=definition.id, parser_version=PARSER_VERSION)
            session.add(result)
        result.status, result.reason, result.details = parsed.status, parsed.reason, parsed.details()
        session.flush()
        ctx.stats[f"parse:{parsed.status}"] += 1
        session.execute(
            update(LineageEdge)
            .where(
                LineageEdge.target_asset_id == asset.id, LineageEdge.origin == "parsed", LineageEdge.status == "active"
            )
            .values(status="superseded", superseded_at=now)
        )

        def source_id(table: str) -> str | None:
            parts = tuple(table.split("."))
            return index.get(parts) if len(parts) == 3 else None

        for column in parsed.columns:
            for src in column.sources:
                session.add(
                    LineageEdge(
                        origin="parsed",
                        parse_result_id=result.id,
                        target_asset_id=asset.id,
                        target_asset_version_id=asset.current_version_id,
                        target_column=column.name,
                        source_asset_id=source_id(src.ref.table),
                        source_ref=src.ref.table,
                        source_column=src.ref.column,
                        relation=src.relation,
                        expression=column.expression,
                    )
                )
        for inf in parsed.influences:
            session.add(
                LineageEdge(
                    origin="parsed",
                    parse_result_id=result.id,
                    target_asset_id=asset.id,
                    target_asset_version_id=asset.current_version_id,
                    target_column=None,
                    source_asset_id=source_id(inf.ref.table),
                    source_ref=inf.ref.table,
                    source_column=inf.ref.column,
                    relation=inf.relation,
                    expression=inf.expression,
                )
            )
        # 表级依赖：即使没有列来源（COUNT(1)、常量列）或列级解析不支持（UNION），读取视图也就读取了这些表。
        # 权限闭包与字段指纹都依赖这些边，不能只靠列级边。
        for table in parsed.tables:
            session.add(
                LineageEdge(
                    origin="parsed",
                    parse_result_id=result.id,
                    target_asset_id=asset.id,
                    target_asset_version_id=asset.current_version_id,
                    target_column=None,
                    source_asset_id=source_id(table),
                    source_ref=table,
                    source_column=None,
                    relation="table",
                    expression=None,
                )
            )
        if parsed.group_outputs or parsed.distinct:
            columns = parsed.group_outputs if parsed.group_outputs else [c.name for c in parsed.columns]
            session.add(
                Grain(
                    asset_id=asset.id,
                    asset_version_id=asset.current_version_id,
                    columns=columns,
                    origin="parsed",
                    status="verified" if parsed.status == "ok" else "unverified",
                    verify_detail={"basis": "GROUP BY 键" if parsed.group_outputs else "SELECT DISTINCT"},
                )
            )


# --- 外部表身份映射 ----------------------------------------------------------------


def _norm_type(data_type: str) -> tuple[str, str]:
    t = data_type.lower().replace(" ", "")
    t = re.sub(r"^(tinyint|smallint|mediumint|int|integer|bigint)\(\d+\)", r"\1", t)
    t = t.replace("integer", "int")
    base = re.sub(r"\(.*", "", t)
    return base, t


def resolve_link(session: Session, asset: Asset) -> dict[str, Any]:
    version = session.get(AssetVersion, asset.current_version_id) if asset.current_version_id else None
    ext = (version.properties or {}).get("external") if version else None
    if not ext or not ext.get("host"):
        return {
            "status": "unresolved",
            "target": None,
            "method": "endpoint_alias",
            "evidence": {"reason": "未读到外部表的连接地址"},
            "column_match": {},
        }
    alias = session.scalar(
        select(EndpointAlias).where(
            EndpointAlias.host == ext["host"], EndpointAlias.port == int(ext.get("port") or 3306)
        )
    )
    evidence: dict[str, Any] = {
        "endpoint": f"{ext['host']}:{ext.get('port')}",
        "database": ext.get("database"),
        "table": ext.get("table"),
        "via": ext.get("via"),
    }
    if alias is None:
        evidence["reason"] = "该地址没有登记为任何数据源的端点别名"
        return {
            "status": "unresolved",
            "target": None,
            "method": "endpoint_alias",
            "evidence": evidence,
            "column_match": {},
        }
    target_ds = session.get(Datasource, alias.datasource_id)
    evidence["datasource"] = target_ds.name
    evidence["server_uuid"] = (target_ds.server_identity or {}).get("instance_id")  # 目标实例身份（由连接器给出）
    target = session.scalar(
        select(Asset).where(
            Asset.datasource_id == target_ds.id,
            Asset.db_name == ext.get("database"),
            Asset.name == ext.get("table"),
            Asset.status == "active",
        )
    )
    if target is None:
        evidence["reason"] = f"数据源 {target_ds.name} 中没有采集到 {ext.get('database')}.{ext.get('table')}"
        return {
            "status": "unresolved",
            "target": None,
            "method": "endpoint_alias",
            "evidence": evidence,
            "column_match": {},
        }
    src_cols = {
        c.name: c
        for c in session.scalars(
            select(ColumnVersion).where(ColumnVersion.asset_version_id == asset.current_version_id)
        )
    }
    dst_cols = {
        c.name: c
        for c in session.scalars(
            select(ColumnVersion).where(ColumnVersion.asset_version_id == target.current_version_id)
        )
    }
    missing = [n for n in src_cols if n not in dst_cols]
    type_mismatch, type_notes = [], []
    for name in set(src_cols) & set(dst_cols):
        a, b = _norm_type(src_cols[name].data_type), _norm_type(dst_cols[name].data_type)
        if a[0] != b[0]:
            type_mismatch.append(
                {"column": name, "external": src_cols[name].data_type, "source": dst_cols[name].data_type}
            )
        elif a[1] != b[1]:
            type_notes.append(
                {"column": name, "external": src_cols[name].data_type, "source": dst_cols[name].data_type}
            )
    column_match = {
        "matched": sorted(set(src_cols) & set(dst_cols)),
        "missing_in_source": missing,
        "type_mismatch": type_mismatch,
        "type_notes": type_notes,
        "source_only": sorted(n for n in dst_cols if n not in src_cols),
    }
    status = "confirmed" if not missing and not type_mismatch else "mismatch"
    if status == "confirmed" and not evidence["server_uuid"]:
        status = "candidate"
        evidence["reason"] = "目标数据源尚未确认实例身份"
    return {
        "status": status,
        "target": target.id,
        "method": "endpoint_alias+db_table+columns",
        "evidence": evidence,
        "column_match": column_match,
    }


def relink_all(session: Session) -> list[dict[str, Any]]:
    """重算全部外部表 / Catalog 表的身份映射；只有结论变化时才写入新记录（旧记录保留为历史）。"""
    changes = []
    now = utcnow()
    for asset in session.scalars(select(Asset).where(Asset.kind.in_(("external_table", "catalog_table")))):
        current = session.scalar(
            select(IdentityLink).where(IdentityLink.source_asset_id == asset.id, IdentityLink.superseded_at.is_(None))
        )
        if asset.status != "active":
            if current is not None:
                current.superseded_at = now
            continue
        desired = resolve_link(session, asset)
        signature = (
            desired["status"],
            desired["target"],
            sha256_json(desired["column_match"]),
            desired["evidence"].get("server_uuid"),
        )
        if (
            current is not None
            and (
                current.status,
                current.target_asset_id,
                sha256_json(current.column_match),
                (current.evidence or {}).get("server_uuid"),
            )
            == signature
        ):
            continue
        if current is not None:
            current.superseded_at = now
        session.add(
            IdentityLink(
                source_asset_id=asset.id,
                target_asset_id=desired["target"],
                status=desired["status"],
                method=desired["method"],
                evidence=desired["evidence"],
                column_match=desired["column_match"],
            )
        )
        changes.append(
            {
                "asset_id": asset.id,
                "before": {"status": current.status, "target": current.target_asset_id} if current else {},
                "after": {
                    "status": desired["status"],
                    "target": desired["target"],
                    "reason": desired["evidence"].get("reason"),
                },
            }
        )
    return changes


# --- 指纹 -------------------------------------------------------------------------


def current_fingerprints(session: Session) -> dict[tuple[str, str], ColumnFingerprint]:
    return {
        (f.asset_id, f.column_name): f
        for f in session.scalars(select(ColumnFingerprint).where(ColumnFingerprint.superseded_at.is_(None)))
    }


def refresh_fingerprints(session: Session, run_id: str | None) -> list[dict[str, Any]]:
    """重算全部字段推导指纹；返回变化列表（首次出现不算变化）。"""
    session.flush()
    computed = compute_fingerprints(GraphState(session))
    existing = current_fingerprints(session)
    now = utcnow()
    changes = []
    for key, fp in computed.items():
        old = existing.get(key)
        if old is not None and old.fingerprint == fp.fingerprint and old.status == fp.status:
            continue
        if old is not None:
            old.superseded_at = now
            changes.append(
                {
                    "asset_id": key[0],
                    "column": key[1],
                    "before": {"fingerprint": old.fingerprint, "status": old.status},
                    "after": {"fingerprint": fp.fingerprint, "status": fp.status, "reason": fp.reason},
                }
            )
        session.add(
            ColumnFingerprint(
                asset_id=key[0],
                column_name=key[1],
                fingerprint=fp.fingerprint,
                status=fp.status,
                reason=fp.reason,
                detail=fp.detail,
                run_id=run_id,
                computed_at=now,
            )
        )
    for key, old in existing.items():
        if key not in computed:
            old.superseded_at = now
            changes.append(
                {
                    "asset_id": key[0],
                    "column": key[1],
                    "before": {"fingerprint": old.fingerprint, "status": old.status},
                    "after": {"fingerprint": None, "status": "broken", "reason": "字段或资产已不存在"},
                }
            )
    session.flush()
    return changes


def fingerprint_map(session: Session) -> dict[tuple[str, str], str]:
    return {k: v.fingerprint for k, v in current_fingerprints(session).items()}


def latest_run(session: Session, datasource_id: str) -> CollectionRun | None:
    return session.scalar(
        select(CollectionRun)
        .where(CollectionRun.datasource_id == datasource_id)
        .order_by(CollectionRun.started_at.desc())
        .limit(1)
    )


def freshness_boundary(session: Session, datasource_id: str) -> dict[str, datetime | None]:
    """新鲜度边界：最近一次成功采集的时间。轮询采集有延迟：未发现变更 ≠ 没有变更。"""
    run = session.scalar(
        select(CollectionRun)
        .where(CollectionRun.datasource_id == datasource_id, CollectionRun.status == "succeeded")
        .order_by(CollectionRun.finished_at.desc())
        .limit(1)
    )
    return {"last_collected_at": run.finished_at if run else None}
