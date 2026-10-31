"""探索查询的绑定、执行与审计（ADR 0009 第 6 点）；校验本身在 explore.py（纯函数）。

- 资产可见 = 整表全行全列可读（v0.1 没有列级、行级权限）；表源绑定到 数据源 + 资产 + 资产版本 + 物理身份；
- 物理类型按引擎档案：StarRocks 只允许 default_catalog 下的原生内表；PostgreSQL 只允许当前库内的普通表（生成 FROM ONLY，
  不扫描继承子表），分区表拒绝；视图只有上游闭包全部可见、且上游血缘已确认时才允许；外部表、外部 catalog、物化视图拒绝；
- 执行前用只读账号核对物理身份与列（资产定义或依赖变化后已绑定的版本失效，拒绝且不执行）；执行前后各核对一次权限，
  撤权后不再交付结果；
- 每次探索查询都记审计：用户、运行、数据源与引擎、方言 / sqlglot / 档案版本、实际执行的 SQL、绑定的资产与版本、
  执行身份、资源上限、结果与完整性摘要；拒绝的记稳定错误码与阶段，并写明没有发到数据库。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from avelis import audit
from avelis.analysis.explore import (
    BoundColumn,
    BoundTable,
    ExploreProfile,
    ExploreRejected,
    OutputColumn,
    Validated,
    profile_for,
    validate,
)
from avelis.connectors.base import QueryError, QueryResult
from avelis.governance import access
from avelis.models import Asset, AssetVersion, ColumnVersion, Datasource, DatasourceCredential, Definition, User



@dataclass
class ExploreSource:
    datasource: Datasource
    profile: ExploreProfile
    default_catalog: str


def explore_source(session: Session, ref: str) -> ExploreSource:
    """按名称或编号找可以探索的数据源：开关开启、引擎有探索档案、具备问数执行能力。"""
    from avelis.engines import dialect_of, engine_of
    from avelis.engines.base import QUERY

    ds = session.get(Datasource, ref) or session.scalar(select(Datasource).where(Datasource.name == ref))
    if ds is None:
        raise ExploreRejected("binding", "datasource_unknown", f"数据源 {ref} 不存在")
    profile = profile_for(ds.kind)
    engine = engine_of(ds)
    if profile is None or not engine.supports(QUERY):
        raise ExploreRejected("binding", "engine_unsupported", f"{engine.label} 尚不支持探索查询")
    if not ds.allow_exploration:
        raise ExploreRejected("binding", "exploration_disabled", f"数据源 {ds.name} 已关闭探索查询")
    return ExploreSource(ds, profile, dialect_of(ds).default_catalog)


def route(session: Session, user: User, sql_text: str) -> ExploreSource:
    """方言路由（ADR 0009 第 6 点）：按标准 SQL 里的资产确定数据源，模型不填数据源。
    两段名 库.表 按各数据源的默认 catalog 解释（与 SQL 本身的含义一致），三段名精确匹配；只认当前用户可见的已采集资产，
    不可见与不存在报同样的错误。同名表落在多个数据源时要求写三段名；一条查询涉及多个数据源时拒绝。"""
    from avelis.analysis.explore import table_refs
    from avelis.engines import dialect_of

    allowed = access.domain_ids(session, user)
    defaults: dict[str, str | None] = {}
    chosen: dict[str, Datasource] = {}
    for catalog, db, name in table_refs(sql_text):
        label = ".".join(x for x in (catalog, db, name) if x)
        if not db:
            raise ExploreRejected("binding", "table_unqualified", f"表 {name} 需要写出库名（库.表）")
        query = select(Asset).where(Asset.db_name == db, Asset.name == name, Asset.status == "active")
        if catalog:
            query = query.where(Asset.catalog_name == catalog)
        hits = []
        for asset in session.scalars(query):
            if not access.can_read_asset(session, user, asset, allowed):
                continue
            ds = session.get(Datasource, asset.datasource_id)
            if ds is None:
                continue
            if not catalog:  # 两段名：只指该数据源默认 catalog 下的表
                if ds.id not in defaults:
                    try:
                        defaults[ds.id] = dialect_of(ds).default_catalog
                    except Exception:  # noqa: BLE001 - 未知引擎：不参与两段名解释
                        defaults[ds.id] = None
                if asset.catalog_name != defaults[ds.id]:
                    continue
            hits.append((asset, ds))
        sources = {ds.id: ds for _, ds in hits}
        if not sources:
            raise ExploreRejected("binding", "asset_not_visible", f"表 {label} 不存在或当前用户不可见")
        if len(sources) > 1:
            raise ExploreRejected("binding", "ambiguous_table", f"表 {label} 在多个数据源里都有，请写出 catalog.库.表",
                                  sorted(f"{a.catalog_name}.{a.db_name}.{a.name}（{ds.name}）" for a, ds in hits))
        chosen.update(sources)
    if not chosen:
        raise ExploreRejected("syntax", "no_source", "每个查询块都必须有表源（FROM 已采集的表）")
    if len(chosen) > 1:
        raise ExploreRejected("binding", "cross_datasource",
                              "一条探索查询只能用同一个数据源的表："
                              f"{'、'.join(sorted(d.name for d in chosen.values()))}，请分别查询后用计算工具合并")
    return explore_source(session, next(iter(chosen)))


def resolver(session: Session, user: User, source: ExploreSource):
    """表源绑定：只认当前用户可见的已采集资产；不可见与不存在报同样的错误（不泄露存在性）。"""
    from avelis.ask.service import upstream_closure

    allowed = access.domain_ids(session, user)

    def deny(name: str, code: str = "asset_not_visible", detail: str = "不存在或当前用户不可见") -> ExploreRejected:
        return ExploreRejected("binding", code, f"表 {name} {detail}")

    def resolve(catalog: str | None, db: str | None, name: str) -> BoundTable:
        label = ".".join(x for x in (catalog, db, name) if x)
        if not db:
            raise ExploreRejected("binding", "table_unqualified", f"表 {name} 需要写出库名（库.表）")
        catalog = catalog or source.default_catalog
        asset = session.scalar(select(Asset).where(
            Asset.datasource_id == source.datasource.id, Asset.catalog_name == catalog, Asset.db_name == db,
            Asset.name == name, Asset.status == "active"))
        if asset is None or not access.can_read_asset(session, user, asset, allowed):
            raise deny(label)
        version = session.get(AssetVersion, asset.current_version_id) if asset.current_version_id else None
        if version is None:
            raise deny(label, "asset_version_missing", "没有已采集的版本")
        use_only, definition, upstream_versions = False, None, ()
        if asset.kind == "view":
            upstream = upstream_closure(session, [asset.id])
            closure = dict(upstream)
            upstream.pop(asset.id, None)
            if not upstream:
                raise deny(label, "view_lineage_unknown", "是视图，但上游血缘未确认，无法判断上游是否都可见")
            if _unresolved_lineage(session, closure):  # 固定文案：未绑定的来源名可能是用户无权看到的表
                raise deny(label, "view_lineage_unresolved", "是视图，但上游血缘不完整，无法确认可读范围，拒绝执行")
            if not all(access.can_read_asset(session, user, a, allowed) for a in upstream.values()):
                raise deny(label, "view_upstream_not_visible", "是视图，它的上游有当前用户不可见的资产")
            definition = session.scalar(select(Definition.raw_sql).where(Definition.asset_version_id == version.id))
            if definition is None:
                raise deny(label, "view_definition_missing", "是视图，但没有采集到定义")
            upstream_versions = tuple(sorted((a.id, a.current_version_id) for a in upstream.values()))
        elif asset.kind != "table":
            raise deny(label, "kind_not_allowed", f"是 {asset.kind}，探索查询只允许普通表与上游可见的视图")
        elif source.profile.engine == "starrocks":
            if catalog != "default_catalog" or not version.table_model:
                raise deny(label, "kind_not_allowed", "不是 default_catalog 下的原生内表")
        elif source.profile.engine == "postgres":
            relkind = (version.properties or {}).get("relkind")
            if relkind == "p":
                raise deny(label, "partitioned_table", "是分区表（查询父表会扫描全部分区），第一版不支持")
            if relkind != "r":
                raise deny(label, "kind_not_allowed", "不是普通表")
            use_only = True
        columns = tuple(BoundColumn(c.name, c.data_type, c.comment) for c in session.scalars(
            select(ColumnVersion).where(ColumnVersion.asset_version_id == version.id).order_by(ColumnVersion.ordinal)))
        return BoundTable(asset.id, version.id, asset.physical_id, catalog, db, name, asset.kind, columns, use_only,
                          definition, upstream_versions)
    return resolve


def _unresolved_lineage(session: Session, closure: dict[str, Asset]) -> str | None:
    """视图上游闭包里的血缘必须全部解析并绑定到资产：有来源未绑定（source_asset_id 为空）的边，或闭包里的视图没有
    任何已确认的来源边，都返回说明（拒绝）。"""
    from avelis.models import LineageEdge

    for asset in closure.values():
        edges = list(session.execute(select(LineageEdge.source_asset_id, LineageEdge.source_ref).where(
            LineageEdge.target_asset_id == asset.id, LineageEdge.status == "active",
            LineageEdge.origin.in_(("parsed", "manual")))))
        unbound = [ref for source, ref in edges if source is None]
        if unbound:
            return f"{asset.db_name}.{asset.name} ← {unbound[0]}"
        if asset.kind in ("view", "materialized_view") and not edges:
            return f"{asset.db_name}.{asset.name} 的上游血缘未确认"
    return None


def live_check(connector: Any, validated: Validated, *, timeout_seconds: int) -> None:
    """执行前核对物理身份与列：与绑定的资产版本不一致就拒绝（资产定义已变化），探索查询不执行。
    核对查询同样受统一截止时间约束（timeout_seconds 取剩余时间）。"""
    objects = [(t.db, t.name) for t in validated.tables]
    current = connector.describe_objects(objects, timeout_seconds=timeout_seconds)
    for t in validated.tables:
        now = current.get((t.db, t.name)) or {}
        if t.physical_id and now.get("physical_id") != t.physical_id:
            raise ExploreRejected("binding", "asset_changed", f"表 {t.qualified} 的物理身份已变化，请等待重新采集")
        expected = [c.name for c in t.columns]
        if [c[0] for c in now.get("columns") or []] != expected:
            raise ExploreRejected("binding", "asset_changed", f"表 {t.qualified} 的列已变化，请等待重新采集")
        if t.kind == "view" and _normalized(now.get("definition")) != _normalized(t.definition):
            raise ExploreRejected("binding", "asset_changed", f"视图 {t.qualified} 的定义已变化，请等待重新采集")


def _normalized(definition: str | None) -> str | None:
    return " ".join(definition.split()) if definition else None


def recheck_access(session: Session, user: User, validated: Validated) -> None:
    """执行前、交付前再核对一次：撤权后停止读取，也不再交付结果。"""
    allowed = access.domain_ids(session, user)
    for t in validated.tables:
        asset = session.get(Asset, t.asset_id, populate_existing=True)
        if asset is None or asset.status != "active" or not access.can_read_asset(session, user, asset, allowed):
            raise ExploreRejected("binding", "access_revoked", f"表 {t.qualified} 当前已无权访问")
        if asset.current_version_id != t.asset_version_id:
            raise ExploreRejected("binding", "asset_changed", f"表 {t.qualified} 已有新版本，请重新提交查询")
        for upstream_id, version_id in t.upstream:  # 视图的上游闭包一起复查
            up = session.get(Asset, upstream_id, populate_existing=True)
            if up is None or up.status != "active" or not access.can_read_asset(session, user, up, allowed):
                raise ExploreRejected("binding", "access_revoked", f"视图 {t.qualified} 的上游当前已无权访问")
            if up.current_version_id != version_id:
                raise ExploreRejected("binding", "asset_changed", f"视图 {t.qualified} 的上游已有新版本，请重新提交查询")


def units(columns: list[OutputColumn]) -> dict[str, dict]:
    """有效量纲只由后端按表达式推断；探索数据没有口径单位。"""
    return {c.name: {"kind": c.kind} for c in columns if c.kind != "text"}


def evidence(columns: list[OutputColumn]) -> dict[str, dict]:
    return {c.name: {"eligible": c.eligible, "label_ok": c.label_ok, "assumption": c.assumption,
                     "expression": c.expression, "deps": c.deps} for c in columns}


def execution_identity(session: Session, ds: Datasource) -> str | None:
    cred = session.scalar(select(DatasourceCredential).where(DatasourceCredential.datasource_id == ds.id,
                                                             DatasourceCredential.purpose == "query"))
    return cred.username if cred else None


def digest(result: QueryResult) -> str:
    return hashlib.sha256(json.dumps([result.columns, result.rows], ensure_ascii=False, default=str).encode()).hexdigest()


def record(session: Session, user: User, run_id: str, *, step: int, source: ExploreSource | None, purpose: str,
           outcome: str, validated: Validated | None = None, error: ExploreRejected | QueryError | None = None,
           result: QueryResult | None = None, limits: dict | None = None, identity: str | None = None) -> None:
    detail: dict[str, Any] = {"run_id": run_id, "step": step, "purpose": purpose, "outcome": outcome,
                              "sent_to_database": outcome in ("ok", "failed")}
    if source is not None:
        detail.update(datasource_id=source.datasource.id, engine=source.datasource.kind,
                      engine_version=(source.datasource.server_identity or {}).get("version"),
                      profile=f"{source.profile.engine}/{source.profile.version}", dialect=source.profile.dialect)
    if validated is not None:
        detail.update(sql=validated.sql, sqlglot=validated.sqlglot, row_limit=validated.limit,
                      assets=[{"asset_id": t.asset_id, "asset_version_id": t.asset_version_id,
                               "physical_id": t.physical_id, "qualified": t.qualified} for t in validated.tables],
                      columns=[{"name": c.name, "deps": c.deps} for c in validated.columns])
    if isinstance(error, ExploreRejected):
        detail.update(stage=error.stage, code=error.code, message=error.message)
    elif isinstance(error, QueryError):
        detail.update(code=error.code, message=error.message)
        if getattr(error, "cancel", None):
            detail["cancel"] = error.cancel  # kill_sent / already_finished / cancel_failed
        if getattr(error, "cancel_error", None):
            detail["cancel_error"] = error.cancel_error  # 取消失败时的异常类别与消息（只进审计）
    if result is not None:
        detail.update(rows=len(result.rows), truncated=result.truncated, digest=digest(result))
        if getattr(result, "warnings", None):
            detail["query_warnings"] = list(result.warnings)
    if limits:
        detail["limits"] = limits
    if identity:
        detail["execution_identity"] = identity
    audit.record(session, user, "analysis.explore", "analysis_run", run_id, **detail)


__all__ = ["ExploreRejected", "ExploreSource", "evidence", "explore_source", "live_check", "record", "route",
           "recheck_access", "resolver", "units", "validate"]
