"""三层预检后的原子导入；只创建草稿，不执行 SQL 或连接数据源。"""

from __future__ import annotations

import json
import re
from collections import deque
from copy import deepcopy
from typing import Any, Literal

import sqlglot
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlglot import exp
from sqlglot.errors import SqlglotError

from avelis import audit
from avelis.collect.service import current_fingerprints
from avelis.config import get_settings
from avelis.engines import engine_of
from avelis.governance import access, metrics
from avelis.governance.derived import parse_formula
from avelis.governance.errors import Forbidden, GovernanceError
from avelis.governance.joins import effectively_approved
from avelis.i18n import gettext
from avelis.models import Asset, Datasource, Domain, JoinPath, Metric, MetricVersion, User

from .expressions import equivalent_expressions, fold_filters, physical_column_name
from .validation import (
    _matching,
    iter_expression_dialects,
    iter_metrics,
    normalize_document,
    validate_model,
    validate_structure,
)


class ImportRejected(GovernanceError):
    """定义错误为 400；权限错误为 403，报告仍包含逐条原因。"""

    def __init__(self, report: dict[str, Any], *, forbidden: bool = False):
        self.status_code = 403 if forbidden else 400
        super().__init__("Ossie 导入校验未通过", **report)


class _FieldUnavailable(ValueError):
    pass


class _Options(BaseModel):
    """只解释已知的 Avelis 扩展字段；其他内容留在原始交换文档中。"""

    model_config = ConfigDict(extra="ignore", strict=True)
    kind: Literal["base", "derived"] = "base"
    name: str | None = Field(default=None, min_length=1, max_length=128)
    aliases: list[str] = Field(default_factory=list)
    base_dataset: str | None = None
    expression: str | None = None
    formula: str | None = None
    time_column: str | None = Field(default=None, max_length=255)
    timezone: str | None = Field(default=None, max_length=64)
    time_format: Literal["yyyymmdd", "yyyy-mm-dd", "yyyymm"] | None = None
    data_grain: Literal["day", "week", "month", "quarter", "year"] = "day"
    time_aggregation: Literal["sum", "last", "avg"] = "sum"
    value_kind: Literal["level", "ratio", "count"] | None = None
    polarity: Literal["up_good", "down_good"] | None = None
    filters: str | None = None
    dimensions: list[dict[str, str | None]] | None = None
    joins: list[dict[str, Any]] | None = None
    components: list[dict[str, Any]] = Field(default_factory=list)
    grain: list[str] = Field(default_factory=list)
    dedupe_basis: str | None = None
    unit: str | None = Field(default=None, max_length=16)
    unit_scale: int = Field(default=1, ge=1)
    material_change_ratio: float | None = Field(default=None, gt=0, lt=1)


def _error(layer: str, path: str, reason: str) -> dict[str, str]:
    return {"layer": layer, "path": path, "reason": reason}


def _reject(report: dict[str, Any], *, forbidden: bool = False) -> None:
    report["ok"] = False
    for result in report["results"]:
        result["action"] = "failed"
        result["reason"] = "文件校验未通过，整体未写入"
    report["counts"] = {"created": 0, "new_version": 0, "skipped": 0, "failed": len(report["results"])}
    raise ImportRejected(report, forbidden=forbidden)


def _options(item: dict[str, Any]) -> _Options:
    data = {}
    for extension in item.get("custom_extensions", []):
        if extension["vendor_name"] == "avelis":
            value = json.loads(extension["data"])
            if not isinstance(value, dict):
                raise ValueError("avelis 扩展 data 必须为 JSON 对象")
            data.update(value)
    return _Options.model_validate(data)


def _read(name: str) -> str | None:
    if name in ("OSSIE_SQL_2026", "ANSI_SQL"):
        return name if name in sqlglot.Dialect.classes else ""
    return name if name in sqlglot.Dialect.classes else None


