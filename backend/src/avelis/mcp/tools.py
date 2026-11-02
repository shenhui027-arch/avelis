"""MCP 工具只组装既有治理与问数服务，不接受调用方提供的 SQL。"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import date
from typing import Literal

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from avelis import modelcfg
from avelis.api.governance import change_set_view
from avelis.ask import service
from avelis.ask.agent import Toolbox
from avelis.ask.intent import DimensionFilter, Intent
from avelis.governance import access, metrics
from avelis.governance.errors import GovernanceError, Invalid, NotFound
from avelis.models import (
    Answer,
    Asset,
    AssetColumn,
    ChangeEvent,
    ChangeSet,
    Domain,
    Impact,
    JoinPath,
    LineageEdge,
    Metric,
    MetricVersion,
    User,
)


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
        catalog = service.visible_catalog(self.session, self.user, with_values=False)
        # 与页面问数使用相同的名称、编码、别名、ai_context 同义词和问法匹配。
        matches = json.loads(Toolbox(self.session, self.user, catalog).search_metrics(keyword))
        out = []
        for item in matches:
            metric, version = catalog.versions[item["code"]]
            if version.status != "published" or (domain_id and metric.domain_id != domain_id):
                continue
            domain = self.session.get(Domain, metric.domain_id)
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
            if len(out) == limit:
                break
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
        metric: str,
        start_date: date,
        end_date: date,
        dimensions: list[str] | None = None,
        filters: list[DimensionFilter] | None = None,
        compare: Literal["previous_period", "previous_calendar", "previous_year", "custom"] | None = None,
        compare_start: date | None = None,
        compare_end: date | None = None,
        time_grain: Literal["day", "week", "month", "quarter", "year"] | None = None,
        limit: int | None = None,
    ) -> dict:
        limit = min(200, self.settings.query_max_rows) if limit is None else limit
        if limit < 1 or limit > self.settings.query_max_rows:
            raise Invalid("结果行数不能超过页面问数上限", max_rows=self.settings.query_max_rows)
        if start_date > end_date:
            raise Invalid("开始日期晚于结束日期")
        definition, version = self._metric(metric)
        self._sources(version)
        intent = Intent(
            metric_code=metric,
            start_date=start_date,
            end_date=end_date,
            group_by=dimensions or [],
            filters=filters or [],
            compare=compare,
            compare_start=compare_start,
            compare_end=compare_end,
            time_grain=time_grain,
        )
        catalog = service.Catalog([], {metric: (definition, version)})
        answer = Answer(
            question="MCP structured metric request",
            asked_by=self.user.id,
            status="error",
            model_adapter="mcp",
            intent={},
            plan={},
            evidence={},
            result={},
            metric_version_ids=[],
            freshness={},
            steps=[],
            cache_hit=False,
        )
        allowed = modelcfg.policy(self.session)["send_results"]
        service.fulfil(
            self.session,
            self.user,
            intent,
            answer,
            catalog,
            settings=replace(self.settings, query_max_rows=min(limit, self.settings.query_max_rows)),
            execute=allowed,
        )
        response = {
            "status": answer.status,
            "message": answer.message,
            "error_code": answer.error_code,
            "metric": {"code": definition.code, "version_id": version.id, "version_no": version.version_no},
            "sql": answer.sql,
            "plan": answer.plan,
            "freshness": answer.freshness,
            "max_rows": min(limit, self.settings.query_max_rows),
            "data_returned": allowed and answer.status == "answered",
        }
        if allowed and answer.status == "answered":
            result = dict(answer.result)
            result["rows"] = result.get("rows", [])[:limit]
            result["row_count"] = len(result["rows"])
            result["truncated"] = bool(result.get("truncated") or len(answer.result.get("rows", [])) > limit)
            response["result"] = result
        elif not allowed:
            response["definition"] = self.get_metric(metric)
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
