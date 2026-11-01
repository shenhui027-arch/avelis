"""在批次内按语句顺序折叠中间表，保留组成结果的原始边。"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from avelis.importer.statements import Write

_VALUES = {"value_direct": 0, "value_computed": 1, "value_aggregated": 2}
_INFLUENCES = {"filter", "join_key", "group_by"}


@dataclass
class FoldStatement:
    job_id: str
    statement_id: str
    writes: list[Write] = field(default_factory=list)
    dropped_refs: list[str] = field(default_factory=list)
    removed_refs: list[str] = field(default_factory=list)
    reset_refs: list[str] = field(default_factory=list)


@dataclass
class FoldEdge:
    id: str
    job_id: str
    statement_id: str
    source_ref: str
    source_column: str | None
    target_ref: str
    target_column: str | None
    relation: str
    expression: str | None = None
    attributes: dict[str, Any] = field(default_factory=dict)
    status: str = "active"


@dataclass
class FoldResult:
    active: list[FoldEdge]
    intermediate: list[FoldEdge]
    intermediate_tables: list[str]

    @property
    def edges(self) -> list[FoldEdge]:
        return [*self.intermediate, *self.active]


@dataclass(eq=False)
class _Table:
    ref: str
    created: bool = False
    intermediate: bool = False
    temporary: bool = False
    previous_data: _Table | None = None


@dataclass
class _Segment:
    edge: FoldEdge
    source: _Table
    target: _Table
    position: int


def _id(*parts: str) -> str:
    return uuid5(NAMESPACE_URL, "avelis:import:" + ":".join(parts)).hex


def _compose(upstream: FoldEdge, downstream: FoldEdge) -> tuple[str, str | None, str | None]:
    if "table" in (upstream.relation, downstream.relation):
        return "table", None, None
    if upstream.relation in _INFLUENCES:
        return upstream.relation, upstream.source_column, None
    if downstream.relation in _INFLUENCES:
        return downstream.relation, upstream.source_column, None
    relation = max((upstream.relation, downstream.relation), key=lambda value: _VALUES.get(value, 0))
    return relation, upstream.source_column, downstream.target_column


def fold_writes(statements: list[FoldStatement]) -> FoldResult:
    """折叠有序语句的写入；不修改输入，也不查询数据库。

    同一个表名在 DROP、RENAME 或 SWAP 前后有独立的生命周期，避免把换名后的最终表也当成中间表。
    清空数据会分离来源实例，但保留结构生命周期，之后的 DROP 仍按原规则判定各实例的中间边。
    原始边的 ID 由语句与位置确定，调用方可直接用它插入 ORM，hops 无需另做数据库查询。
    同一条下游语句的重复边保留首条，并在 paths 中保留所有组成路径。
    """
    current: dict[str, _Table] = {}
    tables: list[_Table] = []
    incoming: dict[_Table, list[_Segment]] = {}
    written_columns: dict[_Table, list[tuple[int, set[str] | None]]] = {}
    segments: list[_Segment] = []

    def table(ref: str, *, created: bool = False, temporary: bool = False, fresh: bool = False) -> _Table:
        if fresh or ref not in current:
            current[ref] = _Table(ref, created or temporary, temporary, temporary)
            tables.append(current[ref])
        state = current[ref]
        state.created = state.created or created or temporary
        state.intermediate = state.intermediate or temporary
        state.temporary = state.temporary or temporary
        return state

    def mark_intermediate(state: _Table | None, *, created_only: bool = False) -> None:
        while state is not None:
            if not created_only or state.created:
                state.intermediate = True
            state = state.previous_data

    for position, statement in enumerate(statements):
        raw_writes: list[tuple[Write, list[FoldEdge]]] = []
        for write_index, write in enumerate(statement.writes):
            raw_edges: list[FoldEdge] = []
            for edge_index, edge in enumerate(write.edges):
                raw = FoldEdge(
                    id=_id(statement.statement_id, str(write_index), str(edge_index)),
                    job_id=statement.job_id, statement_id=statement.statement_id,
                    source_ref=edge.source_ref, source_column=edge.source_column,
                    target_ref=write.target_ref, target_column=edge.target_column,
                    relation=edge.relation, expression=edge.expression,
                    attributes={**(write.attributes or {}), **(edge.attributes or {})},
                )
                raw_edges.append(raw)
            raw_writes.append((write, raw_edges))
        # 多路写入和 SWAP 的来源都取语句执行前的状态。
        for _, raw_edges in raw_writes:
            for raw in raw_edges:
                table(raw.source_ref)
        before = dict(current)
        # 覆盖写入读取旧快照，但本次写入必须落入重置后的数据实例。
        for ref in statement.reset_refs:
            previous = table(ref)
            current[ref] = _Table(ref, previous.created, previous.intermediate, previous.temporary, previous)
            tables.append(current[ref])
        targets: list[dict[str, _Table]] = []
        for write, raw_edges in raw_writes:
            op = (write.attributes or {}).get("op")
            write_targets: dict[str, _Table] = {}
            refs = dict.fromkeys(raw.target_ref for raw in raw_edges) or {write.target_ref: None}
            for ref in refs:
                first = next((raw for raw in raw_edges if raw.target_ref == ref), None)
                previous = before.get(first.source_ref) if op in ("rename", "swap") and first else None
                # 改名 / 换表得到的新名字是本批产生的：之后被 DROP 时同样是中间表
                created = write.created or op in ("rename", "swap") or bool(previous and previous.created)
                temporary = write.temporary or bool(previous and previous.temporary)
                write_targets[ref] = table(ref, created=created, temporary=temporary,
                                           fresh=write.created or op in ("rename", "swap"))
                columns = None if any(raw.relation == "table" for raw in raw_edges) else set(write.target_columns)
                written_columns.setdefault(write_targets[ref], []).append((position, columns))
            targets.append(write_targets)
        for (_, raw_edges), write_targets in zip(raw_writes, targets):
            for raw in raw_edges:
                target = write_targets[raw.target_ref]
                segment = _Segment(raw, before[raw.source_ref], target, position)
                segments.append(segment)
                incoming.setdefault(target, []).append(segment)
        for ref in statement.removed_refs:
            previous = before.get(ref)
            if previous is not None:
                mark_intermediate(previous)
                if current.get(ref) is previous:
                    current.pop(ref, None)
        for ref in statement.dropped_refs:
            previous = current.pop(ref, None)
            mark_intermediate(previous, created_only=True)

    expanded_by_id: dict[str, list[FoldEdge]] = {}
    for segment in segments:
        raw = segment.edge
        if not segment.source.intermediate:
            expanded_by_id[raw.id] = [replace(raw, attributes={**raw.attributes, "hops": [raw.id], "via": []})]
            continue
        expanded: list[FoldEdge] = []
        if not segment.source.created and segment.source.previous_data is None and not any(
            position < segment.position and (columns is None or raw.source_column in columns)
            for position, columns in written_columns.get(segment.source, [])
        ):
            # 外部表未被此前写入覆盖的列仍以原表为来源；影响边不会覆盖数据列。
            expanded.append(replace(
                raw, id=_id("folded", raw.id),
                attributes={**raw.attributes, "hops": [raw.id], "via": [segment.source.ref]},
            ))
        for prior in incoming.get(segment.source, []):
            if prior.position >= segment.position:
                continue
            previous = prior.edge
            if (raw.source_column is not None and previous.target_column is not None
                    and raw.source_column != previous.target_column):
                continue
            for source in expanded_by_id[previous.id]:
                relation, source_column, target_column = _compose(source, raw)
                hops = [*source.attributes["hops"], raw.id]
                via = [*source.attributes["via"], segment.source.ref]
                expanded.append(replace(
                    raw, id=_id("folded", *hops), source_ref=source.source_ref,
                    source_column=source_column, target_column=target_column, relation=relation,
                    attributes={**raw.attributes, "via": via, "hops": hops},
                ))
        expanded_by_id[raw.id] = expanded

    active: list[FoldEdge] = []
    intermediate: list[FoldEdge] = []
    intermediate_ids: set[str] = set()
    emitted: dict[tuple[str, str, str | None, str, str | None, str], FoldEdge] = {}
    for segment in segments:
        raw = segment.edge
        if segment.source.intermediate or segment.target.intermediate:
            intermediate.append(replace(raw, status="intermediate"))
            intermediate_ids.add(raw.id)
        if segment.target.intermediate:
            continue
        for edge in expanded_by_id[raw.id] if segment.source.intermediate else [raw]:
            key = (edge.statement_id, edge.source_ref, edge.source_column,
                   edge.target_ref, edge.target_column, edge.relation)
            hops = list(edge.attributes.get("hops", [edge.id]))
            if key in emitted:
                emitted[key].attributes["paths"].append(hops)
                if not segment.source.intermediate and raw.id not in intermediate_ids:
                    intermediate.append(replace(raw, status="intermediate"))
                    intermediate_ids.add(raw.id)
            else:
                edge = replace(edge, attributes={**edge.attributes, "paths": [hops]})
                active.append(edge)
                emitted[key] = edge
    return FoldResult(active, intermediate, sorted({state.ref for state in tables if state.intermediate}))
