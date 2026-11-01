"""有界的元数据图发现（P1 R）。只读元数据库与已有的维度取值缓存，不读取源库数据。

跨数据源 JOIN 不受支持（ADR 0011），键名或共同取值线索只在同一数据源内帮助定位参考表。
"""

from __future__ import annotations

import json
import re
from typing import Literal

from pydantic import BaseModel, Field
from sqlalchemy import or_, select
from sqlalchemy.orm import Session
from sqlglot import exp, parse_one
from sqlglot.errors import ParseError

from avelis import cache
from avelis.ask import service
from avelis.governance import access
from avelis.models import Asset, AssetVersion, ColumnVersion, JoinPath, LineageEdge, Metric, MetricVersion, User

Relation = Literal["lineage", "metric_column", "join_key", "shared_key", "shared_value"]
Direction = Literal["both", "in", "out"]
CLUE_NOTE = "只是发现线索，不是批准的关联，跨表比较走探索查询校验或分别查询后合并"
PAGE_SIZE = 100
LAYER_EDGES = 180
NODE_LIMIT = 30


class FindArgs(BaseModel):
    keyword: str = Field(min_length=1, description="口径、资产或列的名称、注释中的文字")


class NeighborsArgs(BaseModel):
    node: str = Field(description="find 返回的节点编号")
    relation: Relation | None = None
    direction: Direction = "both"
    depth: int = Field(1, ge=1, le=3)


class PathArgs(BaseModel):
    start: str
    end: str
    max_depth: int = Field(3, ge=1, le=3)


def qualified(asset: Asset) -> str:
    return f"{asset.catalog_name}.{asset.db_name}.{asset.name}"


def normalized_type(value: str) -> str:
    """只消除大小写、空白与类型长度，不猜测不同类型之间的兼容性。"""
    return re.sub(r"\s+", "", re.sub(r"\([^)]*\)", "", value or "")).lower()


def _edge(kind: str, source: str, target: str, columns: list[dict]) -> dict:
    return {
        "type": kind,
        "from": source,
        "to": target,
        "columns": columns,
        "clue": kind in {"shared_key", "shared_value"},
    }


def _edge_key(edge: dict) -> tuple:
    return (edge["type"], edge["from"], edge["to"], tuple((c["from"] or "", c["to"] or "") for c in edge["columns"]))


def _column_name(value: str | None) -> str | None:
    if not value:
        return None
    parts = value.split(".")
    return parts[-1] if len(parts) == 1 or parts[-2] == "b" else None


