"""资产档案页的聚合（ADR 0022）。只读；每一项都按当前用户的权限裁剪。

入口是 build()；各段各一个函数，便于单测。字段血缘的说明读写在 avelis.governance.lineage_notes。"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta

import sqlglot
from sqlalchemy import or_, select
from sqlalchemy.orm import Session
from sqlglot import exp

from avelis.db import utcnow
from avelis.governance import access
from avelis.models import (
    AnalysisDataset,
    AnalysisRun,
    Answer,
    Asset,
    AssetVersion,
    ColumnVersion,
    Datasource,
    Domain,
    ExtractedStatement,
    ImportJob,
    JoinPath,
    LineageEdge,
    Metric,
    MetricVersion,
    User,
)

ASKED_DAYS = 30
ASKED_PEOPLE = 8
MAX_SCRIPTS = 5

VALUE_RELATIONS = ("value_direct", "value_computed", "value_aggregated")
# 同一字段多条边的加工类型不一致时取最强的一种
_RELATION_RANK = {"value_direct": 0, "value_computed": 1, "value_aggregated": 2}
_ORIGIN_RANK = {"parsed": 0, "manual": 1, "model_confirmed": 2, "import_manual": 3, "import": 4}
IMPORT_ORIGINS = ("import", "import_manual", "model_confirmed")
METRIC_STATUSES = ("published", "pending_review", "affected", "draft")
_ROLE_RANK = {"base": 0, "dependency": 1, "join": 2}
CLEAR_TYPES = ("TRUNCATE TABLE", "DELETE")


def _qualified(a: Asset) -> str:
    return f"{a.catalog_name}.{a.db_name}.{a.name}"


def _iso(value: datetime | None) -> str | None:
    return _aware(value).isoformat() if value is not None else None


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _person(user: User | None) -> dict | None:
    return {"id": user.id, "display_name": user.display_name} if user is not None else None


def _columns(session: Session, asset: Asset) -> list[ColumnVersion]:
    if not asset.current_version_id:
        return []
    return list(
        session.scalars(
            select(ColumnVersion)
            .where(ColumnVersion.asset_version_id == asset.current_version_id)
            .order_by(ColumnVersion.ordinal)
        )
    )


# --- 入口 -------------------------------------------------------------------------


def build(session: Session, user: User, asset: Asset) -> dict:
    """返回 schemas.AssetDossierOut 形状的 dict。调用方已确认 user 可读 asset。

    owner 取资产所在业务域的负责人（未归属业务域为空）。summary：当前版本有表注释时 source=comment；
    否则有整表说明（lineage_notes.notes_for 的空串键）时 source=note。fields 的 note 来自 notes_for。
    can_edit_notes = lineage_notes.can_edit；model_available = lineage_notes.model_available。"""
    from avelis.api.catalog import asset_summary
    from avelis.governance import lineage_notes

    version = session.get(AssetVersion, asset.current_version_id) if asset.current_version_id else None
    comment = ((version.comment or "").strip() or None) if version else None
    column_count = len(_columns(session, asset)) if version else 0

    domain = session.get(Domain, asset.domain_id) if asset.domain_id else None
    owner = session.get(User, domain.owner_id) if domain is not None else None

    fields = field_lineage(session, asset)
    notes = lineage_notes.notes_for(session, asset, {f["column"]: f["basis_hash"] for f in fields})

    # 字段来源里当前用户不可读的表只给写法
    allowed = access.domain_ids(session, user)
    source_ids = {s["asset_id"] for f in fields for s in f["sources"] if s.get("asset_id")}
    readable = _readable_ids(session, user, source_ids, allowed)
    for f in fields:
        f["sources"] = [
            {**s, "asset_id": s["asset_id"] if s.get("asset_id") in readable else None} for s in f["sources"]
        ]
        f["note"] = notes.get(f["column"])

    table_note = notes.get("")
    if comment:
        summary = {"text": comment, "source": "comment", "note": None}
    elif table_note is not None:
        summary = {"text": table_note["text"], "source": "note", "note": table_note}
    else:
        summary = {"text": None, "source": None, "note": None}

    can_edit = lineage_notes.can_edit(session, user, asset)
    upstream, downstream = neighbors(session, user, asset)
    return {
        "asset": asset_summary(session, asset),
        "comment": comment,
        "domain": {"id": domain.id, "name": domain.name} if domain is not None else None,
        "owner": _person(owner),
        "summary": summary,
        "column_count": column_count,
        "upstream": upstream,
        "downstream": downstream,
        "metrics": metrics_using(session, user, asset),
        "asked": asked(session, user, asset),
        "produced": produced(session, user, asset, show_sql=can_edit),
        "fields": fields,
        "can_edit_notes": can_edit,
        "model_available": lineage_notes.model_available(session),
    }


def _readable_ids(session: Session, user: User, ids: set[str], allowed: set[str]) -> set[str]:
    if not ids:
        return set()
    rows = session.scalars(select(Asset).where(Asset.id.in_(ids)))
    return {a.id for a in rows if access.can_read_asset(session, user, a, allowed)}


# --- 字段来历 ---------------------------------------------------------------------


def field_lineage(session: Session, asset: Asset) -> list[dict]:
    """本表每个字段的来历（schemas.DossierFieldOut，不含 note，另带 basis_hash 供说明比对）。

    取 target_asset_id = asset.id、status = active、relation 为 value_* 的列级边，按 target_column 归并：
    sources 去重；同一字段既有数据库定义解析（origin=parsed）又有导入的边时以 parsed 的表达式与关系为准。
    顺序按当前版本的字段顺序，没有任何来源的字段不出现。"""
    rows = session.execute(
        select(
            LineageEdge.target_column,
            LineageEdge.source_asset_id,
            LineageEdge.source_ref,
            LineageEdge.source_column,
            LineageEdge.relation,
            LineageEdge.expression,
            LineageEdge.origin,
        )
        .where(
            LineageEdge.target_asset_id == asset.id,
            LineageEdge.status == "active",
            LineageEdge.relation.in_(VALUE_RELATIONS),
            LineageEdge.target_column.is_not(None),
        )
        .order_by(LineageEdge.created_at, LineageEdge.id)
    ).all()
    exact: dict[str, list] = {}
    for row in rows:
        exact.setdefault(row.target_column, []).append(row)
    if not exact:
        return []

    columns = _columns(session, asset)
    if columns:
        # 边上的字段名与采集到的字段名大小写可能不同：先按原名对，对不上且不会混淆两个只差大小写的字段时才忽略大小写
        folded: dict[str, list[str]] = {}
        for c in columns:
            folded.setdefault(c.name.lower(), []).append(c.name)
        grouped: dict[str, list] = {}
        for written, edges in exact.items():
            names = folded.get(written.lower(), [])
            target = written if written in names else (names[0] if len(names) == 1 else None)
            if target is not None:
                grouped.setdefault(target, []).extend(edges)
        ordered = [(c.name, c.comment) for c in columns if c.name in grouped]
    else:
        # 没有采集到当前版本时按字段名排序
        grouped = exact
        ordered = sorted((name, None) for name in grouped)

    out = []
    for name, comment in ordered:
        edges = grouped[name]
        parsed = [e for e in edges if e.origin == "parsed"]
        chosen = parsed or edges
        relation = max((e.relation for e in chosen), key=lambda r: _RELATION_RANK.get(r, -1))
        expressions = [e.expression for e in chosen if (e.expression or "").strip()]
        expression = expressions[-1] if expressions else None
        origin = min((e.origin for e in edges), key=lambda o: _ORIGIN_RANK.get(o, len(_ORIGIN_RANK)))
        seen: dict[tuple[str, str | None], dict] = {}
        for e in edges:
            key = (e.source_ref, e.source_column)
            if key not in seen:
                seen[key] = {"asset_id": e.source_asset_id, "ref": e.source_ref, "column": e.source_column}
            elif seen[key]["asset_id"] is None and e.source_asset_id:
                seen[key]["asset_id"] = e.source_asset_id
        sources = sorted(seen.values(), key=lambda s: (s["ref"], s["column"] or ""))
        out.append(
            {
                "column": name,
                "comment": comment,
                "relation": relation,
                "sources": sources,
                "expression": expression,
                "origin": origin,
                "basis_hash": basis_hash(relation, expression, sources),
            }
        )
    return out


def basis_hash(relation: str | None, expression: str | None, sources: list[dict]) -> str:
    """字段来历的校验码：关系、表达式（去首尾空白）与排序后的来源（ref, column）的 sha256。
    lineage_notes 用它判断说明是否过期，两边必须用同一个函数。"""
    refs = sorted({(s.get("ref") or "", s.get("column") or "") for s in sources})
    payload = json.dumps([relation or "", (expression or "").strip(), refs], ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# --- 上下游 -----------------------------------------------------------------------


def neighbors(session: Session, user: User, asset: Asset) -> tuple[list[dict], list[dict]]:
    """直接上游与直接下游的表（schemas.DossierTableRefOut），各按 ref 排序。

    只看一跳、status = active 的边（含表级与影响类关系）；columns 是涉及的本表字段数（上游）或对方字段数（下游）。
    对方未采集（asset_id 为空）或当前用户不可读时，asset_id 置空、不给注释，只保留写法。"""
    up = session.execute(
        select(LineageEdge.source_asset_id, LineageEdge.source_ref, LineageEdge.target_column).where(
            LineageEdge.target_asset_id == asset.id, LineageEdge.status == "active"
        )
    ).all()
    down = session.execute(
        select(LineageEdge.target_asset_id, LineageEdge.target_ref, LineageEdge.target_column).where(
            LineageEdge.source_asset_id == asset.id, LineageEdge.status == "active"
        )
    ).all()
    # (对方资产, 写法, 计数用的字段)；自引用不算上下游
    up_items = [(a, ref, col) for a, ref, col in up if a != asset.id]
    down_items = [(a, ref, col) for a, ref, col in down if a != asset.id]

    ids = {a for a, _, _ in up_items + down_items if a}
    assets = {a.id: a for a in session.scalars(select(Asset).where(Asset.id.in_(ids)))} if ids else {}
    allowed = access.domain_ids(session, user)
    readable = {i for i, a in assets.items() if access.can_read_asset(session, user, a, allowed)}
    version_ids = {assets[i].current_version_id for i in readable if assets[i].current_version_id}
    versions = (
        dict(
            session.execute(select(AssetVersion.id, AssetVersion.comment).where(AssetVersion.id.in_(version_ids))).all()
        )
        if version_ids
        else {}
    )
    ds_ids = {assets[i].datasource_id for i in readable}
    kinds = (
        dict(session.execute(select(Datasource.id, Datasource.kind).where(Datasource.id.in_(ds_ids))).all())
        if ds_ids
        else {}
    )

    def collect(items) -> list[dict]:
        grouped: dict[str, dict] = {}
        for asset_id, ref, column in items:
            other = assets.get(asset_id) if asset_id else None
            written = _qualified(other) if other is not None else ref
            if not written:
                continue
            key = asset_id if other is not None else f"ref:{written}"
            entry = grouped.setdefault(key, {"asset": other, "ref": written, "columns": set()})
            if column:
                entry["columns"].add(column.lower())
        out = []
        for entry in grouped.values():
            other = entry["asset"]
            visible = other is not None and other.id in readable
            comment = versions.get(other.current_version_id) if visible and other.current_version_id else None
            out.append(
                {
                    "asset_id": other.id if visible else None,
                    "ref": entry["ref"],
                    "comment": (comment or "").strip() or None,
                    "datasource_kind": kinds.get(other.datasource_id) if visible else None,
                    "columns": len(entry["columns"]),
                }
            )
        return sorted(out, key=lambda x: x["ref"])

    return collect(up_items), collect(down_items)


# --- 口径 -------------------------------------------------------------------------


def metrics_using(session: Session, user: User, asset: Asset) -> list[dict]:
    """用到这张表的指标的当前口径（schemas.DossierMetricOut）。

    每个指标取一个版本：有 published 用 published，否则取最新的 pending_review / affected / draft；
    retired、archived、rejected 不算。role：base_asset_id 命中为 base，dependency[*].asset_id 命中为 dependency，
    经 joins[*].join_path_id 的关联路径任一侧命中为 join（取最强的一种，base > dependency > join）。
    只返回当前用户可见业务域的指标。"""
    allowed = access.domain_ids(session, user)
    if not allowed:
        return []
    chosen: dict[str, tuple[int, int, str]] = {}
    for version_id, metric_id, version_no, status in session.execute(
        select(MetricVersion.id, MetricVersion.metric_id, MetricVersion.version_no, MetricVersion.status)
        .join(Metric, Metric.id == MetricVersion.metric_id)
        .where(Metric.domain_id.in_(allowed), MetricVersion.status.in_(METRIC_STATUSES))
    ):
        rank = (1 if status == "published" else 0, version_no, version_id)
        if metric_id not in chosen or rank > chosen[metric_id]:
            chosen[metric_id] = rank
    if not chosen:
        return []
    paths = set(
        session.scalars(
            select(JoinPath.id).where(or_(JoinPath.left_asset_id == asset.id, JoinPath.right_asset_id == asset.id))
        )
    )
    out = []
    for version, metric in session.execute(
        select(MetricVersion, Metric)
        .join(Metric, Metric.id == MetricVersion.metric_id)
        .where(MetricVersion.id.in_([rank[2] for rank in chosen.values()]))
    ):
        role = None
        if version.base_asset_id == asset.id:
            role = "base"
        elif any(isinstance(d, dict) and d.get("asset_id") == asset.id for d in version.dependency or []):
            role = "dependency"
        elif any(isinstance(j, dict) and j.get("join_path_id") in paths for j in version.joins or []):
            role = "join"
        if role is None:
            continue
        out.append(
            {
                "id": metric.id,
                "code": metric.code,
                "name": metric.name,
                "version_id": version.id,
                "version_no": version.version_no,
                "status": version.status,
                "description": version.description,
                "role": role,
            }
        )
    return sorted(out, key=lambda m: (_ROLE_RANK[m["role"]], m["code"]))


# --- 最近谁问过 -------------------------------------------------------------------


def _mentions(meta: dict | None, asset_id: str) -> bool:
    # 只认真正查过的表（探索数据集的 tables）；asset_ids 是变更、数据健康等上下文数据集顺带列出的表，不算问过
    if not isinstance(meta, dict):
        return False
    return any(isinstance(t, dict) and t.get("asset_id") == asset_id for t in meta.get("tables") or [])


def asked(session: Session, user: User, asset: Asset, days: int = ASKED_DAYS) -> dict:
    """最近谁问过（schemas.DossierAskedOut）：近 days 天内引用过这张表的问数回答与分析运行，按人归并。

    问数看 answer.evidence["sources"][*].asset_id；分析看 analysis_dataset.meta 的 tables[*].asset_id
    （上下文数据集顺带列出的 asset_ids 不算），同一次分析运行只算一次。
    people 按最近一次时间倒序，最多 8 人；total 是总次数。
    last_question 只在该人是当前用户、或当前用户是管理员时给出，其余为空。"""
    since = utcnow() - timedelta(days=days)
    hit_runs: set[str] = set()
    answer_run: dict[str, str] = {}
    for run_id, answer_id, meta in session.execute(
        select(AnalysisDataset.run_id, AnalysisDataset.answer_id, AnalysisDataset.meta).where(
            AnalysisDataset.created_at >= since
        )
    ):
        if answer_id:
            answer_run[answer_id] = run_id
        if _mentions(meta, asset.id):
            hit_runs.add(run_id)

    # 事件：(人, 时间, 问题)；分析里的受治理查询也会留下问数回答，归到所在的运行上，不重复计数
    events: list[tuple[str, datetime, str]] = []
    for answer_id, asked_by, asked_at, question, evidence in session.execute(
        select(Answer.id, Answer.asked_by, Answer.asked_at, Answer.question, Answer.evidence).where(
            Answer.asked_at >= since
        )
    ):
        sources = (evidence or {}).get("sources") if isinstance(evidence, dict) else None
        if not any(isinstance(s, dict) and s.get("asset_id") == asset.id for s in sources or []):
            continue
        if answer_id in answer_run:
            hit_runs.add(answer_run[answer_id])
            continue
        events.append((asked_by, _aware(asked_at), question))
    if hit_runs:
        for user_id, created_at, question in session.execute(
            select(AnalysisRun.user_id, AnalysisRun.created_at, AnalysisRun.question).where(
                AnalysisRun.id.in_(hit_runs)
            )
        ):
            events.append((user_id, _aware(created_at), question))

    people: dict[str, dict] = {}
    for person, at, question in events:
        entry = people.setdefault(person, {"count": 0, "at": at, "question": question})
        entry["count"] += 1
        if at > entry["at"]:
            entry["at"], entry["question"] = at, question
    top = sorted(people.items(), key=lambda item: item[1]["at"], reverse=True)[:ASKED_PEOPLE]
    users = {u.id: u for u in session.scalars(select(User).where(User.id.in_([pid for pid, _ in top])))} if top else {}
    admin = access.is_admin(session, user) if top else False
    out = []
    for person_id, entry in top:
        person = users.get(person_id)
        out.append(
            {
                "user": {"id": person_id, "display_name": person.display_name if person else person_id},
                "count": entry["count"],
                "last_asked_at": entry["at"].isoformat(),
                "last_question": entry["question"] if admin or person_id == user.id else None,
            }
        )
    return {"days": days, "total": len(events), "people": out}


# --- 产出方式 ---------------------------------------------------------------------


def _same_table(table: exp.Table, asset: Asset, default_db: str | None = None) -> bool:
    """语句里的表是不是这张表：没写库名时按导入文件的默认库判断，默认库也没有才只比表名。"""
    name = table.name.lower()
    if name != asset.name.lower():
        return False
    db = (table.db or default_db or "").lower()
    return not db or db == asset.db_name.lower()


def _clears(sql: str | None, dialect, asset: Asset, default_db: str | None = None) -> bool | None:
    """这条 TRUNCATE / DELETE 是否清空了整张表：是 True，不是 False，解析不了 None。"""
    from avelis.importer.statements import _delete_reset_target

    if not sql:
        return None
    try:
        trees = [t for t in sqlglot.parse(sql, read=dialect.name) if t is not None]
    except sqlglot.errors.SqlglotError:
        return None
    if len(trees) != 1:
        return None
    tree = dialect.prepare(trees[0])
    if isinstance(tree, exp.TruncateTable):
        if tree.args.get("is_database"):
            return False
        hit = [t for t in tree.expressions if isinstance(t, exp.Table) and _same_table(t, asset, default_db)]
        if not hit:
            return False
        # 只清某个分区：不是整表清空，也不能据此说是追加
        partial = tree.args.get("partition") or any(t.args.get("partition") for t in hit)
        return None if partial else True
    if isinstance(tree, exp.Delete):
        target = _delete_reset_target(tree)
        if target is not None:
            return _same_table(target, asset, default_db)
        # 带条件的 DELETE：删的是这张表的一部分时无法归类，删的是别的表则与本表无关
        table = tree.this
        return None if isinstance(table, exp.Table) and _same_table(table, asset, default_db) else False
    return None


def _overwrites_whole(sql: str | None, dialect) -> bool:
    """INSERT OVERWRITE 是否覆盖整张表：带分区的只覆盖一部分，解析不了也不算。"""
    if not sql:
        return False
    try:
        trees = [t for t in sqlglot.parse(sql, read=dialect.name) if t is not None]
    except sqlglot.errors.SqlglotError:
        return False
    if len(trees) != 1 or not isinstance(trees[0], exp.Insert) or not trees[0].args.get("overwrite"):
        return False
    tree = trees[0]
    target = tree.this.this if isinstance(tree.this, exp.Schema) else tree.this
    return isinstance(target, exp.Table) and not target.args.get("partition") and not tree.args.get("partition")


def _dialect(session: Session, job: ImportJob):
    from avelis.engines import UnsupportedEngine, dialect_of
    from avelis.lineage.sql import SqlDialect

    ds = session.get(Datasource, job.datasource_id)
    try:
        if ds is not None:
            return dialect_of(ds)
    except UnsupportedEngine:
        pass
    return SqlDialect(name=job.dialect, default_catalog="")


def _write_mode(session: Session, asset: Asset, statements: list[ExtractedStatement], jobs: dict) -> str | None:
    modes: set[str | None] = set()
    clearing: dict[str, list[ExtractedStatement]] = {}
    for stmt in statements:
        kind = (stmt.statement_type or "").upper()
        if kind == "INSERT OVERWRITE":
            whole = _overwrites_whole(stmt.sql_text, _dialect(session, jobs[stmt.job_id]))
            modes.add("overwrite" if whole else None)
        elif kind == "CREATE TABLE AS":
            modes.add("create")
        elif kind == "INSERT":
            if stmt.job_id not in clearing:
                clearing[stmt.job_id] = list(
                    session.scalars(
                        select(ExtractedStatement)
                        .where(
                            ExtractedStatement.job_id == stmt.job_id,
                            ExtractedStatement.statement_type.in_(CLEAR_TYPES),
                        )
                        .order_by(ExtractedStatement.seq)
                    )
                )
            dialect = _dialect(session, jobs[stmt.job_id])
            default_db = jobs[stmt.job_id].default_db
            results = [
                _clears(c.sql_text, dialect, asset, default_db) for c in clearing[stmt.job_id] if c.seq < stmt.seq
            ]
            if any(r is None for r in results):
                modes.add(None)
            else:
                modes.add("overwrite" if any(results) else "append")
        else:
            # MERGE、UPDATE、改名等无法归入三种写入方式
            modes.add(None)
        if None in modes or len(modes) > 1:
            return None
    return modes.pop() if len(modes) == 1 else None


def produced(session: Session, user: User, asset: Asset, *, show_sql: bool) -> dict | None:
    """这张表是怎么产出的（schemas.DossierProducedOut）；视图返回 kind=view，没有导入脚本写入它时返回 None。

    脚本来自写入这张表的导入边（origin 为 import / model_confirmed，import_job_id 指向未被取代的 import_job）
    及其 attributes.statement_id 对应的 extracted_statement；按文件名、行号排序，最多 5 段。
    show_sql 为假时 sql 置空。write_mode 说的是脚本自己的写法，只在能确定时给出：不带分区的 INSERT OVERWRITE，
    或同一文件里先 TRUNCATE / 整表 DELETE 这张表再 INSERT → overwrite；CREATE TABLE AS（含 CREATE OR REPLACE）→ create；
    只有 INSERT 且同文件没有动过这张表的清空语句 → append；按分区覆盖、按条件删除后再写、MERGE、UPDATE
    或几种写法并存 → 空。"""
    if asset.kind in ("view", "materialized_view"):
        return {"kind": "view", "write_mode": None, "scripts": []}
    edges = session.execute(
        select(LineageEdge.import_job_id, LineageEdge.attributes).where(
            LineageEdge.target_asset_id == asset.id,
            LineageEdge.status == "active",
            LineageEdge.origin.in_(IMPORT_ORIGINS),
            LineageEdge.import_job_id.is_not(None),
        )
    ).all()
    wanted: dict[str, str] = {}
    for job_id, attributes in edges:
        statement_id = (attributes or {}).get("statement_id") if isinstance(attributes, dict) else None
        if statement_id:
            wanted[statement_id] = job_id
    if not wanted:
        return None
    jobs = {
        j.id: j
        for j in session.scalars(
            select(ImportJob).where(ImportJob.id.in_(set(wanted.values())), ImportJob.status != "superseded")
        )
    }
    if not jobs:
        return None
    statements = [
        s
        for s in session.scalars(select(ExtractedStatement).where(ExtractedStatement.id.in_(wanted)))
        if s.job_id in jobs
    ]
    if not statements:
        return None
    statements.sort(key=lambda s: (jobs[s.job_id].filename, s.line_start, s.seq))
    scripts = [
        {
            "job_id": s.job_id,
            "batch_id": jobs[s.job_id].batch_id,
            "filename": jobs[s.job_id].filename,
            "statement_type": s.statement_type,
            "line_start": s.line_start,
            "line_end": s.line_end,
            "sql": (s.sql_text or s.raw_text) if show_sql else None,
            "imported_at": _iso(jobs[s.job_id].created_at),
        }
        for s in statements[:MAX_SCRIPTS]
    ]
    return {"kind": "script", "write_mode": _write_mode(session, asset, statements, jobs), "scripts": scripts}
