"""粒度：来源、传播与关联基数。

存储的粒度（grain 表）：
  observed  数据库观察：StarRocks 主键 / 唯一键表的键；MySQL 主键与唯一索引；
  parsed    视图定义的 GROUP BY 键（都出现在输出中时）或 SELECT DISTINCT；
  declared  人工声明（明细模型表、外部表等），绑定声明时键列的推导指纹，指纹变化即失效。
推导的粒度（不存储，按当前状态计算）：
  linked    外部表 / Catalog 表经身份映射继承他库表的键；
  passthrough  单表视图（或只有 N:1 关联）透传来源表的粒度。

关联基数：右侧关联列覆盖右侧某个有效粒度 → N:1（输出保持左侧粒度）；否则 1:N 或 N:N，可能放大。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from avelis.i18n import Msg
from avelis.lineage.sql import PARSER_VERSION
from avelis.models import Asset, Definition, Grain, IdentityLink, ParseResult

USABLE = ("verified", "unverified")


@dataclass
class EffectiveGrain:
    columns: list[str]
    origin: str
    status: str
    grain_id: str | None = None
    note: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "columns": self.columns,
            "origin": self.origin,
            "status": self.status,
            "grain_id": self.grain_id,
            "note": self.note,
        }


@dataclass
class Cardinality:
    value: str  # n1 | 11 | 1n | nn | unknown
    basis: dict[str, Any] = field(default_factory=dict)

    @property
    def fans_out(self) -> bool:
        return self.value in ("1n", "nn", "unknown")


def effective_grains(
    session: Session, asset_id: str, fingerprints: dict[tuple[str, str], str] | None = None, _depth: int = 0
) -> list[EffectiveGrain]:
    asset = session.get(Asset, asset_id)
    if asset is None or asset.status != "active" or _depth > 8:
        return []
    grains: list[EffectiveGrain] = []
    rows = session.scalars(select(Grain).where(Grain.asset_id == asset_id, Grain.superseded_at.is_(None)))
    for g in rows:
        if g.origin in ("observed", "parsed") and g.asset_version_id != asset.current_version_id:
            continue
        if g.origin == "declared" and fingerprints is not None:
            stale = [c for c in g.columns if fingerprints.get((asset_id, c)) != g.column_fingerprints.get(c)]
            if stale:
                grains.append(
                    EffectiveGrain(
                        g.columns,
                        g.origin,
                        "stale",
                        g.id,
                        Msg("键列推导已变化：{columns}，需重新声明", columns=", ".join(stale)),
                    )
                )
                continue
        grains.append(EffectiveGrain(list(g.columns), g.origin, g.status, g.id, g.reason))

    if asset.kind in ("external_table", "catalog_table"):
        link = session.scalar(
            select(IdentityLink).where(IdentityLink.source_asset_id == asset_id, IdentityLink.superseded_at.is_(None))
        )
        if link and link.status == "confirmed" and link.target_asset_id:
            for g in effective_grains(session, link.target_asset_id, fingerprints, _depth + 1):
                if g.status in USABLE:
                    grains.append(
                        EffectiveGrain(g.columns, "linked", g.status, g.grain_id, Msg("经身份映射继承他库表的键"))
                    )

    if asset.kind == "view" and asset.current_version_id:
        grains.extend(_passthrough(session, asset, fingerprints, _depth))
    return _dedupe(grains)


def _passthrough(session: Session, asset: Asset, fingerprints, depth: int) -> list[EffectiveGrain]:
    definition = session.scalar(select(Definition).where(Definition.asset_version_id == asset.current_version_id))
    if definition is None:
        return []
    parse = session.scalar(
        select(ParseResult).where(
            ParseResult.definition_id == definition.id, ParseResult.parser_version == PARSER_VERSION
        )
    )
    passthrough = (parse.details or {}).get("passthrough") if parse else None
    if not passthrough:
        return []
    base = resolve_qualified(session, asset.datasource_id, passthrough["base"])
    if base is None:
        return []
    for join in parse.details.get("joins", []):
        right = resolve_qualified(session, asset.datasource_id, join.get("right_table") or "")
        if right is None:
            return []
        right_cols = [c for _, c in join["right"]]
        if not covers(right_cols, effective_grains(session, right.id, fingerprints, depth + 1)):
            return []
    mapping = passthrough["mapping"]
    out = []
    for g in effective_grains(session, base.id, fingerprints, depth + 1):
        if g.status in USABLE and all(c in mapping for c in g.columns):
            out.append(
                EffectiveGrain(
                    [mapping[c] for c in g.columns],
                    "passthrough",
                    g.status,
                    g.grain_id,
                    Msg("透传自 {table}", table=f"{base.db_name}.{base.name}"),
                )
            )
    return out


def _dedupe(grains: list[EffectiveGrain]) -> list[EffectiveGrain]:
    seen, out = set(), []
    for g in grains:
        key = (tuple(sorted(g.columns)), g.status)
        if key not in seen:
            seen.add(key)
            out.append(g)
    return out


def covers(columns: list[str], grains: list[EffectiveGrain]) -> EffectiveGrain | None:
    wanted = set(columns)
    for g in grains:
        if g.status in USABLE and g.columns and set(g.columns) <= wanted:
            return g
    return None


def cardinality(
    session: Session,
    left_asset_id: str,
    left_columns: list[str],
    right_asset_id: str,
    right_columns: list[str],
    fingerprints=None,
    right_grains: list[EffectiveGrain] | None = None,
) -> Cardinality:
    rg = right_grains if right_grains is not None else effective_grains(session, right_asset_id, fingerprints)
    lg = effective_grains(session, left_asset_id, fingerprints)
    right_unique = covers(right_columns, rg)
    left_unique = covers(left_columns, lg)
    basis = {
        "right_grain": right_unique.to_dict() if right_unique else None,
        "left_grain": left_unique.to_dict() if left_unique else None,
        "right_known_grains": [g.to_dict() for g in rg],
        "left_known_grains": [g.to_dict() for g in lg],
    }
    if right_unique and left_unique:
        value = "11"
    elif right_unique:
        value = "n1"
    elif left_unique:
        value = "1n"
    elif rg or lg:
        value = "nn"
    else:
        value = "unknown"
    if right_unique and right_unique.status == "unverified" and right_unique.origin == "declared":
        basis["warning"] = "右侧粒度为人工声明且尚未核验"
    return Cardinality(value, basis)


def resolve_qualified(session: Session, datasource_id: str, qualified: str) -> Asset | None:
    parts = qualified.split(".")
    if len(parts) != 3:
        return None
    return session.scalar(
        select(Asset).where(
            Asset.datasource_id == datasource_id,
            Asset.catalog_name == parts[0],
            Asset.db_name == parts[1],
            Asset.name == parts[2],
            Asset.status == "active",
        )
    )