def _choose(item: dict[str, Any], target: str, *, field: bool = False) -> tuple[str, str | None, bool]:
    dialects = list(iter_expression_dialects(item["expression"]))
    for dialect in dialects:
        if dialect["dialect"] == target:
            return dialect["expression"], target, False
    for dialect in dialects:
        if dialect["dialect"] == "OSSIE_SQL_2026" or (field and dialect["dialect"] == "ANSI_SQL"):
            return dialect["expression"], _read(dialect["dialect"]), True
    return dialects[0]["expression"], None, False


def _tree(text: str, read: str | None) -> exp.Expression | None:
    if read is None:
        return None
    try:
        return sqlglot.parse_one(text, read=read)
    except (SqlglotError, ValueError):
        return None  # 可执行性留给发布检查；静态可识别的引用仍须通过目录检查。


def _catalog(
    session: Session, actor: User, document: dict[str, Any], errors: list[dict[str, str]]
) -> tuple[dict[str, dict[str, Any]], dict[str, JoinPath]]:
    allowed = access.domain_ids(session, actor)
    visible = [
        asset
        for asset in session.scalars(select(Asset).where(Asset.status == "active"))
        if access.can_read_asset(session, actor, asset, allowed)
    ]
    by_source: dict[str, list[Asset]] = {}
    for asset in visible:
        by_source.setdefault(f"{asset.catalog_name}.{asset.db_name}.{asset.name}", []).append(asset)
    datasets = {}
    for index, dataset in enumerate(document["datasets"]):
        path = f"$.datasets[{index}]"
        matches = by_source.get(dataset["source"], [])
        if len(matches) != 1 or not matches[0].current_version_id:
            reason = "未找到唯一的可访问已采集资产" if matches else "未找到可访问的已采集资产"
            errors.append(_error("catalog", f"{path}.source", reason))
            continue
        asset = matches[0]
        columns = metrics._asset_columns(session, asset)
        dialect = engine_of(session.get(Datasource, asset.datasource_id)).sql.name
        fields = {}
        for field_index, field in enumerate(dataset.get("fields", [])):
            text, read, _ = _choose(field, dialect, field=True)
            node = _tree(text, read)
            refs = list(node.find_all(exp.Column)) if node is not None else []
            for column in refs:
                name = physical_column_name(column.this, set(columns), dialect)
                if name is None or (column.table and _matching(column.args["table"], {dataset["name"]}) is None):
                    errors.append(_error("catalog", f"{path}.fields[{field_index}]", "字段表达式引用的源列不存在"))
                elif isinstance(column.this, exp.Identifier):
                    column.this.set("this", name)
            fields[field["name"]] = node
        for key in dataset.get("primary_key", []) + [c for k in dataset.get("unique_keys", []) for c in k]:
            if key not in fields and key not in columns:
                errors.append(_error("catalog", path, "主键或唯一键引用的列不存在"))
        datasets[dataset["name"]] = {
            "asset": asset,
            "columns": columns,
            "dialect": dialect,
            "fields": fields,
            "original": dataset,
        }
    paths = {}
    candidates = [
        path
        for path in session.scalars(select(JoinPath).order_by(JoinPath.code))
        if any(path.left_asset_id == d["asset"].id for d in datasets.values())
        and any(path.right_asset_id == d["asset"].id for d in datasets.values())
    ]
    for index, relationship in enumerate(document.get("relationships", [])):
        left, right = datasets.get(relationship["from"]), datasets.get(relationship["to"])
        if not left or not right:
            continue
        columns = []
        for side, dataset in (("from", left), ("to", right)):
            mapped = [dataset["fields"].get(c, exp.column(c)) for c in relationship[f"{side}_columns"]]
            if not all(isinstance(node, exp.Column) for node in mapped):
                errors.append(_error("catalog", f"$.relationships[{index}]", "关联键必须映射到源列"))
            columns.append([node.name if isinstance(node, exp.Column) else "" for node in mapped])
        match = next(
            (
                p
                for p in candidates
                if p.left_asset_id == left["asset"].id
                and p.right_asset_id == right["asset"].id
                and p.left_columns == columns[0]
                and p.right_columns == columns[1]
            ),
            None,
        )
        if match is None:
            errors.append(_error("catalog", f"$.relationships[{index}]", "需要先建关联路径"))
        elif not effectively_approved(session, match):
            errors.append(_error("catalog", f"$.relationships[{index}]", "需要先批准关联路径"))
        else:
            paths[relationship["name"]] = match
    return datasets, paths