class MetadataGraph:
    """每次工具调用新建；口径可见性复用工具箱的来源检查，不保留跨调用的授权快照。"""

    def __init__(self, session: Session, user: User, allowed: set[str], *, metric_sources):
        self.session, self.user, self.allowed = session, user, allowed
        self.metric_sources = metric_sources

    def _rows(self, statement):
        """按页读取候选，匹配数再多也不把整个目录放进内存。"""
        offset = 0
        while True:
            rows = self.session.execute(statement.limit(PAGE_SIZE).offset(offset)).all()
            yield from rows
            if len(rows) < PAGE_SIZE:
                return
            offset += PAGE_SIZE

    def _readable(self, asset: Asset | None) -> bool:
        return bool(
            asset is not None
            and asset.status == "active"
            and asset.kind != "catalog_table"
            and access.can_read_asset(self.session, self.user, asset, self.allowed)
        )

    def _asset(self, asset_id: str | None) -> Asset | None:
        asset = self.session.get(Asset, asset_id) if asset_id else None
        return asset if self._readable(asset) else None

    def _version(self, asset: Asset) -> AssetVersion | None:
        return self.session.get(AssetVersion, asset.current_version_id) if asset.current_version_id else None

    def _columns(self, asset: Asset):
        if asset.current_version_id:
            for row in self._rows(
                select(ColumnVersion)
                .where(ColumnVersion.asset_version_id == asset.current_version_id)
                .order_by(ColumnVersion.name)
            ):
                yield row[0]

    def _metric(self, code: str):
        try:
            metric, version, _, _ = self.metric_sources(code)
        except Exception:  # noqa: BLE001 - 不可见与不存在使用同样的空结果，不带出对象信息
            return None
        if metric is None or version is None or version.status != "published":
            return None
        return metric, version

    def _asset_node(self, asset: Asset, column: ColumnVersion | None = None) -> dict:
        name = qualified(asset)
        version = self._version(asset) if column is None else None
        return {
            "id": f"column:{name}.{column.name}" if column is not None else f"asset:{name}",
            "kind": "column" if column is not None else "asset",
            "qualified": name,
            "name": column.name if column is not None else name,
            "comment": column.comment if column is not None else (version.comment if version else None),
        }

    @staticmethod
    def _metric_node(metric: Metric, version: MetricVersion) -> dict:
        return {
            "id": f"metric:{metric.code}",
            "kind": "metric",
            "name": metric.name,
            "code": metric.code,
            "comment": version.description,
        }

    def _resolve(self, node: str) -> dict | None:
        kind, sep, name = node.partition(":")
        if not sep:
            return None
        if kind == "metric":
            found = self._metric(name)
            return self._metric_node(*found) if found else None
        parts = name.split(".")
        if kind not in {"asset", "column"} or len(parts) != (4 if kind == "column" else 3):
            return None
        for (asset,) in self._rows(
            select(Asset)
            .where(
                Asset.catalog_name == parts[0],
                Asset.db_name == parts[1],
                Asset.name == parts[2],
                Asset.status == "active",
                Asset.kind != "catalog_table",
            )
            .order_by(Asset.id)
        ):
            if not self._readable(asset):
                continue
            if kind == "asset":
                return self._asset_node(asset)
            for (column,) in self._rows(
                select(ColumnVersion)
                .where(ColumnVersion.asset_version_id == asset.current_version_id, ColumnVersion.name == parts[3])
                .order_by(ColumnVersion.id)
            ):
                return self._asset_node(asset, column)
        return None

    def find(self, keyword: str) -> dict:
        """只保留前 20 个命中，继续按页计数；无权对象不参与排序和省略数。"""
        FindArgs(keyword=keyword)
        chosen: list[tuple] = []
        count = 0
        needle = keyword.casefold()

        def add(node, names):
            nonlocal count
            name_hit = any(needle in (name or "").casefold() for name in names)
            if not name_hit and needle not in (node["comment"] or "").casefold():
                return
            count += 1
            chosen.append((not name_hit, node.get("qualified") or node.get("code"), node["id"], node))
            chosen.sort(key=lambda item: item[:3])
            del chosen[20:]

        def match(column):
            return column.icontains(keyword, autoescape=True)

        for (metric,) in self._rows(
            select(Metric)
            .join(MetricVersion, MetricVersion.metric_id == Metric.id)
            .where(
                Metric.domain_id.in_(self.allowed),
                MetricVersion.status == "published",
                or_(match(Metric.code), match(Metric.name), match(MetricVersion.description)),
            )
            .distinct()
            .order_by(Metric.code)
        ):
            if found := self._metric(metric.code):
                add(self._metric_node(*found), [found[0].code, found[0].name])
        full_name = Asset.catalog_name + "." + Asset.db_name + "." + Asset.name
        for (asset,) in self._rows(
            select(Asset)
            .outerjoin(AssetVersion, AssetVersion.id == Asset.current_version_id)
            .where(
                Asset.status == "active",
                Asset.kind != "catalog_table",
                or_(match(full_name), match(AssetVersion.comment)),
            )
            .order_by(Asset.catalog_name, Asset.db_name, Asset.name, Asset.id)
        ):
            if self._readable(asset):
                add(self._asset_node(asset), [qualified(asset)])
        for asset, column in self._rows(
            select(Asset, ColumnVersion)
            .join(ColumnVersion, ColumnVersion.asset_version_id == Asset.current_version_id)
            .where(
                Asset.status == "active",
                Asset.kind != "catalog_table",
                or_(match(ColumnVersion.name), match(ColumnVersion.comment)),
            )
            .order_by(Asset.catalog_name, Asset.db_name, Asset.name, ColumnVersion.name, Asset.id)
        ):
            if self._readable(asset):
                add(self._asset_node(asset, column), [column.name])
        return {"nodes": [item[3] for item in chosen], "truncated": count > 20, "omitted": max(0, count - 20)}

    @staticmethod
    def _pairs(kind, left, right, columns):
        """表级边保留连接列；列级边保留精确端点，二者使用同一种关系类型。"""
        source, target = qualified(left), qualified(right)
        yield _edge(kind, f"asset:{source}", f"asset:{target}", columns)
        for pair in columns:
            if pair["from"] and pair["to"]:
                yield _edge(kind, f"column:{source}.{pair['from']}", f"column:{target}.{pair['to']}", [pair])

    def _lineage_edges(self, assets: set[str], relation):
        if not assets:
            return
        statement = select(LineageEdge).where(
            LineageEdge.status == "active",
            or_(LineageEdge.source_asset_id.in_(assets), LineageEdge.target_asset_id.in_(assets)),
        )
        if relation == "join_key":
            statement = statement.where(LineageEdge.relation == "join_key")
        for (edge,) in self._rows(statement.order_by(LineageEdge.id)):
            left, right = self._asset(edge.source_asset_id), self._asset(edge.target_asset_id)
            if left is None or right is None:
                continue
            columns = [{"from": edge.source_column, "to": edge.target_column}]
            if relation in (None, "lineage"):
                yield from self._pairs("lineage", left, right, columns)
            if edge.relation == "join_key" and relation in (None, "join_key"):
                yield from self._pairs("join_key", left, right, columns)
        if relation in (None, "join_key"):
            for (path,) in self._rows(
                select(JoinPath)
                .where(
                    JoinPath.status == "approved",
                    or_(JoinPath.left_asset_id.in_(assets), JoinPath.right_asset_id.in_(assets)),
                )
                .order_by(JoinPath.id)
            ):
                left, right = self._asset(path.left_asset_id), self._asset(path.right_asset_id)
                if left is not None and right is not None:
                    pairs = [{"from": a, "to": b} for a, b in zip(path.left_columns, path.right_columns)]
                    yield from self._pairs("join_key", left, right, pairs)

    def _metric_edges(self, assets: set[str], metric_codes: set[str]):
        for (metric,) in self._rows(
            select(Metric)
            .join(MetricVersion, MetricVersion.metric_id == Metric.id)
            .where(
                Metric.domain_id.in_(self.allowed),
                MetricVersion.status == "published",
                or_(MetricVersion.base_asset_id.in_(assets), Metric.code.in_(metric_codes)),
            )
            .distinct()
            .order_by(Metric.code)
        ):
            found = self._metric(metric.code)
            if found is None:
                continue
            metric, version = found
            base = self._asset(version.base_asset_id)
            if base is None:
                continue
            used = {
                _column_name(version.time_column),
                *(_column_name(d.get("column")) for d in version.dimensions or []),
            }
            try:
                expression = parse_one(version.expression, read="postgres")
                used.update(c.name for c in expression.find_all(exp.Column) if not c.table or c.table == "b")
            except ParseError:
                pass  # 已发布表达式的元数据无法解析时，仍保留明确登记的时间列与维度列
            columns = [{"from": c.name, "to": c.name} for c in self._columns(base) if c.name in used]
            source = f"metric:{metric.code}"
            if columns:
                yield _edge("metric_column", source, f"asset:{qualified(base)}", columns)
            for pair in columns:
                yield _edge("metric_column", source, f"column:{qualified(base)}.{pair['to']}", [pair])

    def _is_key(self, asset: Asset, column: ColumnVersion) -> bool:
        version = self._version(asset)
        keys = (version.key_columns or []) if version else []
        unique = (version.unique_keys or []) if version else []
        return bool(
            column.is_key
            or column.name in keys
            or any(column.name in (key if isinstance(key, list) else [key]) for key in unique)
        )

    def _shared_key_edges(self, assets: set[str]):
        for asset_id in sorted(assets):
            left = self._asset(asset_id)
            if left is None:
                continue
            for column in self._columns(left):
                # 先用精确列名缩小候选，再检查类型和键；不枚举任意两张表的列组合。
                for right, other in self._rows(
                    select(Asset, ColumnVersion)
                    .join(ColumnVersion, ColumnVersion.asset_version_id == Asset.current_version_id)
                    .where(
                        Asset.status == "active",
                        Asset.kind != "catalog_table",
                        Asset.id != left.id,
                        Asset.datasource_id == left.datasource_id,
                        ColumnVersion.name == column.name,
                    )
                    .order_by(Asset.id, ColumnVersion.id)
                ):
                    if not self._readable(right) or (right.id in assets and right.id < left.id):
                        continue
                    if (
                        not normalized_type(column.data_type)
                        or normalized_type(column.data_type) != normalized_type(other.data_type)
                        or not (self._is_key(left, column) or self._is_key(right, other))
                    ):
                        continue
                    a, b = sorted((left, right), key=qualified)
                    yield from self._pairs("shared_key", a, b, [{"from": column.name, "to": column.name}])

    def _cached_columns(self, asset_ids: set[str] | None = None):
        """只枚举已有缓存键；先核对资产权限与版本，再读取取值。缓存缺失、不可用都不回源。"""
        try:
            client = cache.client()
            patterns = (
                [f"avelis:dimvals:{i}:*" for i in sorted(asset_ids)] if asset_ids is not None else ["avelis:dimvals:*"]
            )
            for pattern in patterns:
                for key in client.scan_iter(match=pattern, count=PAGE_SIZE):
                    key = key.decode() if isinstance(key, bytes) else key
                    parts = key.split(":", 4)
                    if len(parts) != 5:
                        continue
                    _, _, asset_id, version_id, name = parts
                    asset = self._asset(asset_id)
                    if asset is None or asset.current_version_id != version_id:
                        continue
                    for (column,) in self._rows(
                        select(ColumnVersion)
                        .where(ColumnVersion.asset_version_id == version_id, ColumnVersion.name == name)
                        .order_by(ColumnVersion.id)
                    ):
                        raw = client.get(key)
                        raw = raw.decode() if isinstance(raw, bytes) else raw
                        if raw:
                            yield asset, column, set(raw.split("\x1f")) - {""}
        except Exception:  # noqa: BLE001 - 缓存不是事实来源，失败只表示没有这类线索
            return

    def _shared_value_edges(self, assets: set[str]):
        # 每次只保留一个起点列的取值，与按页枚举的已有缓存比较，避免载入全目录取值。
        for left, column, values in self._cached_columns(assets):
            for right, other, candidates in self._cached_columns():
                if left.datasource_id != right.datasource_id:
                    continue
                if left.id == right.id or (
                    right.id in assets and (qualified(right), other.name) < (qualified(left), column.name)
                ):
                    continue
                if not values.isdisjoint(candidates):
                    if qualified(left) < qualified(right):
                        a, b, x, y = left, right, column.name, other.name
                    else:
                        a, b, x, y = right, left, other.name, column.name
                    yield from self._pairs("shared_value", a, b, [{"from": x, "to": y}])

    def _candidate_edges(self, frontier: set[str], relation):
        assets, metrics = set(), set()
        for node in sorted(frontier):
            resolved = self._resolve(node)
            if resolved is None:
                continue
            if resolved["kind"] == "metric":
                metrics.add(resolved["code"])
            else:
                parts = resolved["qualified"].split(".")
                for (asset,) in self._rows(
                    select(Asset)
                    .where(Asset.catalog_name == parts[0], Asset.db_name == parts[1], Asset.name == parts[2])
                    .order_by(Asset.id)
                ):
                    if self._readable(asset):
                        assets.add(asset.id)
        if relation in (None, "lineage", "join_key"):
            yield from self._lineage_edges(assets, relation)
        if relation in (None, "metric_column"):
            yield from self._metric_edges(assets, metrics)
        if relation in (None, "shared_key"):
            yield from self._shared_key_edges(assets)
        if relation in (None, "shared_value"):
            yield from self._shared_value_edges(assets)

    def _expand(self, frontier: set[str], relation, direction, *, target: str | None = None) -> tuple[list[dict], int]:
        chosen: dict[tuple, dict] = {}
        omitted = 0
        for edge in self._candidate_edges(frontier, relation):
            if relation is not None and edge["type"] != relation:
                continue
            if not (
                (direction in ("both", "out") and edge["from"] in frontier)
                or (direction in ("both", "in") and edge["to"] in frontier)
            ):
                continue
            if self._resolve(edge["from"]) is None or self._resolve(edge["to"]) is None:
                continue
            key = _edge_key(edge)
            if key in chosen:
                continue
            chosen[key] = edge
            if len(chosen) > LAYER_EDGES:
                worst = max(chosen, key=lambda k: (target not in (chosen[k]["from"], chosen[k]["to"]), k))
                del chosen[worst]
                omitted += 1
        return [chosen[key] for key in sorted(chosen)], omitted

    def neighbors(
        self, node: str, relation: Relation | None = None, direction: Direction = "both", depth: int = 1
    ) -> dict:
        NeighborsArgs(node=node, relation=relation, direction=direction, depth=depth)
        root = self._resolve(node)
        if root is None:
            return {"nodes": [], "edges": [], "truncated": False, "omitted": 0, "note": CLUE_NOTE}
        nodes, edges, frontier = {node: root}, {}, {node}
        omitted = 0
        for _ in range(depth):
            layer, skipped = self._expand(frontier, relation, direction)
            omitted += skipped
            candidates = sorted({e[k] for e in layer for k in ("from", "to")} - nodes.keys())
            remaining = max(0, NODE_LIMIT - len(nodes))
            accepted = candidates[:remaining]
            omitted += max(0, len(candidates) - remaining)
            frontier = set()
            for item in accepted:
                if resolved := self._resolve(item):
                    nodes[item] = resolved
                    frontier.add(item)
            for edge in layer:
                if edge["from"] in nodes and edge["to"] in nodes:
                    edges[_edge_key(edge)] = edge
            if not frontier:
                break
        return {
            "nodes": list(nodes.values()),
            "edges": [edges[k] for k in sorted(edges)],
            "truncated": omitted > 0,
            "omitted": omitted,
            "note": CLUE_NOTE,
        }

    def path(self, start: str, end: str, max_depth: int = 3) -> dict:
        """按层寻找最短路径；每层最多展开 30 个节点、180 条可见边，不穿过无权节点。"""
        PathArgs(start=start, end=end, max_depth=max_depth)
        empty = {"paths": [], "truncated": False, "omitted": 0, "note": CLUE_NOTE}
        if self._resolve(start) is None or self._resolve(end) is None:
            return empty
        if start == end:
            return {**empty, "paths": [{"nodes": [start], "edges": []}]}
        frontier, distance, adjacency = {start}, {start: 0}, {}
        omitted = 0
        first_layer_clipped = False
        for level in range(1, max_depth + 1):
            layer, skipped = self._expand(frontier, None, "both", target=end)
            omitted += skipped
            candidates = set()
            for edge in layer:
                for a, b in ((edge["from"], edge["to"]), (edge["to"], edge["from"])):
                    if a in frontier and b not in distance:
                        candidates.add(b)
            picked = sorted(candidates)[:NODE_LIMIT]
            # 终点不因字母顺序被层上限挤掉；仍然保留截断标记。
            if end in candidates and end not in picked:
                picked[-1] = end
            omitted += max(0, len(candidates) - len(picked))
            if level == 1:
                first_layer_clipped = bool(skipped or len(candidates) > len(picked))
            for item in picked:
                distance[item] = level
            for edge in layer:
                for a, b in ((edge["from"], edge["to"]), (edge["to"], edge["from"])):
                    if a in frontier and distance.get(b) == level:
                        adjacency.setdefault(a, []).append((b, edge))
            frontier = set(picked)
            if end in distance or not frontier:
                break
        graph_omitted = omitted
        # 直连终点边总被优先保留，因此一、二跳结果仍可确认最短；首层漏掉的分支却可能藏有二跳路径。
        # 此时不把已找到的三跳候选当作最短路径返回。
        if distance.get(end) == 3 and first_layer_clipped:
            return {
                **empty,
                "truncated": True,
                "omitted": omitted,
                "note": CLUE_NOTE + "；展开已截断，尚不能确认最短路径，请缩小节点范围",
            }
        # 动态计数后只构造前三条，避免多条平行边使路径队列成倍增长。
        counts = {end: 1}
        for node in sorted(distance, key=lambda n: (-distance[n], n)):
            if node != end:
                counts[node] = sum(counts.get(other, 0) for other, _ in adjacency.get(node, []))
        paths = []

        def walk(node, walked, edges):
            if len(paths) == 3:
                return
            if node == end:
                paths.append({"nodes": walked, "edges": edges})
                return
            for other, edge in sorted(adjacency.get(node, []), key=lambda pair: (pair[0], _edge_key(pair[1]))):
                if counts.get(other, 0):
                    walk(other, [*walked, other], [*edges, edge])

        walk(start, [start], [])
        omitted += max(0, counts.get(start, 0) - 3)
        return {
            "paths": paths,
            "truncated": omitted > 0,
            "omitted": omitted,
            "note": CLUE_NOTE + ("；展开已截断，路径可能不完整" if graph_omitted else ""),
        }


