"""字段推导指纹。

实体表字段：字段身份（字段 ID，删除后同名重建是新 ID）+ 类型。
外部表 / Catalog 表字段：自身字段身份 + 类型 + 身份映射到的他库字段指纹。
视图字段：本列表达式 + 取值来源的指纹 + 视图内全部影响条件（过滤、关联键、分组）及其来源指纹。

因此：视图表达式变化、过滤或关联变化、上游字段被删除或重建、他库字段类型变化，都会改变指纹；
只改格式不会。指纹 status：ok 正常；broken 上游缺失；unknown 证据不全（未解析、来源未采集）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from avelis.connectors.base import sha256_json
from avelis.lineage.sql import PARSER_VERSION
from avelis.models import (
    Asset,
    AssetVersion,
    ColumnVersion,
    Definition,
    IdentityLink,
    LineageEdge,
    ParseResult,
)

BASE_KINDS = {"table", "materialized_view"}
EXTERNAL_KINDS = {"external_table", "catalog_table"}


@dataclass
class Fingerprint:
    fingerprint: str
    status: str
    reason: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)


class GraphState:
    """一次计算所需的全部当前状态，按需从元数据库读入。"""

    def __init__(self, session: Session):
        self.assets = {a.id: a for a in session.scalars(select(Asset))}
        current_ids = [a.current_version_id for a in self.assets.values() if a.current_version_id]
        self.versions = {v.id: v for v in session.scalars(
            select(AssetVersion).where(AssetVersion.id.in_(current_ids)))} if current_ids else {}
        self.columns: dict[str, dict[str, ColumnVersion]] = {}
        if current_ids:
            for col in session.scalars(select(ColumnVersion).where(ColumnVersion.asset_version_id.in_(current_ids))):
                self.columns.setdefault(col.asset_version_id, {})[col.name] = col
        self.definitions = {d.asset_version_id: d for d in session.scalars(
            select(Definition).where(Definition.asset_version_id.in_(current_ids)))} if current_ids else {}
        def_ids = [d.id for d in self.definitions.values()]
        self.parses = {p.definition_id: p for p in session.scalars(select(ParseResult).where(
            ParseResult.definition_id.in_(def_ids), ParseResult.parser_version == PARSER_VERSION))} if def_ids else {}
        self.edges: dict[str, list[LineageEdge]] = {}
        for edge in session.scalars(select(LineageEdge).where(LineageEdge.status == "active",
                                                              LineageEdge.origin.in_(("parsed", "manual")))):
            self.edges.setdefault(edge.target_asset_id, []).append(edge)
        self.links = {link.source_asset_id: link for link in session.scalars(
            select(IdentityLink).where(IdentityLink.superseded_at.is_(None)))}

    def column(self, asset_id: str, name: str) -> ColumnVersion | None:
        asset = self.assets.get(asset_id)
        if asset is None or asset.status != "active" or not asset.current_version_id:
            return None
        return self.columns.get(asset.current_version_id, {}).get(name)

    def column_names(self, asset_id: str) -> list[str]:
        asset = self.assets[asset_id]
        return list(self.columns.get(asset.current_version_id or "", {}).keys())


def compute_fingerprints(state: GraphState) -> dict[tuple[str, str], Fingerprint]:
    memo: dict[tuple[str, str], Fingerprint] = {}
    active = set()

    def fp(asset_id: str, column: str) -> Fingerprint:
        key = (asset_id, column)
        if key in memo:
            return memo[key]
        if key in active:
            return Fingerprint(sha256_json(["cycle", asset_id, column]), "unknown", "血缘存在环")
        active.add(key)
        try:
            result = _compute(asset_id, column)
        finally:
            active.discard(key)
        memo[key] = result
        return result

    def _compute(asset_id: str, column: str) -> Fingerprint:
        asset = state.assets.get(asset_id)
        if asset is None or asset.status != "active":
            return Fingerprint(sha256_json(["missing-asset", asset_id, column]), "broken", "上游资产已删除")
        col = state.column(asset_id, column)
        if col is None:
            return Fingerprint(sha256_json(["missing-column", asset_id, column]), "broken",
                               f"字段 {asset.db_name}.{asset.name}.{column} 已不存在")
        if asset.kind in BASE_KINDS or (asset.kind == "table"):
            return Fingerprint(sha256_json(["base", col.column_id, col.data_type]), "ok")
        if asset.kind in EXTERNAL_KINDS:
            return _external(asset, col)
        if asset.kind == "view":
            return _view(asset, column)
        return Fingerprint(sha256_json(["opaque", col.column_id, col.data_type]), "unknown",
                           f"{asset.kind} 的字段来源未解析")

    def _external(asset: Asset, col: ColumnVersion) -> Fingerprint:
        own = ["ext", col.column_id, col.data_type]
        link = state.links.get(asset.id)
        if link is None or link.status not in ("confirmed", "candidate") or not link.target_asset_id:
            reason = "外部来源未匹配到他库真实表" if link is None else f"外部来源匹配状态：{link.status}"
            return Fingerprint(sha256_json(own + ["unlinked", link.status if link else None]), "unknown", reason,
                               {"link_status": link.status if link else None})
        upstream = fp(link.target_asset_id, col.name)
        status = upstream.status if upstream.status != "ok" else ("ok" if link.status == "confirmed" else "unknown")
        return Fingerprint(sha256_json(own + [link.target_asset_id, upstream.fingerprint]), status,
                           upstream.reason or (None if link.status == "confirmed" else "外部来源仅为候选匹配"),
                           {"link_status": link.status, "upstream": [link.target_asset_id, col.name]})

    def _view(asset: Asset, column: str) -> Fingerprint:
        definition = state.definitions.get(asset.current_version_id or "")
        if definition is None:
            return Fingerprint(sha256_json(["no-definition", asset.id, column]), "unknown", "未读到视图定义")
        parse = state.parses.get(definition.id)
        if parse is None or parse.status in ("failed", "unsupported"):
            reason = parse.reason if parse else "定义尚未解析"
            return Fingerprint(sha256_json(["unparsed", definition.canonical_hash, column]), "unknown", reason)
        edges = [e for e in state.edges.get(asset.id, []) if e.target_asset_version_id == asset.current_version_id]
        values = [e for e in edges if e.target_column == column and e.relation.startswith("value_")]
        influences = [e for e in edges if e.target_column is None]
        expression = next((c["expression"] for c in parse.details.get("columns", []) if c["name"] == column), None)
        statuses, reasons, parts_v, parts_i = [], [], [], []
        for edge in values + influences:
            if edge.source_asset_id is None:
                statuses.append("unknown")
                reasons.append(f"来源 {edge.source_ref} 未采集")
                part = ["unresolved", edge.source_ref, edge.source_column]
            else:
                up = fp(edge.source_asset_id, edge.source_column or "")
                statuses.append(up.status)
                if up.reason:
                    reasons.append(up.reason)
                part = [edge.source_asset_id, edge.source_column, up.fingerprint]
            if edge.target_column is None:
                parts_i.append([edge.relation, edge.expression, *part])
            else:
                parts_v.append([edge.relation, *part])
        if parse.status == "partial":
            statuses.append("unknown")
            reasons.append(parse.reason or "部分解析")
        status = "broken" if "broken" in statuses else "unknown" if "unknown" in statuses else "ok"
        digest = sha256_json(["view", expression, sorted(map(str, parts_v)), sorted(map(str, parts_i))])
        return Fingerprint(digest, status, "；".join(dict.fromkeys(reasons)) or None,
                           {"expression": expression, "value_sources": len(values), "influences": len(influences)})

    for asset in state.assets.values():
        if asset.status != "active" or not asset.current_version_id:
            continue
        for name in state.column_names(asset.id):
            fp(asset.id, name)
    return memo
