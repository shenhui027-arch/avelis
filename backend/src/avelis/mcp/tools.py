"""MCP 工具只组装既有治理与问数服务，不接受调用方提供的 SQL。"""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import ValidationError
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from avelis import modelcfg
from avelis.api.governance import change_set_view
from avelis.ask import service
from avelis.ask.alignment import AlignmentResult, align_filters, candidate_sources, has_dimension_table
from avelis.ask.intent import MAX_LIMIT, MAX_METRICS, DimensionFilter, Intent
from avelis.governance import access, metrics
from avelis.governance.errors import GovernanceError, Invalid, NotFound
from avelis.models import (
    Answer,
    Asset,
    AssetColumn,
    ChangeEvent,
    ChangeSet,
    Impact,
    JoinPath,
    LineageEdge,
    Metric,
    MetricVersion,
    User,
)


def _normalize_metric_codes(metric: str | None, metric_list: list[str] | str | None) -> list[str]:
    raw_codes: list[str] = []
    if metric is not None:
        if not isinstance(metric, str) or not metric.strip():
            raise Invalid("指标编码不能为空")
        raw_codes.append(metric.strip())
    if metric_list is not None:
        if isinstance(metric_list, str):
            parts = [part.strip() for part in metric_list.split(",")]
            if not metric_list.strip() or any(not part for part in parts):
                raise Invalid("指标列表包含空项")
            raw_codes.extend(parts)
        elif isinstance(metric_list, list):
            for item in metric_list:
                if not isinstance(item, str) or not item.strip():
                    raise Invalid("指标列表包含空项")
                raw_codes.append(item.strip())
        else:
            raise Invalid("metrics 参数格式无效")
    codes = list(dict.fromkeys(raw_codes))
    if not codes:
        raise Invalid("请指定至少一个指标")
    if len(codes) > MAX_METRICS:
        raise Invalid(f"一次最多查询 {MAX_METRICS} 个指标", max_metrics=MAX_METRICS)
    return codes


def _parse_filters(filters: list[DimensionFilter] | None) -> list[DimensionFilter]:
    if not filters:
        return []
    parsed: list[DimensionFilter] = []
    for item in filters:
        if isinstance(item, DimensionFilter):
            parsed.append(item)
        else:
            try:
                parsed.append(DimensionFilter.model_validate(item))
            except ValidationError as exc:
                raise Invalid("筛选条件格式无效") from exc
    return parsed


def _align_metric_filters(
    session: Session,
    user: User,
    versions: list[MetricVersion],
    filters: list[DimensionFilter],
    settings,
) -> AlignmentResult:
    if not filters:
        return AlignmentResult(filters=[], explanations=[], unresolved=[], unknown=[])
    unique_versions = list({version.id: version for version in versions}.values())
    results = [align_filters(session, user, version, filters, settings) for version in unique_versions]
    explanations: list[dict] = []
    unresolved: list[dict] = []
    unknown: list[dict] = []
    aligned: list[DimensionFilter] = []
    table_availability: dict[str, bool] = {}
    for index, item in enumerate(filters):
        values: list[str] = []
        for position, original in enumerate(item.values):

            def matches(record, *, _dim=item.dimension, _orig=original):
                return record["dimension"] == _dim and record["original"] == _orig

            pending = [record for result in results for record in result.unresolved if matches(record)]
            uncertain = [record for result in results for record in result.unknown if matches(record)]
            notes = [record for result in results for record in result.explanations if matches(record)]
            resolved = [result.filters[index].values[position] for result in results]
            if pending or (not uncertain and len(set(resolved)) > 1):
                candidates = [value for record in pending for value in record["candidates"]]
                candidates += [value for value in resolved if value != original or not pending]
                has_table = any(record["has_dimension_table"] for record in pending) or any(
                    record["method"] == "dimension_table" for record in notes
                )
                if not has_table:
                    if item.dimension not in table_availability:
                        table_availability[item.dimension] = any(
                            has_dimension_table(session, user, version, item.dimension) for version in unique_versions
                        )
                    has_table = table_availability[item.dimension]
                record = {
                    "dimension": item.dimension,
                    "original": original,
                    "candidates": list(dict.fromkeys(candidates))[:10],
                    **candidate_sources(pending, notes),
                    "has_dimension_table": has_table,
                }
                if record not in unresolved:
                    unresolved.append(record)
                values.append(original)
            elif uncertain:
                values.append(original)
                unknown.extend(record for record in uncertain if record not in unknown)
            else:
                values.append(resolved[0])
                explanations.extend(record for record in notes if record not in explanations)
        aligned.append(DimensionFilter(dimension=item.dimension, values=values))
    if unresolved:
        projected, _ = service.project_alignment_clarifications(session, user, unresolved)
        unresolved = projected
    return AlignmentResult(filters=aligned, explanations=explanations, unresolved=unresolved, unknown=unknown)