def _joins(
    document: dict[str, Any],
    datasets: dict[str, dict[str, Any]],
    paths: dict[str, JoinPath],
    base: str,
    options: _Options,
    required: set[str],
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    aliases = {base: "b"}
    joins = []
    if options.joins is not None:
        for join in options.joins:
            path = paths.get(join.get("relationship"))
            if path is None:
                raise ValueError("avelis 关联引用的 relationship 不存在或未批准")
            alias, from_alias = join.get("alias"), join.get("from", "b")
            if not isinstance(alias, str) or not alias.isidentifier() or len(alias) > 32 or alias in aliases.values():
                raise ValueError("关联别名无效或重复")
            source = next((name for name, a in aliases.items() if a == from_alias), None)
            if source is None or datasets[source]["asset"].id != path.left_asset_id:
                raise ValueError("关联路径起点与别名不一致")
            target = next(
                (r["to"] for r in document.get("relationships", []) if r["name"] == join["relationship"]), None
            )
            if not target:
                raise ValueError("关联路径终点不存在")
            aliases[target] = alias
            joins.append(
                {
                    "join_path_id": path.id,
                    "alias": alias,
                    "from": from_alias,
                    "pre_aggregate": bool(join.get("pre_aggregate", False)),
                }
            )
    else:
        outgoing: dict[str, list[dict[str, Any]]] = {}
        for relationship in document.get("relationships", []):
            outgoing.setdefault(relationship["from"], []).append(relationship)
        previous = {}
        pending = deque([base])
        while pending:
            for relationship in outgoing.get(pending.popleft(), []):
                right = relationship["to"]
                if right != base and right not in previous:
                    previous[right] = relationship
                    pending.append(right)
        inferred_aliases = {base: "b", **{name: f"j{i}" for i, name in enumerate(previous, 1)}}
        if options.dimensions is None:
            required.update(
                name
                for name in inferred_aliases
                if any("dimension" in field for field in datasets[name]["original"].get("fields", []))
            )
        # 扩展中的维度、时间与过滤表达式使用内部别名；裁剪路径时保留这些别名。
        for text in [*(d.get("column") for d in options.dimensions or []), options.time_column, options.filters]:
            node = _tree(text, datasets[base]["dialect"]) if text else None
            if node is not None:
                for column in node.find_all(exp.Column):
                    name = next((name for name, alias in inferred_aliases.items() if alias == column.table), None)
                    if name is not None:
                        required.add(name)
        needed = {base}
        for name in required:
            while name != base and name not in needed:
                relationship = previous.get(name)
                if relationship is None:
                    raise ValueError("指标或选用维度的数据集没有从基础数据集出发的关联路径")
                needed.add(name)
                name = relationship["from"]
        for right, relationship in previous.items():
            if right in needed:
                aliases[right] = inferred_aliases[right]
                joins.append(
                    {
                        "join_path_id": paths[relationship["name"]].id,
                        "alias": aliases[right],
                        "from": aliases[relationship["from"]],
                        "pre_aggregate": False,
                    }
                )
    return joins, aliases


def _mapped_expression(
    text: str,
    read: str | None,
    target: str,
    base: str,
    datasets: dict[str, dict[str, Any]],
    aliases: dict[str, str],
) -> str:
    node = _tree(text, read)
    if node is None:
        return text

    def replace(column: exp.Expression) -> exp.Expression:
        if not isinstance(column, exp.Column) or column.is_star:
            return column
        dataset = _matching(column.args["table"], set(aliases)) if column.table else base
        name = _matching(column.this, set(datasets[dataset]["fields"])) if dataset in aliases else None
        if name is None:
            raise ValueError("指标字段无法映射到基础资产或已批准的关联路径")
        field = datasets[dataset]["fields"][name]
        if field is None:
            raise _FieldUnavailable("字段表达式没有匹配的可解析方言")
        value = field.copy()
        for ref in value.find_all(exp.Column):
            ref.set("table", exp.to_identifier(aliases[dataset]))
        return exp.Paren(this=value) if not isinstance(value, exp.Column) else value

    return node.transform(replace).sql(dialect=target, unsupported_level=sqlglot.ErrorLevel.RAISE)


def _pins(
    session: Session, actor: User, formula: str, components: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[str]]:
    codes = [c.get("metric_code") for c in components]
    if not components or len(set(codes)) != len(codes) or set(parse_formula(formula)) != set(codes):
        raise ValueError("派生公式与结构化 components 不一致")
    pinned, warnings = [], []
    for component in components:
        code, number = component.get("metric_code"), component.get("version_no")
        if not isinstance(code, str) or not isinstance(number, int) or isinstance(number, bool):
            raise ValueError("components 必须声明口径编码与锁定版本号")
        metric = session.scalar(select(Metric).where(Metric.code == code))
        if metric is None or not access.can_see_object(session, actor, "metric", metric.id):
            raise ValueError("未找到可访问的组成口径；需要先建立并发布组成口径")
        version = metrics.latest_governed_version(session, metric)
        if version is None or version.status != "published" or version.kind != "base":
            raise ValueError("组成口径的锁定版本必须是已发布的基础口径；需要先发布组成口径")
        if metrics.dependency_drift(session, version):
            raise ValueError("组成口径的锁定版本依赖已变化")
        if any(
            component.get(key) is not None and component[key] != getattr(version, key)
            for key in ("expression", "dependency_fingerprint")
        ):
            warnings.append(f"组成口径 {code} 在本环境的版本与导出方不同，已按本环境当前版本锁定")
        pinned.append(
            {
                "metric_code": code,
                "metric_id": metric.id,
                "metric_name": metric.name,
                "version_id": version.id,
                "version_no": version.version_no,
                "source_version_no": number,
                "dependency_fingerprint": version.dependency_fingerprint,
            }
        )
    return pinned, warnings


def _plan(
    session: Session,
    actor: User,
    document: dict[str, Any],
    scope: str | None,
    item: dict[str, Any],
    datasets: dict[str, dict[str, Any]],
    paths: dict[str, JoinPath],
) -> dict[str, Any]:
    options = _options(item)
    context = item.get("ai_context")
    if isinstance(context, str):
        context = {"instructions": context}
    if context is not None:
        context = metrics.MetricAIContext.model_validate(
            {k: v for k, v in context.items() if k in metrics.MetricAIContext.model_fields}
        ).model_dump()
    spec = options.model_dump(exclude={"name", "aliases", "components", "base_dataset", "formula"})
    spec["description"] = item.get("description") or options.name or item["name"]
    spec["timezone"] = options.timezone or get_settings().business_timezone
    warnings = []
    pins = []
    unresolved = False
    if options.kind == "derived":
        formula = options.formula or options.expression
        if not formula:
            raise ValueError("派生口径需要 avelis 扩展保留原公式")
        if item["name"] in parse_formula(formula):
            raise ValueError("派生口径不能引用自身")
        pins, warnings = _pins(session, actor, formula, options.components)
        spec.update(expression=formula, base_asset_id=None, time_column="", joins=[], dimensions=[])
        versions = [
            {"metric": session.get(Metric, c["metric_id"]), "version": session.get(MetricVersion, c["version_id"])}
            for c in pins
        ]
        spec["dimensions"] = metrics._shared_dimensions(versions)
        aliases = {}
    else:
        base = options.base_dataset or scope
        if base is None:
            refs = []
            for dialect in iter_expression_dialects(item["expression"]):
                node = _tree(dialect["expression"], _read(dialect["dialect"]))
                if node is not None:
                    refs.extend(
                        name
                        for column in node.find_all(exp.Column)
                        if column.table and (name := _matching(column.args["table"], set(datasets))) is not None
                    )
            base = next(iter(refs), None) or (next(iter(datasets)) if len(datasets) == 1 else None)
        if base not in datasets:
            raise ValueError("无法确定指标基础数据集，请声明 base_dataset 或使用数据集限定的字段引用")
        target = datasets[base]["dialect"]
        text, read, converted = _choose(item, target)
        node = _tree(text, read)
        required = {
            name
            for column in (node.find_all(exp.Column) if node is not None else [])
            if column.table and (name := _matching(column.args["table"], set(datasets))) is not None
        }
        joins, aliases = _joins(document, datasets, paths, base, options, required)
        unresolved = read is None
        if unresolved:
            expression = text
            warnings.append("没有匹配的数据源方言；原表达式已保留，需人工填写表达式后才能提交")
        else:
            try:
                expression = _mapped_expression(text, read, target, base, datasets, aliases)
            except _FieldUnavailable:
                expression = text
                unresolved = True
                warnings.append("引用的字段表达式没有匹配的可解析方言；原文已保留，需人工维护后才能提交")
            if converted:
                warnings.append("表达式已从 OSSIE_SQL_2026 转写，待人工确认")
        if options.expression is not None or options.filters:
            matches = False
            if not unresolved and options.expression is not None:
                try:
                    folded = fold_filters(options.expression, options.filters, target)
                    matches = equivalent_expressions(folded, expression, target)
                except (SqlglotError, ValueError):
                    pass  # An untrusted extension cannot override the executable core.
            if matches and options.filters:
                expression = options.expression
            elif not matches:
                spec["filters"] = None
                warnings.append(
                    gettext("核心表达式与 Avelis 扩展不一致，已采用核心表达式并忽略扩展中的原表达式和固定过滤条件")
                )
        dimensions, times = [], []
        for name, alias in aliases.items():
            for field in datasets[name]["original"].get("fields", []):
                is_time = field.get("dimension", {}).get(
                    "is_time", field.get("datatype") in ("Date", "Time", "DateTime", "DateTimeTz")
                )
                original_node = datasets[name]["fields"][field["name"]]
                if original_node is None:
                    if is_time or "dimension" in field:
                        unresolved = True
                        warnings.append("维度或时间字段表达式无法安全映射；请维护草稿后再提交复核")
                    continue
                node = original_node.copy()
                for column in node.find_all(exp.Column):
                    column.set("table", exp.to_identifier(alias))
                if is_time:
                    times.append(node.sql(dialect=target))
                elif "dimension" in field:
                    dim_name = field["name"]
                    if any(d["name"] == dim_name for d in dimensions):
                        dim_name = f"{name}_{dim_name}"
                    dimensions.append(
                        {"name": dim_name, "column": node.sql(dialect=target), "label": field.get("label") or dim_name}
                    )
        spec.update(
            base_asset_id=datasets[base]["asset"].id,
            expression=expression,
            joins=joins,
            dimensions=options.dimensions if options.dimensions is not None else dimensions,
            time_column=options.time_column
            if options.time_column is not None
            else (times[0] if len(times) == 1 else ""),
        )
        if not spec["time_column"]:
            warnings.append("未确定唯一时间字段；请维护草稿后再提交复核")
    temporary = MetricVersion(**{k: spec[k] for k in metrics.EDITABLE_FIELDS if k in spec}, kind=options.kind)
    metrics._validate_time_semantics(temporary)
    dependency = (
        _dependencies(session, spec, datasets, aliases, unresolved=unresolved) if options.kind == "base" else []
    )
    unknown_dialects = [
        deepcopy(d) for d in iter_expression_dialects(item["expression"]) if _read(d["dialect"]) is None
    ]
    # 文档已由 normalize_document 隔离。每个版本只保留自身指标和共享目录信息，
    # 避免 N 个指标反复复制并存储其余 N-1 个指标；规范版本只由代码常量锁定。
    retained = {key: value for key, value in document.items() if key not in ("version", "metrics", "datasets")}
    retained["datasets"] = [
        {key: value for key, value in dataset.items() if key != "metrics"} for dataset in document["datasets"]
    ]
    if scope is None:
        retained["metrics"] = [item]
    else:
        next(dataset for dataset in retained["datasets"] if dataset["name"] == scope)["metrics"] = [item]
    return {
        "spec": spec,
        "name": options.name or item["name"],
        "aliases": options.aliases,
        "ai_context": context,
        "components": pins,
        "dependency": dependency,
        "metadata": {
            "document": retained,
            "metric_name": item["name"],
            "dataset_name": scope,
            "warnings": warnings,
            "components": pins,
            "unresolved_dialect": unresolved,
            "unknown_dialects": unknown_dialects,
        },
    }


def _dependencies(
    session: Session,
    spec: dict[str, Any],
    datasets: dict[str, dict[str, Any]],
    aliases: dict[str, str],
    *,
    unresolved: bool = False,
) -> list[dict[str, Any]]:
    by_alias = {alias: datasets[name] for name, alias in aliases.items()}
    target = by_alias["b"]["dialect"]
    refs = set()
    for text, role in [
        (spec["expression"], "measure"),
        (spec.get("time_column"), "time"),
        (spec.get("filters"), "filter"),
    ]:
        if unresolved and role == "measure":
            continue
        node = _tree(text, target) if text else None
        if node is not None:
            refs.update((c.table or "b", c.name, role) for c in node.find_all(exp.Column) if not c.is_star)
    for dim in spec["dimensions"]:
        if not dim.get("name") or not isinstance(dim.get("column"), str):
            raise ValueError("维度需要 name 与 column")
        node = _tree(dim["column"], target)
        if node is not None:
            refs.update((c.table or "b", c.name, "dimension") for c in node.find_all(exp.Column))
    for join in spec["joins"]:
        path = session.get(JoinPath, join["join_path_id"])
        refs.update((join["from"], column, "join_key") for column in path.left_columns)
        refs.update((join["alias"], column, "join_key") for column in path.right_columns)
    fingerprints = current_fingerprints(session)
    dependency = []
    for alias, column, role in sorted(refs):
        dataset = by_alias.get(alias)
        if dataset is None or column not in dataset["columns"]:
            # 专有表达式可能无法静态解析，但已识别的列不能绕过目录边界。
            raise ValueError("口径扩展或表达式引用了不存在的源列或关联别名")
        asset = dataset["asset"]
        fp = fingerprints.get((asset.id, column))
        dependency.append(
            {
                "alias": alias,
                "asset_id": asset.id,
                "column": column,
                "role": role,
                "qualified": f"{asset.catalog_name}.{asset.db_name}.{asset.name}",
                "fingerprint": fp.fingerprint if fp else None,
                "status": fp.status if fp else "unknown",
                "reason": fp.reason if fp else "尚未计算推导指纹",
            }
        )
    return dependency


def import_model(
    session: Session, actor: User, document: Any, domain_id: str, *, dry_run: bool = True
) -> dict[str, Any]:
    report = {
        "ok": True,
        "dry_run": dry_run,
        "results": [],
        "errors": [],
        "warnings": [],
        "counts": {"created": 0, "new_version": 0, "skipped": 0, "failed": 0},
    }
    try:
        access.require_domain_maintainer(session, actor, domain_id)
    except Forbidden:
        report["errors"].append(_error("catalog", "$.domain_id", "需要目标业务域的负责人或维护者身份"))
        _reject(report, forbidden=True)
    if session.get(Domain, domain_id) is None:
        report["errors"].append(_error("catalog", "$.domain_id", "目标业务域不存在"))
        _reject(report)
    report["errors"] = validate_structure(document)
    if report["errors"]:
        _reject(report)
    if "semantic_model" in document:
        report["warnings"].append(
            {"metric": "", "path": "$.semantic_model", "reason": "按旧版 Ossie 格式（88e0011）读取"}
        )
    document = normalize_document(document)
    items = list(iter_metrics(document))
    report["results"] = [{"name": item["name"], "action": "skipped"} for _, item, _ in items]
    report["errors"] = validate_model(document)
    if report["errors"]:
        _reject(report)
    datasets, paths = _catalog(session, actor, document, report["errors"])
    if report["errors"]:
        _reject(report)
    plans = []
    forbidden = False
    for scope, item, path in items:
        code = item["name"]
        if len(code) > 64 or not re.fullmatch(r"[a-z0-9_]+", code):
            report["errors"].append(_error("model", f"{path}.name", "口径编码须为最多 64 个小写字母、数字或下划线"))
            continue
        existing = session.scalar(select(Metric).where(Metric.code == code))
        if existing is not None and existing.domain_id != domain_id:
            report["errors"].append(_error("catalog", f"{path}.name", "编码冲突：目标域内没有可维护的同编码口径"))
            continue
        if existing is None:
            try:
                access.require_role(session, actor, "steward")
            except Forbidden:
                forbidden = True
                report["errors"].append(_error("catalog", path, "新建口径需要维护者角色"))
                continue
        try:
            plan = _plan(session, actor, document, scope, item, datasets, paths)
        except (ValueError, TypeError, KeyError, GovernanceError, SqlglotError) as exc:
            reason = "avelis 扩展或 ai_context 字段无效" if isinstance(exc, ValidationError) else str(exc)
            report["errors"].append(_error("catalog", path, reason))
            continue
        plan.update(existing=existing, code=code)
        plans.append(plan)
        report["warnings"].extend(
            {"metric": code, "path": path, "reason": reason} for reason in plan["metadata"]["warnings"]
        )
    if report["errors"]:
        _reject(report, forbidden=forbidden)
    if dry_run:
        for result, plan in zip(report["results"], plans, strict=True):
            result["reason"] = "校验通过；将创建新草稿版本" if plan["existing"] else "校验通过；将新建口径及草稿版本"
        report["counts"]["skipped"] = len(plans)
    else:
        try:
            with session.begin_nested():
                for result, plan in zip(report["results"], plans, strict=True):
                    metric = plan["existing"]
                    action = "new_version" if metric else "created"
                    if metric is None:
                        metric = metrics.create_metric(
                            session,
                            actor,
                            code=plan["code"],
                            name=plan["name"],
                            domain_id=domain_id,
                            owner_id=None,
                            aliases=plan["aliases"],
                            ai_context=plan["ai_context"],
                        )
                    elif plan["ai_context"] is not None:
                        metrics.update_ai_context(session, actor, metric, plan["ai_context"])
                    version = metrics.create_version(session, actor, metric, plan["spec"], defer_validation=True)
                    version.dependency = plan["dependency"]
                    version.components = plan["components"]
                    version.publish_check = {"ossie": plan["metadata"], "warnings": plan["metadata"]["warnings"]}
                    version.dependency_fingerprint = metrics.dependency_fingerprint(
                        version, version.components if version.kind == "derived" else version.dependency
                    )
                    if version.kind == "base":
                        version.asset_versions = metrics._asset_versions(session, version.dependency)
                    result.update(
                        action=action, metric_id=metric.id, version_id=version.id, version_no=version.version_no
                    )
                    report["counts"][action] += 1
                session.flush()
        except (IntegrityError, GovernanceError) as exc:
            report["errors"].append(_error("catalog", "$", "写入冲突，整体未写入；请刷新后重试"))
            for result in report["results"]:
                for key in ("metric_id", "version_id", "version_no"):
                    result.pop(key, None)
            _reject(report, forbidden=isinstance(exc, Forbidden))
    audit.record(
        session,
        actor,
        "interchange.ossie_import",
        "domain",
        domain_id,
        dry_run=dry_run,
        metric_count=len(items),
        counts=report["counts"],
    )
    return report
