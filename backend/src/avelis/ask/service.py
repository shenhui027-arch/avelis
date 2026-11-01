"""问数管线：理解问题 → 检索有权访问的口径 → 处理歧义 → 检查版本、状态与新鲜度 → 编译 SQL
→ 确定性校验 → 只读执行 → 解释结果并给出依据。每一步的结论都写入 answer 记录。

后端约束（不依赖前端或提示词）：
- 受影响 / 失效的指标版本不执行，返回 blocked 并说明原因，不悄悄切换到其他口径；
- 执行前再次核对依赖指纹（计划与执行之间发生变化则拒绝）；
- 权限覆盖检索（只给模型看有权指标）、SQL（沿血缘检查来源资产所属域）、结果与缓存（键含权限范围）；
- 数据库报错如实返回，不改写成看似成功的答案。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

import sqlglot
from sqlglot import exp
from sqlalchemy import or_, select
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from avelis import audit, cache
from avelis.ask.compiler import ValidationFailed, compile_metric_query, distinct_values_sql, validate_sql
from avelis.ask.intent import Intent, MetricCard, period_column
from avelis.collect.service import freshness_boundary
from avelis.config import get_settings
from avelis.connectors.base import QueryError
from avelis.db import utcnow
from avelis.engines import dialect_of, engine_of, query_connector
from avelis.engines.base import LIVE_CHECK, QUERY
from avelis.governance import access
from avelis.governance.errors import GovernanceError
from avelis.governance.metrics import (
    dependency_drift,
    describe_version,
    latest_governed_version,
    publish_check,
    resolve_joins,
    validate_spec,
    visible_metrics,
)
from avelis.i18n import current_locale, gettext, locale_context
from avelis.lineage.sql import canonical_hash_of
from avelis.models import (
    Answer,
    Asset,
    ColumnVersion,
    Datasource,
    Definition,
    Impact,
    Job,
    LineageEdge,
    Metric,
    MetricVersion,
    ReviewTask,
    User,
)

TRACEABLE_NOTE = "可追溯 ≠ 可复现：保存了 SQL、口径与元数据版本，但不保存数据快照，历史答案不保证能按当时数据重算。"
DISCOVERY_NOTE = "元数据按轮询采集，最长延迟为一个采集间隔：未发现变更不代表没有变更。"
DIMENSION_VALUES_LIMIT = 200


@dataclass
class Catalog:
    """当前用户可检索的口径：只含有权业务域、且进入过发布流程的指标。"""

    cards: list[MetricCard]
    versions: dict[str, tuple[Metric, MetricVersion]]


def visible_catalog(session: Session, user: User, *, with_values: bool) -> Catalog:
    access.require_role(session, user, "analyst", "steward", "reviewer")
    settings = get_settings()
    cards, versions = [], {}
    for metric in visible_metrics(session, user):
        version = latest_governed_version(session, metric)
        if version is None:
            continue
        versions[metric.code] = (metric, version)
        cards.append(MetricCard(
            code=metric.code, name=metric.name, aliases=metric.aliases or [], dimensions=version.dimensions or [],
            dimension_values=_dimension_values(session, user, version, settings) if with_values else {},
            description=version.description, status=version.status))
    return Catalog(cards, versions)


def visible_metric(session: Session, code: str, allowed: set[str]) -> tuple[Metric | None, MetricVersion | None]:
    """单个口径的可见性，与 visible_catalog 同一规则（不建全量目录）：allowed 为用户可访问的业务域。
    指标不存在时为 (None, None)；存在但不在 allowed 中、或没有进入过发布流程的版本时，版本为 None。"""
    metric = session.scalar(select(Metric).where(Metric.code == code))
    if metric is None:
        return None, None
    if metric.domain_id not in allowed:
        return metric, None
    return metric, latest_governed_version(session, metric)


def version_sources(session: Session, version: MetricVersion) -> dict[str, Asset]:
    """一个口径版本查询时会读到的全部来源：主表与关联的右表（resolve_joins），再沿血缘向上（upstream_closure）。
    与 _fulfil 的来源权限检查是同一范围。"""
    aliases, _ = resolve_joins(session, version)
    return upstream_closure(session, [a.id for a in aliases.values()])


def start_answer(session: Session, user: User, question: str, adapter: str, thread_id: str | None) -> Answer:
    answer = Answer(question=question, asked_by=user.id, status="error", model_adapter=adapter, thread_id=thread_id)
    session.add(answer)
    session.flush()
    return answer


def finish_answer(session: Session, user: User, answer: Answer, started: float) -> Answer:
    answer.duration_ms = int((time.monotonic() - started) * 1000)
    # JSON 字段在管线中被原地修改；显式标记，确保持久化的依据与返回给用户的一致
    for name in ("intent", "plan", "metric_version_ids", "evidence", "result", "freshness", "steps"):
        flag_modified(answer, name)
    session.flush()
    audit.record(session, user, "ask", "answer", answer.id, status=answer.status, error_code=answer.error_code,
                 metric_version_ids=answer.metric_version_ids, cache_hit=answer.cache_hit,
                 thread_id=answer.thread_id)
    return answer


def fulfil(session: Session, user: User, intent: Intent, answer: Answer, catalog: Catalog) -> None:
    """意图已由模型（或演示规则）给出：其余全部是确定性的治理、编译、校验与执行。
    单指标、无对比：整条下推为一条 SQL。多个指标、对比期、派生指标：展开为执行计划（见 _fulfil_plan）。"""
    try:
        version = catalog.versions.get(intent.metric_code or "", (None, None))[1]
        if intent.is_simple and (version is None or version.kind != "derived"):
            _fulfil(session, user, intent, answer, catalog.versions, get_settings())
        else:
            _fulfil_plan(session, user, intent, answer, catalog, get_settings())
    except GovernanceError as exc:
        answer.status = "error"
        answer.message = exc.message
        answer.error_code = "governance"


def _fulfil(session: Session, user: User, intent: Intent, answer: Answer,
            versions: dict[str, tuple[Metric, MetricVersion]], settings) -> None:
    answer.intent = intent.model_dump(mode="json")
    if intent.metric_code not in versions:
        answer.status = "denied"
        answer.message = "意图引用了不可见或不存在的指标，已拒绝"
        answer.error_code = "invalid_intent"
        return
    metric, version = versions[intent.metric_code]
    answer.metric_version_ids = [version.id]
    allowed_dims = {d["name"] for d in version.dimensions or []}
    bad = [d for d in [*intent.group_by, *(f.dimension for f in intent.filters)] if d not in allowed_dims]
    if bad:
        answer.status = "denied"
        answer.error_code = "invalid_intent"
        answer.message = f"维度 {'、'.join(bad)} 不在指标允许的维度中"
        return
    if intent.start_date and intent.end_date and intent.start_date > intent.end_date:
        answer.status = "clarify"
        answer.message = "开始日期晚于结束日期"
        return

    # 版本与状态：两条状态线都要满足
    version_info = describe_version(session, version)
    answer.evidence["metric_version"] = _version_evidence(session, metric, version)
    if version.status != "published":
        answer.status = "blocked"
        answer.error_code = f"metric_{version.status}"
        answer.message = _blocked_message(session, metric, version)
        return
    drift = dependency_drift(session, version)
    if drift:
        answer.status = "blocked"
        answer.error_code = "dependency_drift"
        cols = "、".join(f"{d['qualified']}.{d['column']}" for d in drift)
        answer.message = (f"指标“{metric.name}”V{version.version_no} 的依赖 {cols} 与发布时不一致，"
                          f"已停止使用该口径，等待重新采集与复核")
        answer.evidence["drift"] = drift
        return

    resolved = validate_spec(session, version)
    aliases, steps = resolved["aliases"], resolved["steps"]
    from avelis.governance.joins import effectively_approved

    for step in steps:
        if not effectively_approved(session, step["path"]):
            answer.status = "blocked"
            answer.error_code = "join_path_not_approved"
            answer.message = f"关联路径 {step['path'].code} 当前为 {step['path'].status}，不能用于问数"
            return
    if steps:
        # 关联基数按当前粒度重新判定：粒度被证伪、键变化后，发布时的 N:1 依据可能已不成立
        fan_out = publish_check(session, version, user)["fan_out"]
        if fan_out:
            answer.status = "blocked"
            answer.error_code = "fan_out"
            answer.message = "关联会放大度量，已停止执行：" + "；".join(fan_out)
            return

    # 权限：查询会读到的全部来源（含视图背后的表）都必须可见
    closure = upstream_closure(session, [a.id for a in aliases.values()])
    allowed = access.domain_ids(session, user)
    denied = [a for a in closure.values() if not access.can_read_asset(session, user, a, allowed)]
    if denied:
        answer.status = "denied"
        answer.error_code = "permission"
        unassigned = [a for a in denied if a.domain_id is None]
        answer.message = "没有权限读取查询涉及的来源：" + "、".join(sorted(
            f"{a.catalog_name}.{a.db_name}.{a.name}{'（未归属业务域）' if a in unassigned else ''}" for a in denied))
        return

    time_alias, time_col = version.time_column.split(".", 1) if "." in version.time_column else ("b",
                                                                                                 version.time_column)
    time_type = _column_type(session, aliases[time_alias], time_col)
    base = aliases["b"]
    ds = session.get(Datasource, base.datasource_id)
    dialect = dialect_of(ds)
    compiled = compile_metric_query(version=version, aliases=aliases, steps=steps, intent=intent,
                                    max_rows=settings.query_max_rows, time_type=time_type, dialect=dialect,
                                    reserved_names=(metric.name, "value"))
    answer.sql = compiled.sql
    answer.plan = {**compiled.plan, "metric_version_id": version.id,
                   "dependency_fingerprint": version.dependency_fingerprint}
    schema = {f"{a.catalog_name}.{a.db_name}.{a.name}": set(_columns(session, a)) for a in aliases.values()}
    try:
        validate_sql(compiled.sql, dialect=dialect, allowed_tables=set(compiled.tables), schema=schema,
                     max_rows=settings.query_max_rows)
    except ValidationFailed as exc:
        answer.status = "denied"
        answer.error_code = "validation"
        answer.message = "SQL 校验未通过：" + "；".join(exc.reasons)
        return

    connector = query_connector(session, ds)
    # 元数据过期：轮询采集之间库侧可能已变。执行（含使用缓存）前核对来源对象，与上次采集不一致就停下并触发采集
    stale = _live_check(session, ds, connector, closure.values(), resolved["dependency"], settings)
    if stale:
        answer.status = "blocked"
        answer.error_code = "metadata_stale"
        answer.message = ("来源结构在上次采集之后已变化，未执行；已为该数据源排队重新采集，采集完成后再问：" +
                          "；".join(s["summary"] for s in stale))
        answer.evidence["live_check"] = stale
        return
    internal = [(a.db_name, a.name) for a in closure.values()
                if a.datasource_id == ds.id and a.kind == "table"]
    freshness = {"tables": connector.freshness(internal), **{k: _iso(v) for k, v in
                                                            freshness_boundary(session, ds.id).items()},
                 "note": DISCOVERY_NOTE}
    answer.freshness = freshness
    answer.evidence.update({
        "sources": [_asset_evidence(session, a) for a in closure.values()],
        "lineage": _lineage_evidence(session, version),
        "limits": {"query_timeout_seconds": settings.query_timeout_seconds,
                   "query_mem_limit_bytes": settings.query_mem_limit_bytes,
                   "max_rows": settings.query_max_rows, "account": "只读账号（query 凭据）"},
        "traceability": TRACEABLE_NOTE,
        "version_detail": {k: version_info[k] for k in ("expression", "time_column", "timezone", "filters",
                                                         "dimensions", "grain", "joins")},
    })

    key = cache.answer_cache_key({
        "sql": compiled.sql, "version": version.id, "deps": version.dependency_fingerprint,
        "assets": sorted((a.id, a.current_version_id) for a in closure.values()),
        "scope": sorted(allowed), "freshness": freshness.get("tables"),
    })
    cached = _cache_get(key)
    if cached is not None:
        answer.status = "answered"
        answer.result = cached["result"]
        answer.executed_at = utcnow()
        answer.cache_hit = True
        answer.evidence["cache"] = {"hit": True, "original_answer_id": cached.get("answer_id"),
                                    "original_executed_at": cached.get("executed_at")}
        answer.message = _explain(metric, version, intent, answer.result)
        return

    # 执行前最后一次核对：计划绑定的版本不能在此期间变化
    session.refresh(version)
    if version.status != "published" or dependency_drift(session, version):
        answer.status = "blocked"
        answer.error_code = "version_changed"
        answer.message = "生成计划后指标版本或其依赖发生变化，已停止执行，请重试"
        return
    try:
        result = connector.execute_select(compiled.sql, timeout_seconds=settings.query_timeout_seconds,
                                          mem_limit_bytes=settings.query_mem_limit_bytes,
                                          max_rows=settings.query_max_rows)
    except QueryError as exc:
        answer.status = "error"
        answer.error_code = f"db_{exc.code}"
        label = {"timeout": "查询超时", "permission": "数据库拒绝访问", "invalid_object": "对象或列无法解析"}.get(
            exc.code, "数据库执行错误")
        answer.message = f"{label}（数据库错误 {exc.db_errno}）：{exc.message}"
        answer.executed_at = utcnow()
        return
    answer.executed_at = utcnow()
    answer.result = {"columns": result.columns, "rows": [[_jsonable(v) for v in row] for row in result.rows],
                     "row_count": len(result.rows), "truncated": result.truncated,
                     "column_docs": _dimension_docs(session, user, version, aliases, intent.group_by)}
    answer.status = "answered"
    answer.evidence["cache"] = {"hit": False}
    answer.message = _explain(metric, version, intent, answer.result)
    _cache_put(key, {"result": answer.result, "answer_id": answer.id, "executed_at": answer.executed_at.isoformat()},
               settings.answer_cache_seconds, [f"metric_version:{version.id}"])


def _upstream_columns(session: Session, dependency: list[dict[str, Any]]) -> dict[str, set[str]]:
    """口径依赖的列沿已解析的血缘向上展开：资产 → 实际影响结果的列（含过滤、关联、分组等影响条件的来源列）。"""
    out: dict[str, set[str]] = {}
    stack = [(d["asset_id"], d["column"]) for d in dependency]
    while stack:
        asset_id, column = stack.pop()
        if column in out.setdefault(asset_id, set()):
            continue
        out[asset_id].add(column)
        asset = session.get(Asset, asset_id)
        if asset is None or not asset.current_version_id:
            continue
        for edge in session.scalars(select(LineageEdge).where(
                LineageEdge.target_asset_id == asset_id, LineageEdge.status == "active",
                LineageEdge.origin.in_(("parsed", "manual")),
                LineageEdge.target_asset_version_id == asset.current_version_id,
                or_(LineageEdge.target_column == column, LineageEdge.target_column.is_(None)))):
            if edge.source_asset_id and edge.source_column:
                stack.append((edge.source_asset_id, edge.source_column))
    return out


def _live_check(session: Session, ds: Datasource, connector, assets, dependency: list[dict[str, Any]],
                settings) -> list[dict[str, Any]]:
    """用只读账号读取来源对象当前的列与视图定义，与当前资产版本比较，返回不一致的对象。
    - 只看口径实际依赖的列（沿血缘展开）：无关列的增删不拦截；视图定义变化总是拦截。
    - 只核对本数据源内部 Catalog 的表与视图；他库经外部表的变化由那个数据源的采集发现。
    - 发现不一致时为该数据源排队一次采集（已有待办则不重复）。"""
    engine = engine_of(ds)
    if not settings.ask_live_check or not engine.supports(LIVE_CHECK):
        return []
    dialect = dialect_of(ds)
    targets = [a for a in assets if a.datasource_id == ds.id and a.catalog_name == dialect.default_catalog
               and a.kind in ("table", "view") and a.current_version_id]
    if not targets:
        return []
    relevant = _upstream_columns(session, dependency)
    try:
        live = connector.describe_objects([(a.db_name, a.name) for a in targets])
    except QueryError:
        return []  # 核对失败不阻止问数：执行时数据库会如实报错
    stale = []
    for a in targets:
        now = live.get((a.db_name, a.name))
        if now is None:
            continue
        cols = [[c.name, c.data_type] for c in session.scalars(select(ColumnVersion).where(
            ColumnVersion.asset_version_id == a.current_version_id).order_by(ColumnVersion.ordinal))]
        problems = []
        if not now["columns"]:
            problems.append("对象已不存在")
        elif a.physical_id and now.get("physical_id") and now["physical_id"] != a.physical_id:
            problems.append("对象已被删除后同名重建")
        elif now["columns"] != cols:
            before, after = {c[0]: c[1] for c in cols}, {c[0]: c[1] for c in now["columns"]}
            used = relevant.get(a.id) or set(before)
            problems += [f"列 {n} 已删除" for n in before if n in used and n not in after]
            problems += [f"列 {n} 类型 {before[n]} → {after[n]}" for n in before
                         if n in used and n in after and before[n] != after[n]]
        if a.kind == "view" and now["columns"]:
            stored = session.scalar(select(Definition.canonical_hash).where(
                Definition.asset_version_id == a.current_version_id))
            current = canonical_hash_of(now["definition"], dialect)[1] if now["definition"] else None
            if current != stored:
                problems.append("视图定义已变化")
        if problems:
            stale.append({"asset_id": a.id, "qualified": f"{a.catalog_name}.{a.db_name}.{a.name}",
                          "problems": problems, "summary": f"{a.db_name}.{a.name}（{'、'.join(problems)}）"})
    if stale:
        from avelis import jobs  # 避免循环导入

        pending = session.scalar(select(Job.id).where(Job.kind == "collect", Job.status.in_(("queued", "running")),
                                                      Job.payload["datasource_id"].as_string() == ds.id).limit(1))
        if pending is None:
            jobs.enqueue("collect", {"datasource_id": ds.id, "trigger": "live_check"}, None)
    return stale


def _cache_get(key: str):
    try:
        return cache.cache_get(key)
    except Exception:  # Redis 不可用只影响性能
        return None


def _cache_put(key: str, value: dict, ttl: int, tags: list[str]) -> None:
    try:
        cache.cache_put(key, value, ttl, tags)
    except Exception:
        pass


def _dimension_docs(session: Session, user: User, version: MetricVersion, aliases: dict[str, Asset],
                    names: list[str]) -> dict[str, dict[str, Any]]:
    """结果中各维度列的数据库注释（原样列名 + 注释状态）。Avelis 不另起显示名：注释缺失就如实标出，
    推动数据库侧补齐（见 governance/column_docs.py）。"""
    from avelis.governance.column_docs import column_doc, visible_to

    can_see = visible_to(session, user)
    dims = {d["name"]: d for d in version.dimensions or []}
    out = {}
    for name in names:
        ref = dims.get(name, {}).get("column", "")
        alias, column = ref.split(".", 1) if "." in ref else ("b", ref)
        asset = aliases.get(alias)
        if asset is not None and column:
            out[name] = column_doc(session, asset, column, can_see=can_see)
    return out


PERIOD_LABEL = {"current": "本期", "previous_period": "上一期", "previous_year": "去年同期", "custom": "对比期"}


def _scratch(answer: Answer, user: User) -> Answer:
    """子查询用的临时答案对象（不入库）：沿用完整的单指标治理管线，只取它的结论与结果。"""
    return Answer(question=answer.question, asked_by=user.id, status="error", model_adapter=answer.model_adapter,
                  intent={}, plan={}, evidence={}, result={}, metric_version_ids=[], freshness={}, steps=[],
                  cache_hit=False)


def _decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return None


def _fulfil_plan(session: Session, user: User, intent: Intent, answer: Answer, catalog: Catalog, settings) -> None:
    """执行计划：把问题展开为若干“指标 × 时间段”的子查询，每个子查询都经过完整的单指标治理管线
    （两条状态线、来源权限、执行前核对、只读执行）。任何一个没有通过，整个答案就按它的原因拦截或报错；
    全部通过后在后端按维度键对齐，计算派生公式与对比列，最后排序、取前 N。模型只给出意图，不参与计算。"""
    from avelis.ask.intent import comparison_range
    from avelis.governance.derived import evaluate

    answer.intent = intent.model_dump(mode="json")
    unknown = [c for c in intent.metric_codes if c not in catalog.versions]
    if unknown:
        answer.status, answer.error_code = "denied", "invalid_intent"
        answer.message = "意图引用了不可见或不存在的指标，已拒绝"
        return
    if intent.start_date is None or intent.end_date is None or intent.start_date > intent.end_date:
        answer.status, answer.message = "clarify", "请指定有效的日期或日期范围"
        return

    requested = [(code, *catalog.versions[code]) for code in intent.metric_codes]
    runs: dict[str, tuple[Metric, MetricVersion]] = {}
    allowed = access.domain_ids(session, user)
    for code, metric, version in requested:
        answer.metric_version_ids = [*answer.metric_version_ids, version.id]
        if version.kind != "derived":
            runs[code] = (metric, version)
            continue
        if version.status != "published":  # 派生指标自身也有两条状态线
            answer.status, answer.error_code = "blocked", f"metric_{version.status}"
            answer.message = _blocked_message(session, metric, version)
            answer.evidence["metric_version"] = _version_evidence(session, metric, version)
            return
        for c in version.components:
            cm, cv = session.get(Metric, c["metric_id"]), session.get(MetricVersion, c["version_id"])
            if cm is None or cv is None or cm.domain_id not in allowed:
                answer.status, answer.error_code = "denied", "permission"
                answer.message = f"派生指标“{metric.name}”的组成指标 {c['metric_name']} 不在你可访问的业务域"
                return
            runs[c["metric_code"]] = (cm, cv)  # 用提交时锁定的组成版本，而不是组成指标的最新版本
            answer.metric_version_ids = [*answer.metric_version_ids, cv.id]

    used = {d for d in [*intent.group_by, *(f.dimension for f in intent.filters)]}
    for code, (metric, version) in runs.items():
        missing = used - {d["name"] for d in version.dimensions or []}
        if missing:
            answer.status, answer.error_code = "denied", "invalid_intent"
            answer.message = f"维度 {'、'.join(sorted(missing))} 不是指标“{metric.name}”开放的维度，不能一起过滤或分组"
            return

    periods = [("current", intent.start_date, intent.end_date)]
    if intent.compare:
        periods.append((intent.compare, *((intent.compare_start, intent.compare_end) if intent.compare == "custom"
                                          else comparison_range(intent.start_date, intent.end_date, intent.compare))))
    values: dict[tuple[str, str], dict[tuple, Decimal | None]] = {}
    keys: list[tuple] = []
    comparison_keys: list[tuple] = []
    plan, sqls, sources, lineage = [], [], {}, []
    truncated, cache_hits = False, []
    for code, (metric, version) in runs.items():
        for label, start, end in periods:
            sub = _scratch(answer, user)
            sub_intent = Intent(metric_code=code, start_date=start, end_date=end, filters=intent.filters,
                                group_by=intent.group_by, time_grain=intent.time_grain)
            _fulfil(session, user, sub_intent, sub, {code: (metric, version)}, settings)
            plan.append({"metric_code": code, "metric_version_id": version.id, "period": label,
                         "range": [str(start), str(end)], "status": sub.status, "error_code": sub.error_code,
                         "cache_hit": sub.cache_hit})
            if sub.sql:
                head = f"-- {metric.name} V{version.version_no} · {PERIOD_LABEL[label]} {start} ~ {end}"
                sqls.append(head + chr(10) + sub.sql)
            if sub.status != "answered":
                answer.status, answer.error_code = sub.status, sub.error_code
                answer.message = f"{metric.name}（{PERIOD_LABEL[label]}）：{sub.message}"
                answer.sql = (chr(10) * 2).join(sqls) or None
                answer.evidence = {**sub.evidence, "plan": plan}
                return
            rows = {tuple(r[:-1]): _decimal(r[-1]) for r in sub.result.get("rows", [])}
            values[(code, label)] = rows
            if label == "current":
                keys += [k for k in rows if k not in keys]
            else:
                comparison_keys += [k for k in rows if k not in comparison_keys]
            truncated = truncated or bool(sub.result.get("truncated"))
            cache_hits.append(sub.cache_hit)
            for s in sub.evidence.get("sources", []):
                sources.setdefault(s.get("qualified"), s)
            lineage += sub.evidence.get("lineage", [])
            if not answer.freshness:
                answer.freshness = sub.freshness
            if label == "current" and "limits" in sub.evidence:
                answer.evidence["limits"] = sub.evidence["limits"]
    keys += [k for k in comparison_keys if k not in keys]

    def value(code: str, label: str, key: tuple) -> Decimal | None:
        metric, version = catalog.versions[code]
        if version.kind == "derived":
            return evaluate(version.expression, {c["metric_code"]: values[(c["metric_code"], label)].get(key)
                                                 for c in version.components})
        return values[(code, label)].get(key)

    def missing_row(code: str, label: str, key: tuple) -> bool:
        _, version = catalog.versions[code]
        components = [c["metric_code"] for c in version.components] if version.kind == "derived" else [code]
        return all(key not in values[(component, label)] for component in components)

    metric_columns = []
    for _, metric, _ in requested:
        metric_columns.append(metric.name)
        if intent.compare:
            metric_columns += [f"{metric.name}·{PERIOD_LABEL[intent.compare]}", f"{metric.name}·变化", f"{metric.name}·变化率"]
    columns = ([period_column(intent.group_by, metric_columns)] if intent.time_grain else []) + list(intent.group_by)
    for _, metric, _ in requested:
        columns.append(metric.name)
        if intent.compare:
            columns += [f"{metric.name}·{PERIOD_LABEL[intent.compare]}", f"{metric.name}·变化", f"{metric.name}·变化率"]
    rows_out = []
    missing_members: dict[tuple, list[str]] = {}
    for key in keys or ([()] if not intent.group_by and not intent.time_grain else []):
        row: list[Any] = list(key)
        missing_members[key] = []
        for code, metric, _ in requested:
            current = value(code, "current", key)
            row.append(current)
            if missing_row(code, "current", key):
                missing_members[key].append(metric.name)
            if intent.compare:
                before = value(code, intent.compare, key)
                if missing_row(code, intent.compare, key):
                    missing_members[key].append(f"{metric.name}·{PERIOD_LABEL[intent.compare]}")
                delta = current - before if current is not None and before is not None else None
                rate = (delta / before).quantize(Decimal("0.0001")) if delta is not None and before else None
                row += [before, delta, rate]
        rows_out.append(row)
    if intent.time_grain and not intent.order:
        rows_out.sort(key=lambda r: str(r[0]))
    if intent.order:  # 按第一个指标的本期值排序；空值排最后
        first = len(intent.group_by) + (1 if intent.time_grain else 0)
        present = [r for r in rows_out if r[first] is not None]
        present.sort(key=lambda r: r[first], reverse=intent.order == "desc")
        rows_out = present + [r for r in rows_out if r[first] is None]
    if intent.limit:
        rows_out = rows_out[:intent.limit]
    key_width = len(intent.group_by) + (1 if intent.time_grain else 0)
    answer.evidence["missing_member_rows"] = [missing_members[tuple(r[:key_width])] for r in rows_out]

    first_code, first_metric, first_version = requested[0]
    answer.status = "answered"
    first_run = next(iter(runs.values()))[1]
    docs = _dimension_docs(session, user, first_run, validate_spec(session, first_run)["aliases"], intent.group_by)
    answer.result = {"columns": columns, "rows": [[_jsonable(v) for v in r] for r in rows_out],
                     "row_count": len(rows_out), "truncated": truncated, "column_docs": docs}
    answer.sql = (chr(10) * 2).join(sqls)
    answer.plan = {"kind": "plan", "subqueries": plan, "order": intent.order, "limit": intent.limit,
                   "compare": intent.compare}
    answer.evidence.update({"metric_version": _version_evidence(session, first_metric, first_version),
                            "plan": plan, "sources": list(sources.values()), "lineage": lineage,
                            "derived": [{"metric_code": c, "formula": v.expression, "components": v.components}
                                        for c, _, v in requested if v.kind == "derived"],
                            "cache": {"hit": all(cache_hits)}})
    answer.executed_at = utcnow()
    answer.cache_hit = bool(cache_hits) and all(cache_hits)
    if truncated and (intent.order or intent.limit):
        answer.evidence["warning"] = "子查询结果超过行数上限被截断，排名可能不完整"
    answer.message = _explain_plan(requested, intent, answer.result)


def _answer_text(template: str, **params) -> str:
    language = getattr(get_settings(), "reply_language", "auto")
    with locale_context(current_locale() if language == "auto" else language):
        return gettext(template, **params)


def _explain_plan(requested, intent: Intent, result: dict[str, Any]) -> str:
    same_day = intent.start_date == intent.end_date
    period = str(intent.start_date) if same_day else _answer_text(
        "{start} 至 {end}", start=intent.start_date, end=intent.end_date)
    scope = "，".join(f"{f.dimension}={'/'.join(f.values)}" for f in intent.filters)
    names = "、".join(f"{m.name}（V{v.version_no}）" for _, m, v in requested)
    head = f"{period}{('（' + scope + '）') if scope else ''} {names}"
    if not result["rows"]:
        return _answer_text("{head}：没有符合条件的数据", head=head)
    if not intent.group_by and len(result["rows"]) == 1:
        cells = dict(zip(result["columns"], result["rows"][0]))
        return _answer_text("{head}：{value}", head=head, value="；".join(
            f"{k} {_answer_text('无数据') if v is None else v}" for k, v in cells.items()))
    rank = _answer_text("，按{metric}{direction}", metric=requested[0][1].name,
                        direction=_answer_text("从高到低" if intent.order == "desc" else "从低到高")) if intent.order else ""
    return _answer_text("{head}：共 {row_count} 行{rank}{truncated}", head=head, row_count=result["row_count"],
                        rank=rank, truncated=_answer_text("（已截断）") if result["truncated"] else "")


def _jsonable(value: Any) -> Any:
    if isinstance(value, (int, float, str)) or value is None:
        return value
    return str(value)


def _iso(value) -> str | None:
    return value.isoformat() if value else None


def _explain(metric: Metric, version: MetricVersion, intent: Intent, result: dict[str, Any]) -> str:
    same_day = intent.start_date == intent.end_date
    period = str(intent.start_date) if same_day else _answer_text(
        "{start} 至 {end}", start=intent.start_date, end=intent.end_date)
    scope = "，".join(f"{f.dimension}={'/'.join(f.values)}" for f in intent.filters)
    head = f"{period}{('（' + scope + '）') if scope else ''} {metric.name}（V{version.version_no}）"
    if not result["rows"]:
        return _answer_text("{head}：没有符合条件的数据", head=head)
    if not intent.group_by and len(result["rows"]) == 1:
        value = result["rows"][0][-1]
        return _answer_text("{head}：{value}", head=head, value=value if value is not None else _answer_text("无数据"))
    return _answer_text("{head}：共 {row_count} 行{rank}{truncated}", head=head, row_count=result["row_count"],
                        rank="", truncated=_answer_text("（已截断）") if result["truncated"] else "")


def _blocked_message(session: Session, metric: Metric, version: MetricVersion) -> str:
    if version.status == "affected":
        task = session.scalar(select(ReviewTask).where(ReviewTask.object_type == "metric_version",
                                                       ReviewTask.object_id == version.id,
                                                       ReviewTask.status == "open"))
        return (f"指标“{metric.name}”V{version.version_no} 已受影响：{version.status_reason}。"
                f"在负责人复核前，不会把当前数据当作已确认的“{metric.name}”返回，也不会自动改用其他口径"
                + (f"（复核任务 {task.id}）" if task else ""))
    if version.status == "retired":
        task = session.scalar(select(ReviewTask).where(ReviewTask.object_type == "metric_version",
                                                       ReviewTask.object_id == version.id,
                                                       ReviewTask.status == "resolved"))
        hint = ""
        if task and task.linked_metric_version_id:
            linked = session.get(MetricVersion, task.linked_metric_version_id)
            linked_metric = session.get(Metric, linked.metric_id)
            hint = f"。负责人已发布新口径“{linked_metric.name}”V{linked.version_no}，如需该口径请直接询问"
        return f"指标“{metric.name}”V{version.version_no} 已停用：{version.status_reason}{hint}"
    return f"指标“{metric.name}”当前没有可用的已发布版本"


def _version_evidence(session: Session, metric: Metric, version: MetricVersion) -> dict[str, Any]:
    owner = session.get(User, metric.owner_id)
    reviewer = session.get(User, version.reviewed_by) if version.reviewed_by else None
    return {"id": version.id, "metric_code": metric.code, "metric_name": metric.name,
            "version_no": version.version_no, "status": version.status, "description": version.description,
            "expression": version.expression, "owner": owner.display_name if owner else None,
            "reviewed_by": reviewer.display_name if reviewer else None,
            "published_at": _iso(version.published_at),
            "dependency_fingerprint": version.dependency_fingerprint}


def upstream_closure(session: Session, asset_ids: list[str]) -> dict[str, Asset]:
    """沿已确认血缘和外部表身份映射向上找全部来源（视图是 SECURITY NONE，授予视图等于暴露来源）。"""
    from avelis.models import IdentityLink

    out: dict[str, Asset] = {}
    queue = list(asset_ids)
    while queue:
        asset_id = queue.pop()
        if asset_id in out:
            continue
        asset = session.get(Asset, asset_id)
        if asset is None:
            continue
        out[asset_id] = asset
        for (source_id,) in session.execute(select(LineageEdge.source_asset_id).where(
                LineageEdge.target_asset_id == asset_id, LineageEdge.status == "active",
                LineageEdge.origin.in_(("parsed", "manual")), LineageEdge.source_asset_id.is_not(None)).distinct()):
            queue.append(source_id)
        link = session.scalar(select(IdentityLink).where(IdentityLink.source_asset_id == asset_id,
                                                         IdentityLink.superseded_at.is_(None)))
        if link and link.target_asset_id and link.status in ("confirmed", "candidate"):
            queue.append(link.target_asset_id)
    return out


def _asset_evidence(session: Session, asset: Asset) -> dict[str, Any]:
    from avelis.models import AssetVersion

    version = session.get(AssetVersion, asset.current_version_id) if asset.current_version_id else None
    ds = session.get(Datasource, asset.datasource_id)
    return {"asset_id": asset.id, "qualified": f"{asset.catalog_name}.{asset.db_name}.{asset.name}",
            "kind": asset.kind, "datasource": ds.name, "is_test_adapter": ds.is_test_adapter,
            "version_id": version.id if version else None, "version_no": version.version_no if version else None,
            "observed_at": _iso(version.observed_at) if version else None,
            "last_seen_at": _iso(version.last_seen_at) if version else None}


def _lineage_evidence(session: Session, version: MetricVersion) -> list[dict[str, Any]]:
    out = []
    for dep in version.dependency or []:
        edges = session.scalars(select(LineageEdge).where(
            LineageEdge.target_asset_id == dep["asset_id"], LineageEdge.target_column == dep["column"],
            LineageEdge.status == "active"))
        out.append({"column": f"{dep['qualified']}.{dep['column']}", "role": dep["role"],
                    "fingerprint": dep["fingerprint"],
                    "sources": [{"source": f"{e.source_ref}.{e.source_column}", "relation": e.relation,
                                 "expression": e.expression, "origin": e.origin} for e in edges]})
    return out


def _columns(session: Session, asset: Asset) -> list[str]:
    return list(session.scalars(select(ColumnVersion.name).where(
        ColumnVersion.asset_version_id == asset.current_version_id)))


def _column_type(session: Session, asset: Asset, column: str) -> str:
    return (session.scalar(select(ColumnVersion.data_type).where(
        ColumnVersion.asset_version_id == asset.current_version_id, ColumnVersion.name == column)) or "").lower()


def _existing_dimension_values(session: Session, user: User, version: MetricVersion, dimension: str,
                               values: list[str], settings) -> set[str] | None:
    """精确核验样本之外的取值；守卫或查询失败时返回未知，不据此宣称取值不存在。"""
    requested = list(dict.fromkeys(values))
    if not requested:
        return set()
    if len(requested) > DIMENSION_VALUES_LIMIT:
        return None
    if version.kind == "derived":
        if (version.status != "published" or dependency_drift(session, version)
                or dimension not in {d["name"] for d in version.dimensions or []}):
            return None
        checked = []
        for component in version.components or []:
            source = session.get(MetricVersion, component["version_id"])
            checked.append(_existing_dimension_values(session, user, source, dimension, requested, settings)
                           if source else None)
        existing = set().union(*(values or set() for values in checked))
        return existing if checked and (all(values is not None for values in checked)
                                        or set(requested) <= existing) else None
    result = _dimension_values(session, user, version, settings, exact_values={dimension: requested})
    return set(result[dimension]) if dimension in result else None


def _dimension_values(session: Session, user: User, version: MetricVersion, settings, *,
                      exact_values: dict[str, list[str]] | None = None) -> dict[str, list[str]]:
    """维度可选值（只读查询，带超时与行数上限；按资产版本缓存）。只用于理解问题中的维度取值。

    与正式问数同一套守卫，且在读缓存之前执行：口径已发布且依赖未漂移；维度所在资产的完整来源闭包对当前用户都可见；
    真正查库前再做执行前核对。取值会进入模型上下文，不能成为绕过权限读取来源数据的旁路。"""
    out: dict[str, list[str]] = {}
    if version.status != "published" or dependency_drift(session, version):
        return out
    if version.kind == "derived":  # 派生：取值来自锁定的组成指标（各自按完整来源权限过滤）
        for c in version.components or []:
            component = session.get(MetricVersion, c["version_id"])
            for name, vals in (_dimension_values(session, user, component, settings, exact_values=exact_values)
                               if component else {}).items():
                out.setdefault(name, vals)
        shared = {d["name"] for d in version.dimensions or []}
        return {k: v for k, v in out.items() if k in shared}
    try:
        resolved = validate_spec(session, version)
    except GovernanceError:
        return out
    aliases = resolved["aliases"]
    allowed = access.domain_ids(session, user)
    for dim in version.dimensions or []:
        if exact_values is not None and dim["name"] not in exact_values:
            continue
        alias, column = dim["column"].split(".", 1) if "." in dim["column"] else ("b", dim["column"])
        asset = aliases.get(alias)
        if asset is None:
            continue
        closure = upstream_closure(session, [asset.id])
        if not all(access.can_read_asset(session, user, a, allowed) for a in closure.values()):
            continue
        key = f"avelis:dimvals:{asset.id}:{asset.current_version_id}:{column}"
        cached = None
        if exact_values is None:
            try:
                cached = cache.client().get(key)
            except Exception:
                pass
        if cached is not None:
            out[dim["name"]] = cached.split("\x1f") if cached else []
            continue
        ds = session.get(Datasource, asset.datasource_id)
        engine = engine_of(ds)
        if not engine.supports(QUERY):
            continue
        qualified = f"{asset.catalog_name}.{asset.db_name}.{asset.name}"
        dialect = dialect_of(ds)
        sql = distinct_values_sql(qualified, column, limit=DIMENSION_VALUES_LIMIT, dialect=dialect)
        if exact_values is not None:
            literals = [exp.National(this=v) if dialect.national_strings else exp.Literal.string(v)
                        for v in exact_values[dim["name"]]]
            matches = [exp.Max(this=exp.Case().when(
                exp.In(this=exp.column(column, table="t"), expressions=[value.copy()]), exp.Literal.number(1))
                .else_(exp.Literal.number(0))) for value in literals]
            sql = (sqlglot.parse_one(sql, read=dialect.name)
                   .select(*matches, append=False).distinct(distinct=False)
                   .where(exp.In(this=exp.column(column, table="t"), expressions=literals))
                   .sql(dialect=dialect.name, identify=True))
        try:
            validate_sql(sql, dialect=dialect, allowed_tables={qualified}, schema={qualified: {column}},
                         max_rows=DIMENSION_VALUES_LIMIT)
            connector = query_connector(session, ds)
            dep = [{"asset_id": asset.id, "column": column}]
            if _live_check(session, ds, connector, closure.values(), dep, settings):
                continue  # 库侧已变、尚未采集：不读取（已排队采集）
            result = connector.execute_select(sql, timeout_seconds=10, mem_limit_bytes=512 * 1024**2,
                                              max_rows=DIMENSION_VALUES_LIMIT)
        except (QueryError, ValidationFailed):
            continue
        if exact_values is not None and result.truncated:
            continue
        if exact_values is not None:
            requested = exact_values[dim["name"]]
            if result.rows and (len(result.rows) != 1 or len(result.rows[0]) != len(requested)):
                continue
            flags = [_decimal(value) if value is not None else Decimal(0)
                     for value in result.rows[0]] if result.rows else [Decimal(0)] * len(requested)
            if any(flag not in (Decimal(0), Decimal(1)) for flag in flags):
                continue
            values = [value for value, flag in zip(requested, flags) if flag == 1]
        else:
            values = [str(r[0]) for r in result.rows]
        out[dim["name"]] = values
        if exact_values is None:
            try:
                cache.client().set(key, "\x1f".join(values), ex=600)
            except Exception:
                pass
    return out


def impacts_for(session: Session, object_type: str, object_id: str) -> list[Impact]:
    return list(session.scalars(select(Impact).where(Impact.object_type == object_type, Impact.object_id == object_id)
                                .order_by(Impact.created_at.desc())))
