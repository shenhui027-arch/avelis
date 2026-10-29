"""指标与指标版本：定义校验、依赖指纹、发布前检查（含粒度放大）、状态机、禁止自审。

状态与合法转换（谁可以做）：
  draft ──submit（域负责人/维护者）──▶ pending_review ──approve（复核者，非提交人/创建人）──▶ published
  pending_review ──reject（复核者）──▶ rejected
  published / pending_review ──（系统：依赖指纹变化）──▶ affected
  affected ──复核：restored（指纹已与本版本一致）──▶ published
  affected ──复核：new_version / retired ──▶ retired
  published ──archive（域负责人）或被同指标新版本取代──▶ archived
问数只使用 published 版本，并在执行前再次核对依赖指纹（两条状态线：观察到的事实与业务批准）。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from avelis import audit, cache
from avelis.collect.service import current_fingerprints
from avelis.config import get_settings
from avelis.connectors.base import sha256_json
from avelis.db import utcnow
from avelis.engines import engine_of
from avelis.governance import access
from avelis.governance.errors import Conflict, Forbidden, Invalid, NotFound
from avelis.governance.metric_sql import BASE_ALIAS, column_ref, is_additive, parse_snippet
from avelis.lineage.grain import cardinality, effective_grains
from avelis.models import (
    Asset,
    AssetVersion,
    ColumnVersion,
    Datasource,
    Domain,
    JoinPath,
    Metric,
    MetricVersion,
    MetricVersionTransition,
    User,
)

TRANSITIONS = {
    ("draft", "pending_review"),
    ("pending_review", "published"),
    ("pending_review", "rejected"),
    ("pending_review", "affected"),
    ("published", "affected"),
    ("published", "archived"),
    ("affected", "published"),
    ("affected", "pending_review"),  # 待批准时受影响：恢复后回到待批准，仍须正式复核
    ("affected", "retired"),
}
EDITABLE_FIELDS = ("description", "base_asset_id", "expression", "time_column", "timezone", "filters",
                   "dimensions", "joins", "grain", "dedupe_basis")
TIME_TYPES = ("date", "datetime", "timestamp")


def transition(session: Session, version: MetricVersion, to_status: str, actor: User | None, reason: str) -> None:
    if (version.status, to_status) not in TRANSITIONS:
        raise Conflict(f"不允许从 {version.status} 转为 {to_status}", from_status=version.status, to_status=to_status)
    session.add(MetricVersionTransition(metric_version_id=version.id, from_status=version.status, to_status=to_status,
                                        actor_id=actor.id if actor else None, reason=reason))
    version.status = to_status
    version.status_reason = reason
    if to_status in ("affected", "retired", "archived"):
        cache.invalidate_tag(f"metric_version:{version.id}")


# --- 指标 -------------------------------------------------------------------------

def create_metric(session: Session, actor: User, *, code: str, name: str, domain_id: str, owner_id: str | None,
                  aliases: list[str]) -> Metric:
    access.require_role(session, actor, "steward")
    if session.get(Domain, domain_id) is None:
        raise NotFound("业务域不存在")
    access.require_domain_maintainer(session, actor, domain_id)
    if session.scalar(select(Metric.id).where(Metric.code == code)):
        raise Conflict(f"指标编码 {code} 已存在")
    owner_id = owner_id or session.get(Domain, domain_id).owner_id
    metric = Metric(code=code, name=name, domain_id=domain_id, owner_id=owner_id, aliases=aliases,
                    created_by=actor.id)
    session.add(metric)
    session.flush()
    audit.record(session, actor, "metric.create", "metric", metric.id, code=code, name=name, owner_id=owner_id)
    return metric


def create_version(session: Session, actor: User, metric: Metric, spec: dict[str, Any]) -> MetricVersion:
    access.require_domain_maintainer(session, actor, metric.domain_id)
    number = (session.scalar(select(func.max(MetricVersion.version_no))
                             .where(MetricVersion.metric_id == metric.id)) or 0) + 1
    kind = spec.get("kind") or "base"
    if kind not in ("base", "derived"):
        raise Invalid("指标类型只能是 base（基础）或 derived（派生）")
    version = MetricVersion(metric_id=metric.id, version_no=number, status="draft", created_by=actor.id, kind=kind,
                            timezone=spec.get("timezone") or get_settings().business_timezone,
                            **{k: spec[k] for k in EDITABLE_FIELDS if k in spec and k != "timezone"})
    if kind == "derived":
        version.time_column = ""
        version.base_asset_id = None
    resolved = validate_spec(session, version)
    if kind == "derived":
        version.dimensions = resolved["dimensions"]
    version.dependency = resolved["dependency"]
    session.add(version)
    session.flush()
    session.add(MetricVersionTransition(metric_version_id=version.id, from_status=None, to_status="draft",
                                        actor_id=actor.id, reason="创建草稿"))
    audit.record(session, actor, "metric_version.create", "metric_version", version.id, metric=metric.code,
                 version_no=number)
    return version


def update_draft(session: Session, actor: User, version: MetricVersion, spec: dict[str, Any]) -> MetricVersion:
    metric = session.get(Metric, version.metric_id)
    access.require_domain_maintainer(session, actor, metric.domain_id)
    if version.status not in ("draft", "rejected"):
        raise Conflict("只有草稿或被驳回的版本可以修改；已发布版本请新建版本")
    for key in EDITABLE_FIELDS:
        if key in spec:
            value = spec[key]
            if key == "timezone" and not value:
                value = get_settings().business_timezone
            setattr(version, key, value)
    if version.status == "rejected":
        transition_rejected_to_draft(session, version, actor)
    resolved = validate_spec(session, version)
    version.dependency = resolved["dependency"]
    if version.kind == "derived":
        version.dimensions = resolved["dimensions"]
    audit.record(session, actor, "metric_version.update", "metric_version", version.id, fields=sorted(spec))
    return version


def transition_rejected_to_draft(session: Session, version: MetricVersion, actor: User) -> None:
    session.add(MetricVersionTransition(metric_version_id=version.id, from_status="rejected", to_status="draft",
                                        actor_id=actor.id, reason="修改被驳回的版本"))
    version.status = "draft"
    # 被驳回期间不受保留期保护：引用的资产版本可能已被清理。不留悬空引用，提交时按当前版本重新绑定（ADR 0008）
    if _purged_refs(session, version.asset_versions):
        version.asset_versions = {}
        version.status_reason = "引用的资产版本已按保留期清理：草稿不再绑定旧版本，重新提交时按当前版本绑定"
    # 派生口径经 components 固定的组成版本，间接引用它们的资产版本：组成版本不在了或其资产版本被清理，同样不留悬空引用
    components = [c for c in (version.components or []) if isinstance(c, dict)]
    if any((cv := session.get(MetricVersion, c.get("version_id"))) is None or _purged_refs(session, cv.asset_versions)
           for c in components if c.get("version_id")):
        version.components = []
        version.status_reason = ("组成口径版本引用的资产版本已按保留期清理：草稿不再固定旧的组成版本，"
                                 "需要重新提交以重新固定")


def _purged_refs(session: Session, asset_versions: dict[str, Any] | None) -> bool:
    refs = [v.get("version_id") for v in (asset_versions or {}).values() if isinstance(v, dict)]
    return any(ref and session.get(AssetVersion, ref) is None for ref in refs)


# --- 定义校验与依赖 -----------------------------------------------------------------

def _asset_columns(session: Session, asset: Asset) -> dict[str, ColumnVersion]:
    return {c.name: c for c in session.scalars(select(ColumnVersion).where(
        ColumnVersion.asset_version_id == asset.current_version_id))}


def resolve_joins(session: Session, version: MetricVersion) -> tuple[dict[str, Asset], list[dict[str, Any]]]:
    """返回 别名 → 资产，以及逐步的关联信息（含路径、方向、是否先聚合）。"""
    base = session.get(Asset, version.base_asset_id)
    if base is None or base.status != "active":
        raise Invalid("基础资产不存在或已删除", base_asset_id=version.base_asset_id)
    aliases: dict[str, Asset] = {BASE_ALIAS: base}
    steps = []
    for i, join in enumerate(version.joins or []):
        alias, path_id = join.get("alias"), join.get("join_path_id")
        left_alias = join.get("from", BASE_ALIAS)
        if not alias or alias in aliases or not alias.isidentifier():
            raise Invalid(f"第 {i + 1} 个关联的别名不合法或重复：{alias}")
        if left_alias not in aliases:
            raise Invalid(f"第 {i + 1} 个关联引用了未定义的别名 {left_alias}")
        path = session.get(JoinPath, path_id)
        if path is None:
            raise Invalid(f"关联路径不存在：{path_id}")
        if path.left_asset_id != aliases[left_alias].id:
            raise Invalid(f"关联路径 {path.code} 的左侧不是 {left_alias}")
        right = session.get(Asset, path.right_asset_id)
        if right is None or right.status != "active":
            raise Invalid(f"关联路径 {path.code} 的右侧资产已删除")
        aliases[alias] = right
        steps.append({"alias": alias, "from": left_alias, "path": path, "right": right,
                      "pre_aggregate": bool(join.get("pre_aggregate"))})
    return aliases, steps


def derived_components(session: Session, version: MetricVersion) -> list[dict[str, Any]]:
    """派生指标公式引用的组成指标，以及各自当前进入过发布流程的版本（可能为空）。"""
    from avelis.governance.derived import parse_formula

    own = session.get(Metric, version.metric_id)
    out = []
    for code in parse_formula(version.expression):
        metric = session.scalar(select(Metric).where(Metric.code == code))
        if metric is None:
            raise Invalid(f"公式引用了不存在的指标 {code}")
        if metric.id == own.id:
            raise Invalid("派生指标不能引用自身")
        current = latest_governed_version(session, metric)
        out.append({"metric": metric, "version": current})
    return out


def _shared_dimensions(components: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """组成指标共有的维度（按维度名）；只有共有维度能用于派生指标的过滤、分组。"""
    lists = [c["version"].dimensions or [] for c in components if c["version"] is not None]
    if not lists:
        return []
    names = set.intersection(*({d["name"] for d in dims} for dims in lists))
    return [{"name": d["name"], "label": d.get("label") or d["name"]} for d in lists[0] if d["name"] in names]


def validate_spec(session: Session, version: MetricVersion) -> dict[str, Any]:
    if version.kind == "derived":
        components = derived_components(session, version)
        return {"aliases": {}, "steps": [], "dependency": [], "expression": None, "components": components,
                "dimensions": _shared_dimensions(components)}
    aliases, steps = resolve_joins(session, version)
    names = set(aliases)
    dialect = engine_of(session.get(Datasource, aliases[BASE_ALIAS].datasource_id)).sql.name
    columns = {alias: _asset_columns(session, asset) for alias, asset in aliases.items()}
    # 先聚合的关联只暴露关联键与 row_count
    for step in steps:
        if step["pre_aggregate"]:
            keep = {c: columns[step["alias"]][c] for c in step["path"].right_columns}
            columns[step["alias"]] = keep
    refs: list[tuple[str, str, str]] = []  # (alias, column, role)

    def check(alias: str, column: str, label: str, role: str) -> None:
        if column == "row_count" and any(s["alias"] == alias and s["pre_aggregate"] for s in steps):
            return
        if column not in columns[alias]:
            asset = aliases[alias]
            raise Invalid(f"{label}引用的列不存在：{asset.db_name}.{asset.name}.{column}")
        refs.append((alias, column, role))

    expression = parse_snippet(version.expression or "", dialect=dialect, label="表达式", aliases=names,
                               require_aggregate=True)
    for alias, column in expression.columns:
        check(alias, column, "表达式", "measure")
    if version.filters:
        for alias, column in parse_snippet(version.filters, dialect=dialect, label="过滤条件", aliases=names,
                                           require_aggregate=False).columns:
            check(alias, column, "过滤条件", "filter")
    time_alias, time_col = column_ref(version.time_column or "", dialect=dialect, label="时间字段", aliases=names)
    check(time_alias, time_col, "时间字段", "time")
    if not columns[time_alias][time_col].data_type.lower().startswith(TIME_TYPES):
        raise Invalid(f"时间字段必须是日期或时间类型：{time_col}（{columns[time_alias][time_col].data_type}）")
    try:
        ZoneInfo(version.timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise Invalid(f"业务时区无效：{version.timezone}") from exc
    seen_dims = set()
    for dim in version.dimensions or []:
        if not dim.get("name") or dim["name"] in seen_dims:
            raise Invalid(f"维度名称缺失或重复：{dim}")
        seen_dims.add(dim["name"])
        alias, column = column_ref(dim.get("column", ""), dialect=dialect, label=f"维度 {dim['name']} ",
                                   aliases=names)
        check(alias, column, f"维度 {dim['name']} ", "dimension")
    for step in steps:
        for c in step["path"].left_columns:
            check(step["from"], c, "关联键", "join_key")
        for c in step["path"].right_columns:
            check(step["alias"], c, "关联键", "join_key")

    fps = current_fingerprints(session)
    dependency = []
    for alias, column, role in sorted(set(refs)):
        asset = aliases[alias]
        fp = fps.get((asset.id, column))
        dependency.append({"alias": alias, "asset_id": asset.id, "column": column, "role": role,
                           "qualified": f"{asset.catalog_name}.{asset.db_name}.{asset.name}",
                           "fingerprint": fp.fingerprint if fp else None,
                           "status": fp.status if fp else "unknown",
                           "reason": fp.reason if fp else "尚未计算推导指纹"})
    return {"aliases": aliases, "steps": steps, "dependency": dependency, "expression": expression}


def dependency_fingerprint(version: MetricVersion, dependency: list[dict[str, Any]]) -> str:
    spec = {k: getattr(version, k) for k in EDITABLE_FIELDS}
    if version.kind == "derived":  # 派生：公式 + 每个组成指标锁定的版本及其依赖指纹
        return sha256_json({"formula": version.expression, "components": [
            [c["metric_code"], c["version_id"], c["dependency_fingerprint"]] for c in dependency]})
    return sha256_json({"spec": spec, "deps": [[d["asset_id"], d["column"], d["fingerprint"]] for d in dependency]})


def dependency_drift(session: Session, version: MetricVersion) -> list[dict[str, Any]]:
    """当前推导指纹与版本绑定的指纹不一致的依赖。为空表示可以按此版本问数。
    派生指标：锁定的组成版本不再是已发布、或其依赖本身已漂移，都算漂移。"""
    if version.kind == "derived":
        drift = []
        for c in version.components or []:
            cv = session.get(MetricVersion, c["version_id"])
            inner = dependency_drift(session, cv) if cv is not None and cv.status == "published" else []
            if cv is None or cv.status != "published" or inner:
                drift.append({"asset_id": None, "column": c["metric_code"], "qualified": f"指标 {c['metric_name']}",
                              "fingerprint": c["dependency_fingerprint"], "current_fingerprint": None,
                              "current_status": cv.status if cv else "missing",
                              "current_reason": f"组成指标 V{c['version_no']} 当前为 {cv.status if cv else '不存在'}"})
        return drift
    fps = current_fingerprints(session)
    drift = []
    for dep in version.dependency or []:
        fp = fps.get((dep["asset_id"], dep["column"]))
        current, status = (fp.fingerprint, fp.status) if fp else (None, "broken")
        if current != dep["fingerprint"] or status != "ok":
            drift.append({**dep, "current_fingerprint": current, "current_status": status,
                          "current_reason": fp.reason if fp else "字段或资产已不存在"})
    return drift


def publish_check(session: Session, version: MetricVersion, viewer: User) -> dict[str, Any]:
    """发布前检查：依赖证据完整、关联路径已批准、关联不放大可加度量、粒度说明。
    viewer：查看（或提交、批准）的人；注释缺口提示中只沿用他能看到的上游注释。"""
    resolved = validate_spec(session, version)
    if version.kind == "derived":
        return _derived_check(resolved)
    blockers: list[str] = []
    warnings: list[str] = []
    for dep in resolved["dependency"]:
        if dep["status"] != "ok":
            blockers.append(f"依赖 {dep['qualified']}.{dep['column']} 证据不全（{dep['status']}）："
                            f"{dep['reason'] or '原因未知'}")
    fps = {(d["asset_id"], d["column"]): d["fingerprint"] for d in resolved["dependency"]}
    joins_report = []
    fan_out: list[str] = []  # 会放大可加度量的问题：发布时阻断，问数时按当前粒度重新判定
    additive = is_additive(resolved["expression"].expression)
    if additive and resolved["steps"]:
        outside = sorted({alias for alias, _ in resolved["expression"].columns if alias != BASE_ALIAS})
        if outside:
            fan_out.append(f"可加度量 {version.expression} 引用了关联侧 {'、'.join(outside)} 的列："
                           "关联后按基础表的行重复计数。请把该表作为基础表，或改为在关联侧先聚合后再关联")
    for step in resolved["steps"]:
        path: JoinPath = step["path"]
        from avelis.governance.joins import effectively_approved

        if not effectively_approved(session, path):
            blockers.append(f"关联路径 {path.code} 尚未批准（{path.status}）")
        if step["pre_aggregate"]:
            card_value, basis = "n1", {"basis": f"右侧先按 {', '.join(path.right_columns)} 聚合，粒度即关联键"}
        else:
            card = cardinality(session, path.left_asset_id, path.left_columns, path.right_asset_id,
                               path.right_columns, fingerprints=current_fingerprint_map(session))
            card_value, basis = card.value, card.basis
            if card.basis.get("warning"):
                warnings.append(f"{path.code}：{card.basis['warning']}")
        report = {"alias": step["alias"], "path": path.code, "cardinality": card_value, "basis": basis,
                  "pre_aggregate": step["pre_aggregate"]}
        joins_report.append(report)
        if card_value in ("1n", "nn", "unknown") and additive:
            fan_out.append(
                f"关联 {path.code}（{step['from']} → {step['alias']}）基数为 {_card_label(card_value)}，"
                f"会放大可加度量 {version.expression}，导致重复计数。请先把右侧按关联键聚合（pre_aggregate），"
                f"或改用不放大的关联路径")
    blockers.extend(fan_out)
    # 字段含义以数据库列注释为准：依赖的列缺注释时提示（AVELIS_REQUIRE_COLUMN_COMMENTS=true 时阻断发布）
    from avelis.governance.column_docs import GAPS, column_doc, visible_to

    can_see = visible_to(session, viewer)
    gaps = []
    for alias, column, role in sorted({(d["alias"], d["column"], d["role"]) for d in resolved["dependency"]}):
        doc = column_doc(session, resolved["aliases"][alias], column, can_see=can_see)
        if doc["status"] in GAPS:
            hint = f"（上游 {doc['source']} 有注释，可沿用）" if doc["status"] == "inherited" else ""
            gaps.append(f"字段 {doc['asset'].split('.', 1)[1]}.{column} 缺少数据库注释{hint}")
    (blockers if get_settings().require_column_comments else warnings).extend(gaps)
    base_grains = [g.to_dict() for g in effective_grains(session, version.base_asset_id,
                                                         current_fingerprint_map(session))]
    if not version.grain:
        warnings.append("未填写统计粒度")
    if version.joins and not version.dedupe_basis:
        warnings.append("涉及关联但未说明避免重复计数的依据")
    return {"ok": not blockers, "blockers": blockers, "fan_out": fan_out, "warnings": warnings, "joins": joins_report,
            "base_grains": base_grains, "dependency": resolved["dependency"],
            "checked_at": utcnow().replace(tzinfo=timezone.utc).isoformat(),
            "fingerprints": len(fps)}


def _derived_check(resolved: dict[str, Any]) -> dict[str, Any]:
    """派生指标的发布检查：组成指标都已发布、不嵌套派生；锁定组成版本作为“依赖”。"""
    blockers, warnings, pinned = [], [], []
    for c in resolved["components"]:
        metric, cv = c["metric"], c["version"]
        if cv is None or cv.status != "published":
            blockers.append(f"组成指标 {metric.name}（{metric.code}）当前没有已发布版本"
                            + (f"（V{cv.version_no} 为 {cv.status}）" if cv else ""))
            continue
        if cv.kind == "derived":
            blockers.append(f"组成指标 {metric.name} 本身是派生指标；v0.1 不支持派生嵌套")
            continue
        pinned.append({"metric_code": metric.code, "metric_id": metric.id, "metric_name": metric.name,
                       "version_id": cv.id, "version_no": cv.version_no,
                       "dependency_fingerprint": cv.dependency_fingerprint})
    if not resolved["dimensions"]:
        warnings.append("组成指标没有共有维度：只能按时间范围汇总，不能按维度过滤或分组")
    return {"ok": not blockers, "blockers": blockers, "fan_out": [], "warnings": warnings, "joins": [],
            "base_grains": [], "dependency": pinned, "components": pinned, "dimensions": resolved["dimensions"],
            "checked_at": utcnow().replace(tzinfo=timezone.utc).isoformat(), "fingerprints": 0}


def current_fingerprint_map(session: Session) -> dict[tuple[str, str], str]:
    return {k: v.fingerprint for k, v in current_fingerprints(session).items()}


def _card_label(value: str) -> str:
    return {"1n": "1:N", "nn": "N:N", "unknown": "未知", "n1": "N:1", "11": "1:1"}[value]


# --- 状态转换 ---------------------------------------------------------------------

def submit(session: Session, actor: User, version: MetricVersion) -> MetricVersion:
    metric = session.get(Metric, version.metric_id)
    access.require_domain_maintainer(session, actor, metric.domain_id)
    if version.status != "draft":
        raise Conflict(f"只有草稿可以提交复核（当前 {version.status}）")
    check = publish_check(session, version, actor)
    version.publish_check = check
    version.dependency = check["dependency"]
    if not check["ok"]:
        audit.record(session, actor, "metric_version.submit_blocked", "metric_version", version.id,
                     blockers=check["blockers"])
        raise Conflict("发布前检查未通过", blockers=check["blockers"], check=check)
    version.dependency_fingerprint = dependency_fingerprint(version, check["dependency"])
    if version.kind == "derived":
        version.components = check["components"]
        version.dimensions = check["dimensions"]
        version.dependency = []
        version.asset_versions = {}
    else:
        version.asset_versions = _asset_versions(session, check["dependency"])
    version.submitted_by = actor.id
    version.submitted_at = utcnow()
    transition(session, version, "pending_review", actor, "提交复核")
    audit.record(session, actor, "metric_version.submit", "metric_version", version.id,
                 dependency_fingerprint=version.dependency_fingerprint)
    return version


def _asset_versions(session: Session, dependency: list[dict[str, Any]]) -> dict[str, Any]:
    out = {}
    for dep in dependency:
        asset = session.get(Asset, dep["asset_id"])
        v = session.get(AssetVersion, asset.current_version_id) if asset.current_version_id else None
        out[asset.id] = {"qualified": dep["qualified"], "version_id": v.id if v else None,
                         "version_no": v.version_no if v else None}
    return out


def approve(session: Session, actor: User, version: MetricVersion, note: str | None) -> MetricVersion:
    access.require_role(session, actor, "reviewer")
    if version.status != "pending_review":
        raise Conflict(f"只有待复核的版本可以批准（当前 {version.status}）")
    if not get_settings().allow_self_review and actor.id in (version.submitted_by, version.created_by):
        raise Forbidden("禁止自审：提交人或创建人不能批准同一版本（AVELIS_ALLOW_SELF_REVIEW=false）")
    check = publish_check(session, version, actor)
    if not check["ok"]:
        raise Conflict("发布前检查未通过", blockers=check["blockers"])
    if dependency_fingerprint(version, check["dependency"]) != version.dependency_fingerprint:
        raise Conflict("提交后依赖已变化，请退回维护者重新提交", drift=dependency_drift(session, version))
    metric = session.get(Metric, version.metric_id)
    for previous in session.scalars(select(MetricVersion).where(
            MetricVersion.metric_id == metric.id, MetricVersion.status == "published",
            MetricVersion.id != version.id)):
        transition(session, previous, "archived", actor, f"被 V{version.version_no} 取代")
    version.reviewed_by = actor.id
    version.reviewed_at = utcnow()
    version.review_note = note
    version.published_at = utcnow()
    version.publish_check = check
    transition(session, version, "published", actor, note or "复核通过")
    audit.record(session, actor, "metric_version.approve", "metric_version", version.id, note=note)
    return version


def reject(session: Session, actor: User, version: MetricVersion, note: str) -> MetricVersion:
    access.require_role(session, actor, "reviewer")
    if not get_settings().allow_self_review and actor.id in (version.submitted_by, version.created_by):
        raise Forbidden("禁止自审：提交人或创建人不能驳回同一版本")
    version.reviewed_by = actor.id
    version.reviewed_at = utcnow()
    version.review_note = note
    transition(session, version, "rejected", actor, note)
    audit.record(session, actor, "metric_version.reject", "metric_version", version.id, note=note)
    return version


def archive(session: Session, actor: User, version: MetricVersion, reason: str) -> MetricVersion:
    metric = session.get(Metric, version.metric_id)
    access.require_domain_owner(session, actor, metric.domain_id)
    transition(session, version, "archived", actor, reason)
    audit.record(session, actor, "metric_version.archive", "metric_version", version.id, reason=reason)
    return version


def status_before_affected(session: Session, version: MetricVersion) -> str | None:
    """最近一次进入“受影响”之前的状态（published 或 pending_review）。"""
    return session.scalar(select(MetricVersionTransition.from_status).where(
        MetricVersionTransition.metric_version_id == version.id, MetricVersionTransition.to_status == "affected")
        .order_by(MetricVersionTransition.at.desc()).limit(1))


def mark_affected(session: Session, version: MetricVersion, reason: str) -> bool:
    if version.status not in ("published", "pending_review"):
        return False
    transition(session, version, "affected", None, reason)
    return True


def visible_metrics(session: Session, user: User) -> list[Metric]:
    allowed = access.domain_ids(session, user)
    if not allowed:
        return []
    return list(session.scalars(select(Metric).where(Metric.domain_id.in_(allowed)).order_by(Metric.code)))


def latest_governed_version(session: Session, metric: Metric) -> MetricVersion | None:
    """问数使用的版本：最近一个进入过发布流程的版本（已发布 / 受影响 / 失效）。"""
    return session.scalar(select(MetricVersion).where(
        MetricVersion.metric_id == metric.id,
        MetricVersion.status.in_(("published", "affected", "retired")),
    ).order_by(MetricVersion.version_no.desc()).limit(1))


def metric_or_404(session: Session, metric_id: str) -> Metric:
    metric = session.get(Metric, metric_id)
    if metric is None:
        raise NotFound("指标不存在")
    return metric


def version_or_404(session: Session, version_id: str) -> MetricVersion:
    version = session.get(MetricVersion, version_id)
    if version is None:
        raise NotFound("指标版本不存在")
    return version


def describe_version(session: Session, version: MetricVersion) -> dict[str, Any]:
    metric = session.get(Metric, version.metric_id)
    base = session.get(Asset, version.base_asset_id) if version.base_asset_id else None
    return {
        "kind": version.kind, "components": version.components,
        "id": version.id, "metric_id": metric.id, "metric_code": metric.code, "metric_name": metric.name,
        "version_no": version.version_no, "status": version.status, "status_reason": version.status_reason,
        "description": version.description, "expression": version.expression,
        "base_asset": {"id": base.id, "qualified": f"{base.catalog_name}.{base.db_name}.{base.name}"} if base else None,
        "time_column": version.time_column, "timezone": version.timezone, "filters": version.filters,
        "dimensions": version.dimensions, "joins": version.joins, "grain": version.grain,
        "dedupe_basis": version.dedupe_basis, "dependency": version.dependency,
        "dependency_fingerprint": version.dependency_fingerprint, "asset_versions": version.asset_versions,
        "publish_check": version.publish_check, "created_by": version.created_by,
        "submitted_by": version.submitted_by, "reviewed_by": version.reviewed_by,
        "review_note": version.review_note, "published_at": _iso(version.published_at),
        "created_at": _iso(version.created_at), "owner_id": metric.owner_id, "domain_id": metric.domain_id,
    }


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None
