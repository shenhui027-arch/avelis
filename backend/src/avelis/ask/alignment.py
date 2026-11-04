"""筛选值只按成员值和受治理的业务维表对齐；启发式匹配只提供候选。"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import sqlglot
from sqlalchemy import or_, select
from sqlalchemy.orm import Session
from sqlglot import exp

from avelis.ask import service
from avelis.ask.compiler import ValidationFailed, distinct_values_sql
from avelis.ask.intent import DimensionFilter
from avelis.connectors.base import QueryError
from avelis.engines import CapabilityMissing, CredentialMissing, UnsupportedEngine
from avelis.engines.base import QUERY
from avelis.governance.errors import GovernanceError
from avelis.governance.joins import effectively_approved
from avelis.governance.metric_sql import column_ref
from avelis.governance.metrics import encoded_time_type
from avelis.models import Asset, ColumnVersion, Datasource, JoinPath, MetricVersion, User

_TEXT_TYPE = re.compile(
    r"^(?:n?(?:var)?char(?:acter)?2?(?:\s+varying)?|(?:tiny|medium|long)?text|n?clob|string|citext)\b", re.I
)
_TABLE_LIMIT = 3
_CANDIDATE_LIMIT = 10
_READ_FAILURES = (
    GovernanceError,
    QueryError,
    ValidationFailed,
    CapabilityMissing,
    CredentialMissing,
    UnsupportedEngine,
)


@dataclass
class AlignmentResult:
    filters: list[DimensionFilter]
    explanations: list[dict[str, Any]] = field(default_factory=list)
    unresolved: list[dict[str, Any]] = field(default_factory=list)
    unknown: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class _DimensionTable:
    asset: Asset
    key: str
    text_columns: list[str]
    closure: dict[str, Asset]
    path: JoinPath


def _normalized(value: str) -> str:
    return "".join(unicodedata.normalize("NFKC", value).split()).casefold()


def _candidates(original: str, sample: list[str]) -> list[str]:
    values = list(dict.fromkeys(sample))
    contained = [value for value in values if original and value and (original in value or value in original)]
    return (contained or values)[:_CANDIDATE_LIMIT]


def _is_text_type(data_type: str) -> bool:
    name = data_type.strip()
    # 先解包标量包装，再排除数组；不能用文本前缀把 text[] 当作标量。
    while match := re.fullmatch(r"(?:LowCardinality|Nullable)\s*\((.*)\)", name, re.I):
        name = match[1].strip()
    if name.endswith("[]") or name.startswith("_") or re.match(r"ARRAY\s*[<(]", name, re.I):
        return False
    return encoded_time_type(name) == "string" or bool(_TEXT_TYPE.match(name))


def candidate_sources(pending: list[dict], explanations: list[dict] | None = None) -> dict:
    """合并未解决候选及跨口径冲突值的来源，不丢失任何维表依赖。"""
    sources: dict[str, list[str]] = {}
    for item in pending:
        for value, ids in item.get("candidate_sources", {}).items():
            sources.setdefault(value, []).extend(ids)
    for item in explanations or []:
        if item.get("method") == "dimension_table":
            sources.setdefault(item["value"], []).append(item.get("asset_id"))
    return {"candidate_sources": {value: list(dict.fromkeys(ids)) for value, ids in sources.items()}} if sources else {}


def confirmed_values(items: list[dict], answer: str | list[dict]) -> list[dict]:
    """只接受已展示候选中同维度、同原话的选择；字符串兼容首项回答。"""
    if isinstance(answer, str):
        answer = [{**items[0], "value": answer}] if items else []
    offered = {(item["dimension"], item["original"]): item for item in items}
    confirmed = []
    for selection in answer:
        item = offered.get((selection["dimension"], selection["original"]))
        value = selection["value"]
        if item is None or value not in item.get("options", []):
            continue
        confirmed.append(
            {
                "dimension": item["dimension"],
                "original": item["original"],
                "value": value,
                "asset_ids": item.get("candidate_sources", {}).get(value, []),
            }
        )
    return confirmed


def _dimension_tables(session: Session, user: User, version: MetricVersion, dimension: str) -> list[_DimensionTable]:
    """关联方向和唯一性来自登记的路径；不从列名、数据值或业务词推断维表。"""
    resolved = service.validate_spec(session, version)
    definition = next((d for d in version.dimensions or [] if d["name"] == dimension), None)
    if definition is None:
        return []
    aliases = resolved["aliases"]
    base = aliases.get("b")
    if base is None:
        return []
    dialect = service.dialect_of(session.get(Datasource, base.datasource_id))
    alias, column = column_ref(definition["column"], dialect=dialect.name, label="维度字段", aliases=set(aliases))
    source = aliases[alias]
    allowed = service.access.domain_ids(session, user)
    out = []
    for path in session.scalars(
        select(JoinPath)
        .where(or_(JoinPath.left_asset_id == source.id, JoinPath.right_asset_id == source.id))
        .order_by(JoinPath.code, JoinPath.id)
    ):
        if len(path.left_columns) != 1 or len(path.right_columns) != 1 or not effectively_approved(session, path):
            continue
        if path.left_asset_id == source.id and path.left_columns == [column] and path.cardinality in ("n1", "11"):
            table_id, key = path.right_asset_id, path.right_columns[0]
        elif path.right_asset_id == source.id and path.right_columns == [column] and path.cardinality in ("1n", "11"):
            table_id, key = path.left_asset_id, path.left_columns[0]
        else:
            continue
        asset = session.get(Asset, table_id)
        if asset is None or asset.status != "active" or not asset.current_version_id:
            continue
        closure = service.upstream_closure(session, [source.id, asset.id])
        if not all(service.access.can_read_asset(session, user, a, allowed) for a in closure.values()):
            continue
        datasource = session.get(Datasource, asset.datasource_id)
        if datasource is None or not service.engine_of(datasource).supports(QUERY):
            continue
        columns = list(
            session.scalars(
                select(ColumnVersion)
                .where(ColumnVersion.asset_version_id == asset.current_version_id)
                .order_by(ColumnVersion.ordinal)
            )
        )
        text_columns = [c.name for c in columns if _is_text_type(c.data_type)]
        if key in {c.name for c in columns} and text_columns:
            out.append(_DimensionTable(asset, key, text_columns, closure, path))
    return out


def has_dimension_table(session: Session, user: User, version: MetricVersion, dimension: str) -> bool:
    """是否有当前可用的可见维表路径；只读治理元数据，不执行维表查询。"""

    def available(current, seen):
        identity = getattr(current, "id", id(current))
        if identity in seen:
            return False
        if current.status != "published" or service.dependency_drift(session, current):
            return False
        if dimension not in {d["name"] for d in current.dimensions or []}:
            return False
        if current.kind != "derived":
            return bool(_dimension_tables(session, user, current, dimension))
        visited = {*seen, identity}
        return any(
            component is not None and available(component, visited)
            for item in current.components or []
            for component in [session.get(MetricVersion, item["version_id"])]
        )

    try:
        return available(version, set())
    except _READ_FAILURES:
        return False


def _table_matches(
    session: Session,
    version: MetricVersion,
    table: _DimensionTable,
    original: str,
    settings,
    remaining_seconds: Callable[[], float] | None = None,
) -> tuple[list[str], list[str]] | None:
    """先仅取不同键；唯一时再查命中列的布尔标志，不把其他维表内容带进说明。"""
    asset = table.asset
    datasource = session.get(Datasource, asset.datasource_id)
    dialect = service.dialect_of(datasource)
    qualified = f"{asset.catalog_name}.{asset.db_name}.{asset.name}"
    literal = exp.National(this=original) if dialect.national_strings else exp.Literal.string(original)
    comparisons = [exp.EQ(this=exp.column(c, table="t"), expression=literal.copy()) for c in table.text_columns]
    query = sqlglot.parse_one(
        distinct_values_sql(qualified, table.key, limit=_TABLE_LIMIT, dialect=dialect), read=dialect.name
    ).where(exp.or_(*[c.copy() for c in comparisons]))
    connector = service.query_connector(session, datasource)
    timezone = getattr(version, "timezone", None) or getattr(settings, "business_timezone", None)
    columns = {table.key, *table.text_columns}
    dependency = [*table.path.dependency, *({"asset_id": asset.id, "column": c} for c in columns)]

    def execute(tree):
        if service._dimension_query_timeout(remaining_seconds) <= 0:
            return None
        sql = tree.sql(dialect=dialect.name, identify=True)
        service.validate_sql(
            sql, dialect=dialect, allowed_tables={qualified}, schema={qualified: columns}, max_rows=_TABLE_LIMIT
        )
        if service._live_check(session, datasource, connector, table.closure.values(), dependency, settings):
            return None
        timeout = service._dimension_query_timeout(remaining_seconds)
        if timeout <= 0:
            return None
        return connector.execute_select(
            sql,
            timeout_seconds=timeout,
            mem_limit_bytes=512 * 1024**2,
            max_rows=_TABLE_LIMIT,
            timezone=timezone,
        )

    try:
        result = execute(query)
        if result is None or any(len(row) != 1 or row[0] is None for row in result.rows):
            return None
        keys = list(dict.fromkeys(str(row[0]) for row in result.rows))
        if len(keys) != 1:
            return (keys, []) if not result.truncated or len(keys) > 1 else None
        if result.truncated:
            return None
        markers = [
            exp.Case().when(comparison.copy(), exp.Literal.number(1)).else_(exp.Literal.number(0))
            for comparison in comparisons
        ]
        evidence = execute(
            query.copy()
            .select(*markers, append=False)
            .distinct(distinct=False)
            .where(
                exp.EQ(
                    this=exp.column(table.key, table="t"),
                    expression=exp.National(this=keys[0]) if dialect.national_strings else exp.Literal.string(keys[0]),
                )
            )
        )
        if evidence is None or evidence.truncated or not evidence.rows:
            return None
        if any(len(row) != len(markers) or any(value not in (0, 1) for value in row) for row in evidence.rows):
            return None
        matched = [c for i, c in enumerate(table.text_columns) if any(row[i] == 1 for row in evidence.rows)]
        return (keys, matched) if matched else None
    except _READ_FAILURES:
        return None


def _unknown(dimension: str, original: str, reason: str) -> dict[str, str]:
    return {"dimension": dimension, "original": original, "reason": reason}


def _unresolved(dimension: str, original: str, candidates: list[str], has_table: bool) -> dict[str, Any]:
    return {"dimension": dimension, "original": original, "candidates": candidates, "has_dimension_table": has_table}


def _derived_alignment(session, user, version, filters, settings, remaining_seconds) -> AlignmentResult:
    components = [session.get(MetricVersion, c["version_id"]) for c in version.components or []]
    if not components or any(component is None for component in components):
        return AlignmentResult(
            filters,
            unknown=[_unknown(f.dimension, value, "component_unavailable") for f in filters for value in f.values],
        )
    results = [
        align_filters(session, user, component, filters, settings, remaining_seconds=remaining_seconds)
        for component in components
    ]
    out = AlignmentResult([])
    for index, item in enumerate(filters):
        values = []
        for offset, original in enumerate(item.values):
            key = (item.dimension, original)
            unresolved = [u for result in results for u in result.unresolved if (u["dimension"], u["original"]) == key]
            unknown = [u for result in results for u in result.unknown if (u["dimension"], u["original"]) == key]
            aligned = [result.filters[index].values[offset] for result in results]
            if any(u["reason"] == "budget_exhausted" for u in unknown):
                values.append(original)
                out.unknown.append(_unknown(item.dimension, original, "budget_exhausted"))
            elif unresolved or (not unknown and len(set(aligned)) != 1):
                values.append(original)
                candidates = list(
                    dict.fromkeys(
                        [*(v for v in aligned if v != original), *(v for u in unresolved for v in u["candidates"])]
                    )
                )
                out.unresolved.append(
                    _unresolved(
                        item.dimension,
                        original,
                        candidates[:_CANDIDATE_LIMIT],
                        any(u["has_dimension_table"] for u in unresolved)
                        or any(
                            e.get("method") == "dimension_table" and (e["dimension"], e["original"]) == key
                            for result in results
                            for e in result.explanations
                        )
                        or has_dimension_table(session, user, version, item.dimension),
                    )
                )
                out.unresolved[-1].update(
                    candidate_sources(
                        unresolved,
                        [e for r in results for e in r.explanations if (e["dimension"], e["original"]) == key],
                    )
                )
            elif unknown:
                values.append(original)
                out.unknown.append(_unknown(item.dimension, original, "component_unknown"))
            else:
                values.append(aligned[0])
                for result in results:
                    for explanation in result.explanations:
                        if (
                            explanation["dimension"],
                            explanation["original"],
                        ) == key and explanation not in out.explanations:
                            out.explanations.append(explanation)
        out.filters.append(item.model_copy(update={"values": values}))
    return out


def align_filters(
    session: Session,
    user: User,
    version: MetricVersion,
    filters: list[DimensionFilter],
    settings,
    *,
    confirmed: list[dict] | None = None,
    remaining_seconds: Callable[[], float] | None = None,
) -> AlignmentResult:
    """保持筛选项和值的位置；未知值原样保留，只有 unresolved 需要用户确认。

    confirmed 由 confirmed_values 按已展示候选校验，包含 dimension/original/value/asset_ids。
    输入中的 (dimension, value) 命中确认记录时直接采用，并保留原话与全部来源依赖。
    其余值精确相等优先，其次是完整样本中唯一的 NFKC/去空白/casefold 相等，再查询有效的单列唯一侧维表。
    不把包含关系当作相等，也不因取样、权限或查询失败宣称成员值不存在。
    """
    copied = [item.model_copy(deep=True) for item in filters]
    result = AlignmentResult(copied)
    if not copied:
        return result
    # 在共用入口消费确认；仅未确认值递归进入成员、规范化及维表查询。
    accepted: dict[tuple[str, str], list[dict]] = {}
    for selection in confirmed or []:
        accepted.setdefault((selection["dimension"], selection["value"]), []).append(selection)
    pending = []
    for item in copied:
        values = []
        for value in item.values:
            selections = accepted.get((item.dimension, value))
            if not selections:
                values.append(value)
                continue
            for selection in selections:
                for asset_id in selection["asset_ids"] or [None]:
                    note = {
                        "dimension": item.dimension,
                        "original": selection["original"],
                        "value": value,
                        "method": "confirmed",
                        **({"asset_id": asset_id} if asset_id else {}),
                    }
                    if note not in result.explanations:
                        result.explanations.append(note)
        pending.append(item.model_copy(update={"values": values}))
    if result.explanations:
        if any(item.values for item in pending):
            remaining = align_filters(session, user, version, pending, settings, remaining_seconds=remaining_seconds)
            for item, rest in zip(copied, remaining.filters):
                aligned = iter(rest.values)
                item.values = [v if (item.dimension, v) in accepted else next(aligned) for v in item.values]
            result.explanations.extend(remaining.explanations)
            result.unresolved, result.unknown = remaining.unresolved, remaining.unknown
        return result
    if service._dimension_query_timeout(remaining_seconds) <= 0:
        result.unknown = [_unknown(f.dimension, v, "budget_exhausted") for f in copied for v in f.values]
        return result
    try:
        unavailable = version.status != "published" or bool(service.dependency_drift(session, version))
    except _READ_FAILURES:
        unavailable = True
    if unavailable:
        result.unknown = [_unknown(f.dimension, v, "version_unavailable") for f in copied for v in f.values]
        return result
    if version.kind == "derived":
        return _derived_alignment(session, user, version, copied, settings, remaining_seconds)
    query_budget = {"remaining_seconds": remaining_seconds} if remaining_seconds is not None else {}
    try:
        samples = service._dimension_values(session, user, version, settings, **query_budget)
    except _READ_FAILURES:
        samples = {}
    exact_by_dimension = {}
    for item in result.filters:
        sample = samples.get(item.dimension)
        # 取样接口和缓存只返回值列表，没有截断标志；达到上限就保守地视为可能截断。
        sample_complete = sample is not None and len(sample) < service.DIMENSION_VALUES_LIMIT
        tables: list[_DimensionTable] | None = None
        if sample is not None and not sample_complete and item.dimension not in exact_by_dimension:
            outside = list(
                dict.fromkeys(
                    value
                    for f in result.filters
                    if f.dimension == item.dimension
                    for value in f.values
                    if value not in sample
                )
            )
            if outside and service._dimension_query_timeout(remaining_seconds) > 0:
                try:
                    exact_by_dimension[item.dimension] = service._existing_dimension_values(
                        session, user, version, item.dimension, outside, settings, **query_budget
                    )
                except _READ_FAILURES:
                    exact_by_dimension[item.dimension] = None
        for offset, original in enumerate(item.values):
            if service._dimension_query_timeout(remaining_seconds) <= 0:
                result.unknown.append(_unknown(item.dimension, original, "budget_exhausted"))
                continue
            if sample is None:
                result.unknown.append(_unknown(item.dimension, original, "member_values_unavailable"))
                continue
            value, method = original, "exact"
            if original not in sample:
                if not sample_complete:
                    existing = exact_by_dimension.get(item.dimension)
                    if existing is None:
                        reason = (
                            "budget_exhausted"
                            if service._dimension_query_timeout(remaining_seconds) <= 0
                            else "exact_check_unavailable"
                        )
                        result.unknown.append(_unknown(item.dimension, original, reason))
                        continue
                    if original in existing:
                        result.explanations.append(
                            {"dimension": item.dimension, "original": original, "value": original, "method": "exact"}
                        )
                        continue
                normalized = list(dict.fromkeys(v for v in sample if _normalized(v) == _normalized(original)))
                if sample_complete and len(normalized) == 1:
                    value, method = normalized[0], "normalized"
                else:
                    try:
                        if tables is None:
                            tables = _dimension_tables(session, user, version, item.dimension)
                        matches = [
                            _table_matches(session, version, table, original, settings, remaining_seconds)
                            for table in tables
                        ]
                    except _READ_FAILURES:
                        matches = [None]
                    if any(match is None for match in matches):
                        reason = (
                            "budget_exhausted"
                            if service._dimension_query_timeout(remaining_seconds) <= 0
                            else "dimension_table_unavailable"
                        )
                        result.unknown.append(_unknown(item.dimension, original, reason))
                        continue
                    keys = list(dict.fromkeys(key for match in matches if match is not None for key in match[0]))
                    if len(keys) == 1:
                        item.values[offset] = keys[0]
                        for table, match in zip(tables or [], matches):
                            if match is None or not match[0]:
                                continue
                            for column in match[1]:
                                result.explanations.append(
                                    {
                                        "dimension": item.dimension,
                                        "original": original,
                                        "value": keys[0],
                                        "method": "dimension_table",
                                        "asset_id": table.asset.id,
                                        "table": f"{table.asset.catalog_name}.{table.asset.db_name}.{table.asset.name}",
                                        "column": column,
                                    }
                                )
                        continue
                    candidates = list(dict.fromkeys([*keys, *normalized]))
                    if not keys:
                        candidates = list(dict.fromkeys([*candidates, *_candidates(original, sample)]))
                    result.unresolved.append(
                        _unresolved(
                            item.dimension,
                            original,
                            candidates[:_CANDIDATE_LIMIT],
                            bool(tables),
                        )
                    )
                    sources = {}
                    for table, match in zip(tables or [], matches):
                        for key in match[0] if match else []:
                            sources.setdefault(key, []).append(table.asset.id)
                    if sources:
                        result.unresolved[-1]["candidate_sources"] = sources
                    continue
            item.values[offset] = value
            result.explanations.append(
                {"dimension": item.dimension, "original": original, "value": value, "method": method}
            )
    return result
