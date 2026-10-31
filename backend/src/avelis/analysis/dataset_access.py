"""历史数据集按阅读者当前权限复查实际来源（ADR 0009）。"""

from __future__ import annotations

from collections.abc import Mapping

from sqlalchemy import select
from sqlalchemy.orm import Session

from avelis.ask import service as ask_service
from avelis.governance import access
from avelis.governance.errors import Forbidden, GovernanceError
from avelis.models import AnalysisDataset, Answer, Asset, Metric, MetricVersion, User


def can_read_dataset(session: Session, user: User, dataset: AnalysisDataset,
                     datasets: Mapping[str, AnalysisDataset] | None = None) -> bool:
    """检查保存版本与输入来源，不用最新版本替代历史上实际读过的来源。"""
    allowed = access.domain_ids(session, user)
    known = datasets
    checked: dict[str, bool] = {}
    versions_checked: dict[str, bool] = {}

    def assets_visible(ids: set[str]) -> bool:
        if any(session.get(Asset, asset_id, populate_existing=True) is None for asset_id in ids):
            return False
        closure = ask_service.upstream_closure(session, list(ids))
        return all(access.can_read_asset(session, user,
                                         session.get(Asset, asset_id, populate_existing=True), allowed)
                   for asset_id in closure)

    def version_visible(version_id: str) -> bool:
        if version_id in versions_checked:
            return versions_checked[version_id]
        versions_checked[version_id] = False
        version = session.get(MetricVersion, version_id, populate_existing=True)
        metric = session.get(Metric, version.metric_id, populate_existing=True) if version else None
        if metric is None or metric.domain_id not in allowed:
            return False
        if version.kind == "derived":
            result = all(version_visible(c["version_id"]) for c in version.components or [])
        else:
            try:
                result = assets_visible(set(ask_service.version_sources(session, version)))
            except GovernanceError:
                result = False
        versions_checked[version_id] = result
        return result

    def context_visible(d: AnalysisDataset, meta: dict) -> bool:
        if "asset_ids" in meta or "metric_version_ids" in meta:
            if d.rows and not (meta.get("asset_ids") or meta.get("metric_version_ids")):
                return False
            for version_id in meta.get("metric_version_ids") or []:
                version = session.get(MetricVersion, version_id, populate_existing=True)
                metric = session.get(Metric, version.metric_id, populate_existing=True) if version else None
                if metric is None or metric.domain_id not in allowed:
                    return False
            return assets_visible(set(meta.get("asset_ids") or []))
        # 旧上下文只保存显示名：按当时固定列结构定位，找不到来源时不交付历史行。
        rows = [dict(zip(d.columns, row)) for row in d.rows]
        asset_names = {r["base_asset"] for r in rows} if "base_asset" in d.columns else {
            r["object"] for r in rows if r.get("type") == "元数据变更"}
        metric_names = {r["object"] for r in rows if r.get("type") == "口径版本流转"}
        if rows and not asset_names and not metric_names:
            return False
        ids: set[str] = set()
        for name in asset_names:
            matches = list(session.scalars(select(Asset).where(
                Asset.catalog_name + "." + Asset.db_name + "." + Asset.name == name)))
            if not matches:
                return False
            ids.update(a.id for a in matches)
        for name in metric_names:
            metrics = list(session.scalars(select(Metric).where(Metric.name == name)))
            if not metrics or any(session.get(Metric, m.id, populate_existing=True).domain_id not in allowed
                                  for m in metrics):
                return False
        return assets_visible(ids)

    def visible(d: AnalysisDataset) -> bool:
        nonlocal known
        key = d.id or f"D{d.seq}"
        if key in checked:
            return checked[key]
        checked[key] = False
        meta = d.meta or {}
        parents = set(d.inputs or []) | set(meta.get("closure") or [])
        if parents:
            if known is None:
                known = {f"D{item.seq}": item for item in session.scalars(select(AnalysisDataset).where(
                    AnalysisDataset.run_id == d.run_id))}
            if any(ref not in known or not visible(known[ref]) for ref in parents):
                return False
        if d.kind == "governed":
            answer = session.get(Answer, d.answer_id) if d.answer_id else None
            ids = set(meta.get("metric_version_ids") or []) | set((meta.get("versions") or {}).values())
            if answer is not None:
                ids.update(answer.metric_version_ids or [])
            sources = {s["asset_id"] for s in (answer.evidence or {}).get("sources", [])
                       if s.get("asset_id")} if answer else set()
            result = bool(ids) and all(version_visible(v) for v in ids) and assets_visible(sources)
        elif d.kind in ("explore", "ungoverned"):
            ids = {table["asset_id"] for table in meta.get("tables") or [] if table.get("asset_id")}
            result = bool(ids) and assets_visible(ids | set(meta.get("upstream_asset_ids") or []))
        elif d.kind == "context":
            result = context_visible(d, meta)
        else:
            result = d.kind == "calc" and bool(parents)
        checked[key] = result
        return result

    return visible(dataset)


def require_dataset_access(session: Session, user: User, dataset: AnalysisDataset,
                           datasets: Mapping[str, AnalysisDataset] | None = None) -> None:
    if not can_read_dataset(session, user, dataset, datasets):
        raise Forbidden("你已没有该数据的访问权限")