class ToolService:
    def __init__(self, session: Session, user: User, settings):
        self.session, self.user, self.settings = session, user, settings

    def _metric(self, code: str) -> tuple[Metric, MetricVersion]:
        access.require_role(self.session, self.user, "analyst", "steward", "reviewer")
        metric, version = service.visible_metric(self.session, code, access.domain_ids(self.session, self.user))
        if metric is None or version is None:
            raise NotFound("指标不存在或无权访问")
        return metric, version

    def _sources(self, version: MetricVersion, seen: set[str] | None = None) -> dict[str, Asset]:
        """定义也不能泄露不可见来源；沿页面问数使用的上游闭包逐项检查。"""
        seen = seen if seen is not None else set()
        if version.id in seen:
            raise Invalid("口径依赖存在循环")
        seen.add(version.id)
        asset_ids = [version.base_asset_id] if version.base_asset_id else []
        asset_ids += [d["asset_id"] for d in version.dependency or [] if d.get("asset_id")]
        for join in version.joins or []:
            path = self.session.get(JoinPath, join.get("join_path_id"))
            if path:
                asset_ids += [path.left_asset_id, path.right_asset_id]
        sources = service.upstream_closure(self.session, asset_ids)
        for component in version.components or []:
            if not access.can_see_object(self.session, self.user, "metric", component["metric_id"]):
                raise NotFound("口径来源不存在或无权访问")
            child = self.session.get(MetricVersion, component["version_id"])
            if child is None:
                raise NotFound("口径来源不存在或无权访问")
            sources.update(self._sources(child, seen))
        seen.remove(version.id)
        if any(not access.can_read_asset(self.session, self.user, asset) for asset in sources.values()):
            raise NotFound("口径来源不存在或无权访问")
        return sources

    def search_metrics(self, keyword: str = "", domain_id: str | None = None, limit: int = 50) -> list[dict]:
        if not 1 <= limit <= 100:
            raise Invalid("limit 必须在 1 到 100 之间")
        out = []
        for metric, version, domain in service.metric_catalog_rows(
            self.session, self.user, keyword=keyword, domain_id=domain_id, limit=limit, published_only=True
        ):
            context = metrics.metric_ai_context(metric)
            out.append(
                {
                    "code": metric.code,
                    "name": metric.name,
                    "domain": {"id": domain.id, "code": domain.code, "name": domain.name},
                    "description": version.description[:300],
                    "ai_context": {
                        "instructions": (context.get("instructions") or "")[:300] or None,
                        "synonyms": context.get("synonyms", []),
                    },
                }
            )
        return out

    def get_metric(self, metric: str) -> dict:
        definition, version = self._metric(metric)
        sources = self._sources(version)
        return {
            **metrics.describe_version(self.session, version),
            "ai_context": metrics.metric_ai_context(definition),
            "sources": [
                {"id": a.id, "qualified": f"{a.catalog_name}.{a.db_name}.{a.name}", "domain_id": a.domain_id}
                for a in sources.values()
            ],
        }

    def ask_metric(
        self,
        metric: str | None = None,
        start_date: date | None = None,
        end_date: date | None = None,
        dimensions: list[str] | None = None,
        filters: list[DimensionFilter] | None = None,
        compare: Literal["previous_period", "previous_calendar", "previous_year", "custom"] | None = None,
        compare_start: date | None = None,
        compare_end: date | None = None,
        time_grain: Literal["day", "week", "month", "quarter", "year"] | None = None,
        limit: int | None = None,
        metrics: list[str] | str | None = None,
        order: Literal["asc", "desc"] | None = None,
    ) -> dict:
        if start_date is None or end_date is None:
            raise Invalid("必须提供 start_date 与 end_date")
        if start_date > end_date:
            raise Invalid("开始日期晚于结束日期")
        explicit_limit = limit
        limit = min(200, self.settings.query_max_rows) if limit is None else limit
        if limit < 1 or limit > self.settings.query_max_rows:
            raise Invalid("结果行数不能超过页面问数上限", max_rows=self.settings.query_max_rows)
        if order is not None:
            if order not in ("asc", "desc"):
                raise Invalid("排序方向只能为 asc 或 desc")
            if not (dimensions or time_grain):
                raise Invalid("排序需要分组维度（dimensions）或时间粒度（time_grain）")
        codes = _normalize_metric_codes(metric, metrics)
        parsed_filters = _parse_filters(filters)
        resolved_metrics: list[tuple[str, Metric, MetricVersion]] = []
        for code in codes:
            definition, version = self._metric(code)
            self._sources(version)
            resolved_metrics.append((code, definition, version))
        allowed = modelcfg.policy(self.session)["send_results"]
        alignment: AlignmentResult | None = None
        if parsed_filters and allowed and all(version.status == "published" for _, _, version in resolved_metrics):
            shared_dims = set.intersection(
                *(
                    {d["name"] for d in (version.dimensions or []) if isinstance(d, dict) and "name" in d}
                    for _, _, version in resolved_metrics
                )
            )
            if all(item.dimension in shared_dims for item in parsed_filters):
                alignment = _align_metric_filters(
                    self.session,
                    self.user,
                    [version for _, _, version in resolved_metrics],
                    parsed_filters,
                    self.settings,
                )
        intent_limit = (
            explicit_limit
            if (order is not None and explicit_limit is not None and explicit_limit <= MAX_LIMIT)
            else None
        )
        try:
            intent = Intent(
                metric_codes=codes,
                start_date=start_date,
                end_date=end_date,
                group_by=dimensions or [],
                filters=alignment.filters if alignment else parsed_filters,
                order=order,
                limit=intent_limit,
                compare=compare,
                compare_start=compare_start,
                compare_end=compare_end,
                time_grain=time_grain,
            )
        except ValidationError as exc:
            raise Invalid(exc.errors()[0]["msg"] if exc.errors() else "问数意图无效") from exc
        catalog = service.Catalog([], {code: (definition, version) for code, definition, version in resolved_metrics})
        evidence: dict = {}
        if alignment and alignment.explanations:
            evidence["filter_alignment"] = alignment.explanations
        if alignment and alignment.unknown:
            evidence["filter_alignment_unknown"] = alignment.unknown
        answer = Answer(
            question="MCP structured metric request",
            asked_by=self.user.id,
            status="error",
            model_adapter="mcp",
            intent={},
            plan={},
            evidence=evidence,
            result={},
            metric_version_ids=[],
            freshness={},
            steps=[],
            cache_hit=False,
        )
        alignment_evidence = {
            key: answer.evidence[key]
            for key in ("filter_alignment", "filter_alignment_unknown")
            if key in answer.evidence
        }
        service.fulfil(
            self.session,
            self.user,
            intent,
            answer,
            catalog,
            settings=self.settings,
            execute=allowed,
        )
        answer.evidence.update(alignment_evidence)
        metric_refs = [
            {"code": definition.code, "version_id": version.id, "version_no": version.version_no}
            for _, definition, version in resolved_metrics
        ]
        response: dict = {
            "status": answer.status,
            "message": answer.message,
            "error_code": answer.error_code,
            "metric": metric_refs[0],
            "metrics": metric_refs,
            "sql": answer.sql,
            "plan": answer.plan,
            "freshness": answer.freshness,
            "max_rows": min(limit, self.settings.query_max_rows),
            "data_returned": allowed and answer.status == "answered",
        }
        if alignment and (alignment.explanations or alignment.unresolved or alignment.unknown):
            response["filter_alignment"] = {
                "explanations": alignment.explanations,
                "unresolved": alignment.unresolved,
                "unknown": alignment.unknown,
            }
        warnings = []
        if alignment and alignment.unresolved:
            for item in alignment.unresolved:
                candidates = "、".join(item.get("candidates", [])) or "无"
                hint = f"维度 {item['dimension']} 的取值 {item['original']} 尚未确定；候选：{candidates}"
                if not item.get("has_dimension_table"):
                    hint += "（可在维表中维护别名并建立关联路径）"
                warnings.append(hint)
        if answer.evidence.get("warning"):
            warnings.append(answer.evidence["warning"])
        if warnings:
            response["warnings"] = warnings
        if allowed and answer.status == "answered":
            raw_rows = answer.result.get("rows", [])
            client_truncated = len(raw_rows) > limit
            result = dict(answer.result)
            result["rows"] = raw_rows[:limit]
            result["row_count"] = len(result["rows"])
            result["truncated"] = bool(result.get("truncated") or client_truncated)
            if result["truncated"]:
                result["insights"] = []
            if client_truncated:
                msg_intent = (
                    intent
                    if (intent.group_by or len(result["rows"]) != 1)
                    else intent.model_copy(update={"group_by": [intent.time_grain or "_"]})
                )
                first_version = catalog.versions.get(intent.metric_code or "", (None, None))[1]
                if intent.is_simple and (first_version is None or first_version.kind != "derived"):
                    first_metric, metric_version = catalog.versions[intent.metric_code]
                    response["message"] = service._explain(first_metric, metric_version, msg_intent, result)
                else:
                    requested = [(code, *catalog.versions[code]) for code in intent.metric_codes]
                    response["message"] = service._explain_plan(requested, msg_intent, result)
            response["result"] = result
        elif not allowed:
            definitions = [self.get_metric(code) for code in codes]
            response["definition"] = definitions[0]
            response["definitions"] = definitions
        return response

    def lineage_neighbors(
        self,
        asset_id: str,
        column: str | None = None,
        direction: Literal["upstream", "downstream", "both"] = "both",
        depth: int = 1,
    ) -> dict:
        asset = self.session.get(Asset, asset_id)
        if asset is None or not access.can_read_asset(self.session, self.user, asset):
            raise NotFound("资产不存在或无权访问")
        if column and not self.session.scalar(
            select(AssetColumn.id).where(
                AssetColumn.asset_id == asset_id,
                AssetColumn.name == column,
                AssetColumn.status == "active",
            )
        ):
            raise NotFound("字段不存在或无权访问")
        if not 1 <= depth <= 5:
            raise Invalid("depth 必须在 1 到 5 之间")
        if direction not in ("upstream", "downstream", "both"):
            raise Invalid("未知血缘方向")
        nodes, edges, frontier, visited = {asset.id: asset}, {}, {(asset_id, column)}, set()
        for _ in range(depth):
            next_frontier = set()
            for current, current_col in frontier - visited:
                visited.add((current, current_col))
                conditions = []
                if direction in ("upstream", "both"):
                    cond = LineageEdge.target_asset_id == current
                    conditions.append(cond & (LineageEdge.target_column == current_col) if current_col else cond)
                if direction in ("downstream", "both"):
                    cond = LineageEdge.source_asset_id == current
                    conditions.append(cond & (LineageEdge.source_column == current_col) if current_col else cond)
                query = select(LineageEdge).where(LineageEdge.status == "active", or_(*conditions))
                for edge in self.session.scalars(query):
                    source = self.session.get(Asset, edge.source_asset_id) if edge.source_asset_id else None
                    target = self.session.get(Asset, edge.target_asset_id) if edge.target_asset_id else None
                    if (
                        not source
                        or not target
                        or any(not access.can_read_asset(self.session, self.user, node) for node in (source, target))
                    ):
                        continue
                    nodes.update({source.id: source, target.id: target})
                    edges[edge.id] = {
                        "id": edge.id,
                        "source_asset_id": source.id,
                        "source_column": edge.source_column,
                        "target_asset_id": target.id,
                        "target_column": edge.target_column,
                        "relation": edge.relation,
                        "origin": edge.origin,
                    }
                    if target.id == current and direction in ("upstream", "both"):
                        next_frontier.add((source.id, edge.source_column if current_col else None))
                    if source.id == current and direction in ("downstream", "both"):
                        next_frontier.add((target.id, edge.target_column if current_col else None))
            frontier = next_frontier
        return {
            "nodes": [
                {"id": a.id, "qualified": f"{a.catalog_name}.{a.db_name}.{a.name}", "domain_id": a.domain_id}
                for a in nodes.values()
            ],
            "edges": list(edges.values()),
        }

    def list_changes(self, object_type: Literal["asset", "metric"], object_id: str, limit: int = 20) -> list[dict]:
        if not 1 <= limit <= 100:
            raise Invalid("limit 必须在 1 到 100 之间")
        if object_type == "metric":
            metric = self.session.get(Metric, object_id) or self.session.scalar(
                select(Metric).where(Metric.code == object_id)
            )
            if metric is None or not access.can_see_object(self.session, self.user, "metric", metric.id):
                raise NotFound("指标不存在或无权访问")
            versions = set(self.session.scalars(select(MetricVersion.id).where(MetricVersion.metric_id == metric.id)))
            matching = select(Impact.change_set_id).where(
                Impact.object_type == "metric_version", Impact.object_id.in_(versions)
            )
        elif object_type == "asset":
            if not access.can_see_object(self.session, self.user, "asset", object_id):
                raise NotFound("资产不存在或无权访问")
            matching = select(ChangeEvent.change_set_id).where(ChangeEvent.asset_id == object_id)
        else:
            raise Invalid("未知对象类型")
        query = select(ChangeSet).where(ChangeSet.id.in_(matching)).order_by(ChangeSet.created_at.desc()).limit(limit)
        out = []
        for change in self.session.scalars(query):
            data = change_set_view(self.session, change, full=True, user=self.user)
            out.append(
                {
                    "id": data["id"],
                    "status": data["status"],
                    "created_at": data["created_at"],
                    "effective_window": data["effective_window"],
                    "events": [
                        {key: event[key] for key in ("id", "kind", "asset_id", "asset", "column")}
                        for event in data["events"]
                        if object_type != "asset" or event["asset_id"] == object_id
                    ],
                    "impacts": [
                        self._impact(i)
                        for i in data["impacts"]
                        if object_type != "metric" or i["object_id"] in versions
                    ],
                }
            )
        return out

    def _impact(self, impact: dict) -> dict:
        data = {key: impact[key] for key in ("id", "object_type", "object_id", "level", "reason")}
        # reason 是持久化自由文本，可能包含跨域依赖的名称；目标可见不代表来源可见。
        restricted = False
        if impact["object_type"] == "metric_version":
            version = self.session.get(MetricVersion, impact["object_id"])
            try:
                if version is None:
                    restricted = True
                else:
                    self._sources(version)
            except GovernanceError:
                restricted = True
        elif impact["object_type"] == "asset":
            restricted = any(
                not access.can_read_asset(self.session, self.user, source)
                for source in service.upstream_closure(self.session, [impact["object_id"]]).values()
            )
        if restricted:
            data.update(reason=None, reason_redacted=True)
        return data