def reference_tables(
    graph: MetadataGraph, versions: list[MetricVersion], closure: dict[str, Asset], k: int = 12
) -> list:
    """只展开来源闭包的直接邻居，优先补充不同来源且能对齐时间窗口的表。"""
    k = min(12, max(0, k))
    if not k:
        return []
    base_ids = {v.base_asset_id for v in versions if v.base_asset_id}
    sources = {f"asset:{qualified(a)}": a for a in closure.values() if graph._readable(a)}
    upstream: dict[str, set[str]] = {}

    def upstream_ids(asset: Asset) -> set[str]:
        if asset.id not in upstream:
            upstream[asset.id] = {
                a.id
                for a in service.upstream_closure(graph.session, [asset.id]).values()
                if access.can_read_asset(graph.session, graph.user, a, graph.allowed)
            }
        return upstream[asset.id]

    roots = {
        a.id
        for a in closure.values()
        if access.can_read_asset(graph.session, graph.user, a, graph.allowed) and not upstream_ids(a) - {a.id}
    }
    candidates: dict[str, dict] = {}
    # 来源闭包本身可能较大，也分成至多 30 个起点的一批；不继续展开发现的邻居。
    ordered = sorted(sources)
    for offset in range(0, len(ordered), NODE_LIMIT):
        batch = set(ordered[offset : offset + NODE_LIMIT])
        for relation in ("join_key", "shared_key", "shared_value"):
            edges, _ = graph._expand(batch, relation, "both")
            for edge in edges:
                for here, there in ((edge["from"], edge["to"]), (edge["to"], edge["from"])):
                    if here not in batch or not there.startswith("asset:"):
                        continue
                    if (
                        there not in candidates
                        and there not in sources
                        and sum(n not in sources for n in candidates) >= LAYER_EDGES
                    ):
                        continue
                    entry = candidates.setdefault(
                        there, {"distance": 0 if there in sources else 1, "reasons": [], "connected": set()}
                    )
                    side = "to" if there == edge["to"] else "from"
                    entry["connected"].update(c[side] for c in edge["columns"] if c[side])
                    reason = {key: edge[key] for key in ("type", "columns", "clue")}
                    if reason not in entry["reasons"]:
                        entry["reasons"].append(reason)
    for node in sources:
        candidates.setdefault(node, {"distance": 0, "reasons": [], "connected": set()})
    ranked = []
    for node, entry in candidates.items():
        resolved = graph._resolve(node)
        if resolved is None:
            continue
        name = resolved["qualified"]
        asset = sources.get(node)
        if asset is None:
            parts = name.split(".")
            asset = next(
                (
                    a
                    for (a,) in graph._rows(
                        select(Asset)
                        .where(Asset.catalog_name == parts[0], Asset.db_name == parts[1], Asset.name == parts[2])
                        .order_by(Asset.id)
                    )
                    if graph._readable(a)
                ),
                None,
            )
        if asset is None or asset.id in base_ids:
            continue
        reasons = sorted(entry["reasons"], key=lambda r: (r["type"], str(r["columns"])))
        connected = entry["connected"]
        internal = node in sources
        if internal and not reasons:
            reasons = [{"type": "lineage", "columns": [], "clue": False}]
        sibling = bool(upstream_ids(asset) & roots)
        date_columns = sorted(
            c.name
            for c in graph._columns(asset)
            if normalized_type(c.data_type)
            in {
                "date",
                "datetime",
                "timestamp",
                "timestampwithtimezone",
                "timestampwithouttimezone",
            }
        )[:3]
        rank = (internal, sibling, not date_columns, entry["distance"], -len(connected), name)
        ranked.append(
            (
                rank,
                {
                    "table": name,
                    "comment": resolved["comment"],
                    "via": reasons,
                    "date_columns": date_columns,
                    "sibling": sibling,
                },
            )
        )
        ranked.sort(key=lambda item: item[0])
        del ranked[k:]
    return [table for _, table in ranked]


def reference_block(refs: list[dict], limit: int = 2500) -> str:
    """参考表独立文字上限包含标题、线索说明和省略数；按整张表截断。"""
    limit = min(2500, max(0, limit))
    if not refs or not limit:
        return ""
    head = "参考表（元数据图线索）\n" + CLUE_NOTE + "。仅按有界展开内的可见表排序，只给元数据定位，不查数据、不下结论。"
    lines = [head]
    for index, table in enumerate(refs):
        text = json.dumps(table, ensure_ascii=False, default=str, separators=(",", ":"))
        marker = f"（省略 {len(refs) - index - 1} 张参考表）" if index + 1 < len(refs) else ""
        if len("\n".join([*lines, text])) + (len(marker) + 1 if marker else 0) > limit:
            marker = f"（省略 {len(refs) - index} 张参考表）"
            block = "\n".join([*lines, marker])
            return block if len(block) <= limit else marker if len(marker) <= limit else ""
        lines.append(text)
    return "\n".join(lines)
