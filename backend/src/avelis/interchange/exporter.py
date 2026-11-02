"""Map visible, published metric definitions to the pinned Ossie exchange model."""

from __future__ import annotations

import json
import logging
import re
from copy import deepcopy
from typing import Any

import sqlglot
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlglot import exp
from sqlglot.errors import SqlglotError

from avelis.engines import engine_of
from avelis.governance import access
from avelis.governance.derived import parse_formula
from avelis.governance.errors import Forbidden, GovernanceError, Invalid, NotFound
from avelis.governance.joins import effectively_approved
from avelis.governance.metrics import metric_ai_context, resolve_joins
from avelis.i18n import Msg, gettext
from avelis.models import Asset, ColumnVersion, Datasource, Domain, JoinPath, Metric, MetricVersion, User

from .constants import OSSIE_SPEC_COMMIT, OSSIE_SPEC_VERSION
from .expressions import fold_filters, physical_column_name
from .importer import _catalog, _plan
from .validation import (
    _matching,
    _read_dialect,
    iter_expression_dialects,
    iter_metrics,
    validate_model,
    validate_structure,
)

logger = logging.getLogger(__name__)


def _unique_name(stem: str, reserved: set[str]) -> str:
    name, suffix = stem, 2
    while name.casefold() in reserved:
        name = f"{stem}_{suffix}"
        suffix += 1
    reserved.add(name.casefold())
    return name


def _check_reimport(session: Session, actor: User, document: dict[str, Any]) -> None:
    """Run the importer's read-only checks without write permissions or audit writes."""
    errors = validate_model(document)
    if not errors:
        with session.no_autoflush:
            datasets, paths = _catalog(session, actor, document, errors)
            if not errors:
                for scope, item, path in iter_metrics(document):
                    if len(item["name"]) > 64 or not re.fullmatch(r"[a-z0-9_]+", item["name"]):
                        errors.append(
                            {
                                "layer": "model",
                                "path": f"{path}.name",
                                "reason": gettext("口径编码须为最多 64 个小写字母、数字或下划线"),
                            }
                        )
                        continue
                    try:
                        _plan(session, actor, document, scope, item, datasets, paths)
                    except (ValueError, TypeError, KeyError, GovernanceError, SqlglotError) as exc:
                        reason = "avelis 扩展或 ai_context 字段无效" if isinstance(exc, ValidationError) else str(exc)
                        errors.append({"layer": "catalog", "path": path, "reason": gettext(reason)})
    if errors:
        raise Invalid(gettext("Ossie 导出结果无法通过再导入校验"), errors=errors)


def _extensions(*groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for group in groups:
        encountered: list[dict[str, Any]] = []
        for extension in group or []:
            encountered.append(extension)
            # Merge copies of the same imported document without dropping repeated entries within it.
            if encountered.count(extension) > result.count(extension):
                result.append(deepcopy(extension))
    return result


def _avelis_data(item: dict[str, Any]) -> dict[str, Any]:
    result = {}
    for extension in item.get("custom_extensions", []):
        if extension.get("vendor_name") == "avelis":
            try:
                data = json.loads(extension["data"])
            except (ValueError, TypeError, KeyError):
                continue
            if isinstance(data, dict):
                result.update(data)
    return result


def _metadata(version: MetricVersion) -> tuple[dict[str, Any], dict[str, Any]]:
    metadata = (version.publish_check or {}).get("ossie", {})
    document = metadata.get("document", {})
    # Imported dataset-scoped metrics are always exported at the root.
    container = document
    if metadata.get("dataset_name"):
        container = next((d for d in document.get("datasets", []) if d["name"] == metadata["dataset_name"]), {})
    metric = next((m for m in container.get("metrics", []) if m["name"] == metadata.get("metric_name")), {})
    return document, metric


def _dialect(session: Session, asset: Asset) -> str:
    return engine_of(session.get(Datasource, asset.datasource_id)).sql.name


def _qualified(asset: Asset) -> str:
    return f"{asset.catalog_name}.{asset.db_name}.{asset.name}"


def _expression(
    text: str,
    dialect: str,
    aliases: dict[str, str],
    column_fields: dict[str, dict[str, str]] | None = None,
    *,
    default_alias: str | None = "b",
) -> str:
    """Rename SQL column aliases using its AST, preserving quoted identifiers."""
    node = sqlglot.parse_one(text, read=dialect)
    if node is None:
        raise ValueError("SQL expression is empty")
    for column in node.find_all(exp.Column):
        alias = _matching(column.args["table"], set(aliases)) if column.table else default_alias
        if alias in aliases and not column.db and not column.catalog:
            dataset = aliases[alias]
            fields = (column_fields or {}).get(dataset, {})
            name = physical_column_name(column.this, set(fields), dialect)
            if name is not None:
                column.set("this", exp.to_identifier(fields[name], quoted=not fields[name].isidentifier()))
            column.set("table", exp.to_identifier(dataset, quoted=not dataset.isidentifier()))
    return node.sql(dialect=dialect)


def _same_expression(text: str, read: str, current: str, target: str) -> bool:
    """Retain executable alternatives only when their parsed meaning still matches."""
    try:
        original = sqlglot.parse_one(text, read=read)
        return original is not None and original == sqlglot.parse_one(current, read=target)
    except (SqlglotError, ValueError):
        return False


def _field_column(dataset: dict[str, Any], field: dict[str, Any], dialect: str) -> exp.Column | None:
    """Recognize identity fields using the same executable dialect selected on import."""
    dialects = list(iter_expression_dialects(field["expression"]))
    selected = next((entry for entry in dialects if entry["dialect"] == dialect), None)
    if selected is None:
        selected = next((entry for entry in dialects if entry["dialect"] in ("OSSIE_SQL_2026", "ANSI_SQL")), None)
    if selected is None:
        return None
    try:
        column = sqlglot.parse_one(selected["expression"], read=dialect if selected["dialect"] == dialect else None)
    except (SqlglotError, ValueError):
        return None
    if (
        not isinstance(column, exp.Column)
        or column.db
        or column.catalog
        or (column.table and _matching(column.args["table"], {dataset["name"]}) is None)
    ):
        return None
    column.set("this", sqlglot.Dialect.get_or_raise(dialect).normalize_identifier(column.this.copy()))
    return column


def _physical_columns(dataset: dict[str, Any], names: list[str], dialect: str) -> list[str] | None:
    """Resolve relationship field names to the direct physical columns they declare."""
    fields = {field["name"]: field for field in dataset.get("fields", [])}
    result = []
    for name in names:
        if name not in fields:
            result.append(name)
            continue
        column = _field_column(dataset, fields[name], dialect)
        if column is None:
            return None
        result.append(column.name)
    return result


def _components(session: Session, version: MetricVersion) -> list[tuple[Metric, MetricVersion]]:
    result = []
    for component in version.components or []:
        current = session.get(MetricVersion, component.get("version_id")) if component.get("version_id") else None
        metric = (
            session.get(Metric, current.metric_id)
            if current
            else session.scalar(select(Metric).where(Metric.code == component.get("metric_code")))
        )
        if current is None and metric is not None:
            current = session.scalar(
                select(MetricVersion).where(
                    MetricVersion.metric_id == metric.id,
                    MetricVersion.version_no == component.get("version_no"),
                )
            )
        if metric is None or current is None or current.status != "published":
            return []
        result.append((metric, current))
    return result


def _expand_derived(
    session: Session,
    version: MetricVersion,
    components: list[tuple[Metric, MetricVersion]],
    aliases_by_version: dict[str, dict[str, str]],
    column_fields: dict[str, dict[str, str]],
) -> tuple[str, str]:
    """Expand only aggregates sharing one governed row set; otherwise keep the formula."""
    dialects = {
        _dialect(session, session.get(Asset, component.base_asset_id))
        for _, component in components
        if component.base_asset_id
    }
    dialect = next(iter(dialects)) if len(dialects) == 1 else "OSSIE_SQL_2026"
    if not components or len(dialects) != 1:
        return dialect, version.expression
    first = components[0][1]
    same_row_set = ("base_asset_id", "joins", "filters", "time_column", "timezone", "time_format", "data_grain")
    if any(
        c.kind != "base"
        or c.filters
        or any(join.get("pre_aggregate") for join in c.joins or [])
        or (c.time_aggregation or "sum") != "sum"
        or any(getattr(c, key) != getattr(first, key) for key in same_row_set)
        for _, c in components
    ):
        return dialect, version.expression
    try:
        codes = set(parse_formula(version.expression))
        if codes != {metric.code for metric, _ in components}:
            return dialect, version.expression
        replacement = {
            metric.code: sqlglot.parse_one(
                _expression(component.expression, dialect, aliases_by_version[component.id], column_fields),
                read=dialect,
            )
            for metric, component in components
        }
        formula = sqlglot.parse_one(version.expression, read=dialect)
        formula = formula.transform(
            lambda node: (
                exp.Paren(this=replacement[node.name].copy())
                if isinstance(node, exp.Column) and not node.table and node.name in replacement
                else node
            )
        )
        # Avelis derived evaluation yields NULL on division by zero.
        for division in formula.find_all(exp.Div):
            # Decimal literal prevents integer division for integral aggregate values.
            division.set(
                "this", exp.Paren(this=exp.Mul(this=division.this.copy(), expression=exp.Literal.number("1.0")))
            )
            division.set("expression", exp.Nullif(this=division.expression.copy(), expression=exp.Literal.number(0)))
        return dialect, formula.sql(dialect=dialect)
    except (SqlglotError, GovernanceError, KeyError, ValueError):
        return dialect, version.expression


def export_model(session: Session, actor: User, domain_id: str, metric_ids: list[str] | None = None) -> dict[str, Any]:
    """Export the current published versions without revealing inaccessible dependencies."""
    allowed = access.domain_ids(session, actor)
    if domain_id not in allowed:
        raise Forbidden("无权导出该业务域的口径")
    domain = session.get(Domain, domain_id)
    if domain is None:
        raise NotFound("业务域不存在")
    query = select(Metric).where(Metric.domain_id == domain_id).order_by(Metric.code)
    if metric_ids is not None:
        query = query.where(Metric.id.in_(metric_ids))
    selected: list[tuple[Metric, MetricVersion, list[tuple[Metric, MetricVersion]]]] = []
    resolved: dict[str, tuple[dict[str, Asset], list[dict[str, Any]]]] = {}

    def collect(version: MetricVersion) -> bool:
        if version.id in resolved:
            return True
        try:
            aliases, steps = resolve_joins(session, version)
        except GovernanceError:
            return False
        if not all(access.can_read_asset(session, actor, asset, allowed) for asset in aliases.values()):
            return False
        if not all(effectively_approved(session, step["path"]) for step in steps):
            return False
        if len({asset.id for asset in aliases.values()}) != len(aliases):
            raise GovernanceError("口径包含同一资产的多个关联角色，无法安全导出；请调整关联或分别定义口径")
        resolved[version.id] = aliases, steps
        return True

    for metric in session.scalars(query):
        version = session.scalar(
            select(MetricVersion)
            .where(MetricVersion.metric_id == metric.id, MetricVersion.status == "published")
            .order_by(MetricVersion.version_no.desc())
            .limit(1)
        )
        if version is None:
            continue
        components = _components(session, version) if version.kind == "derived" else []
        if version.kind == "derived":
            if not components or any(m.domain_id not in allowed or not collect(c) for m, c in components):
                continue
        elif not collect(version):
            continue
        selected.append((metric, version, components))

    if not selected:
        raise GovernanceError("没有可导出的已发布口径")

    out: dict[str, Any] = {
        "version": OSSIE_SPEC_VERSION,
        "name": domain.code,
        "datasets": [],
        "relationships": [],
        "metrics": [],
    }
    if domain.description:
        out["description"] = domain.description
    datasets: dict[str, dict[str, Any]] = {}
    dataset_assets: dict[str, Asset] = {}
    source_names: dict[str, str] = {}
    dataset_names: set[str] = set()
    originals: dict[str, dict[str, Any]] = {}
    originals_by_version: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
    versions = {v.id: v for _, v, _ in selected}
    for _, _, components in selected:
        versions.update({v.id: v for _, v in components})
    resolved = {key: value for key, value in resolved.items() if key in versions}
    readable_assets = {
        _qualified(a): a
        for a in session.scalars(select(Asset).where(Asset.status == "active"))
        if access.can_read_asset(session, actor, a, allowed)
    }
    for version in versions.values():
        document, original = deepcopy(_metadata(version))
        renamed = {}
        for dataset in document.get("datasets", []):
            asset = readable_assets.get(dataset.get("source"))
            if asset is not None:
                name = source_names.get(asset.id)
                if name is None:
                    name = _unique_name(dataset["name"], dataset_names)
                    source_names[asset.id] = name
                renamed[dataset["name"]] = name
        qualifiers = {old: new for old, new in renamed.items() if old != new}
        for dataset in document.get("datasets", []):
            if dataset["name"] in renamed:
                dataset["name"] = renamed[dataset["name"]]
            if not qualifiers:
                continue
            for field in dataset.get("fields", []):
                for entry in iter_expression_dialects(field["expression"]):
                    read = _read_dialect(entry["dialect"])
                    if read is not None:
                        try:
                            text = _expression(entry["expression"], read, qualifiers, default_alias=None)
                        except (SqlglotError, ValueError):
                            continue
                        if entry["dialect"] == "OSSIE_SQL_2026" and "sql" in field["expression"]:
                            field["expression"]["sql"] = text
                        else:
                            entry["expression"] = text
        for relationship in document.get("relationships", []):
            for side in ("from", "to"):
                relationship[side] = renamed.get(relationship[side], relationship[side])
        originals_by_version[version.id] = document, original
        context = document.get("ai_context")
        if context is not None:
            context = {"instructions": context} if isinstance(context, str) else context
            existing_context = out.setdefault("ai_context", {})
            if any(key in existing_context and existing_context[key] != value for key, value in context.items()):
                raise GovernanceError("所选口径来自语义提示不同的模型，请分别导出或统一模型语义提示")
            existing_context.update(deepcopy(context))
        root_extensions = _extensions(out.get("custom_extensions", []), document.get("custom_extensions", []))
        if root_extensions:
            out["custom_extensions"] = root_extensions
        for dataset in document.get("datasets", []):
            asset = readable_assets.get(dataset.get("source"))
            if asset is None:
                continue
            name = dataset["name"]
            if name not in datasets:
                datasets[name] = deepcopy(dataset)
                datasets[name].pop("metrics", None)
                # Old Ossie documents permitted empty keys, which declare no uniqueness.
                if not datasets[name].get("primary_key"):
                    datasets[name].pop("primary_key", None)
                if "unique_keys" in datasets[name]:
                    keys = [key for key in datasets[name]["unique_keys"] if key]
                    if keys:
                        datasets[name]["unique_keys"] = keys
                    else:
                        datasets[name].pop("unique_keys")
                dataset_assets[name] = asset
            else:
                existing = datasets[name]
                extra = _extensions(existing.get("custom_extensions", []), dataset.get("custom_extensions", []))
                if extra:
                    existing["custom_extensions"] = extra
                existing_fields = {f["name"]: f for f in existing.get("fields", [])}
                for field in dataset.get("fields", []):
                    if field["name"] not in existing_fields:
                        existing.setdefault("fields", []).append(deepcopy(field))
                        existing_fields[field["name"]] = existing["fields"][-1]
                    else:
                        previous = existing_fields[field["name"]]
                        if previous.get("expression") != field.get("expression") or previous.get(
                            "datatype"
                        ) != field.get("datatype"):
                            raise GovernanceError(
                                "所选口径包含同名但定义不同的字段，请分别导出或统一字段定义",
                                dataset=name,
                                field=field["name"],
                            )
                        extra = _extensions(previous.get("custom_extensions", []), field.get("custom_extensions", []))
                        if extra:
                            previous["custom_extensions"] = extra
            source_names.setdefault(asset.id, name)
        for relationship in document.get("relationships", []):
            previous = originals.setdefault(relationship["name"], deepcopy(relationship))
            extra = _extensions(previous.get("custom_extensions", []), relationship.get("custom_extensions", []))
            if extra:
                previous["custom_extensions"] = extra

    def dataset_name(asset: Asset) -> str:
        if asset.id not in source_names:
            stem = re.sub(r"\W+", "_", asset.name).strip("_") or "dataset"
            if not stem[0].isalpha() and stem[0] != "_":
                stem = f"dataset_{stem}"
            name = _unique_name(stem, dataset_names)
            source_names[asset.id] = name
            datasets[name] = {"name": name, "source": _qualified(asset), "fields": []}
            dataset_assets[name] = asset
        return source_names[asset.id]

    aliases_by_version: dict[str, dict[str, str]] = {}
    paths: dict[str, JoinPath] = {}
    for version_id, (aliases, steps) in resolved.items():
        aliases_by_version[version_id] = {alias: dataset_name(asset) for alias, asset in aliases.items()}
        paths.update({step["path"].id: step["path"] for step in steps})

    original_relationships = []
    for relationship in originals.values():
        left_name, right_name = relationship["from"], relationship["to"]
        if left_name not in dataset_assets or right_name not in dataset_assets:
            continue
        left_asset, right_asset = dataset_assets[left_name], dataset_assets[right_name]
        left_columns = _physical_columns(
            datasets[left_name], relationship["from_columns"], _dialect(session, left_asset)
        )
        right_columns = _physical_columns(
            datasets[right_name], relationship["to_columns"], _dialect(session, right_asset)
        )
        if left_columns is not None and right_columns is not None:
            original_relationships.append((relationship, left_asset.id, right_asset.id, left_columns, right_columns))
    approved_paths = [
        path
        for path in session.scalars(select(JoinPath).where(JoinPath.status == "approved"))
        if path.left_asset_id in source_names
        and path.right_asset_id in source_names
        and effectively_approved(session, path)
    ]
    originals_for_path = {}
    for path in approved_paths:
        original = next(
            (
                original
                for original, left_id, right_id, left_columns, right_columns in original_relationships
                if (
                    left_id == path.left_asset_id
                    and right_id == path.right_asset_id
                    and sorted(zip(left_columns, right_columns)) == sorted(zip(path.left_columns, path.right_columns))
                )
                or (
                    left_id == path.right_asset_id
                    and right_id == path.left_asset_id
                    and sorted(zip(right_columns, left_columns)) == sorted(zip(path.left_columns, path.right_columns))
                )
            ),
            None,
        )
        if original is not None:
            originals_for_path[path.id] = original

    # Retained fields are historical metadata; only the current snapshot is authoritative.
    used_columns: dict[str, set[str]] = {}
    for version_id, (aliases, steps) in resolved.items():
        version = versions[version_id]
        dialect = _dialect(session, aliases["b"])
        sql_dialect = sqlglot.Dialect.get_or_raise(dialect)
        preaggregate_keys = {step["alias"]: set(step["path"].right_columns) for step in steps if step["pre_aggregate"]}
        for text in [
            version.expression,
            version.filters,
            version.time_column,
            *(dim.get("column") for dim in version.dimensions or []),
        ]:
            if not text:
                continue
            try:
                node = sqlglot.parse_one(text, read=dialect)
            except (SqlglotError, ValueError):
                continue
            for column in node.find_all(exp.Column) if node is not None else []:
                alias = _matching(column.args["table"], set(aliases)) if column.table else "b"
                if column.is_star or (alias in preaggregate_keys and column.name not in preaggregate_keys[alias]):
                    continue
                asset = aliases.get(alias)
                if asset is not None:
                    used_columns.setdefault(asset.id, set()).add(
                        sql_dialect.normalize_identifier(column.this.copy()).name
                    )
        for step in steps:
            for side in ("left", "right"):
                path = step["path"]
                used_columns.setdefault(getattr(path, f"{side}_asset_id"), set()).update(
                    sql_dialect.normalize_identifier(exp.to_identifier(column, quoted=True)).name
                    for column in getattr(path, f"{side}_columns")
                )

    # Physical references must not resolve to a retained logical field that transforms that column.
    column_fields: dict[str, dict[str, str]] = {}
    for dataset_index, (name, dataset) in enumerate(datasets.items()):
        asset = dataset_assets[name]
        dialect = _dialect(session, asset)
        fields = dataset.setdefault("fields", [])
        columns = list(
            session.scalars(
                select(ColumnVersion)
                .where(ColumnVersion.asset_version_id == asset.current_version_id)
                .order_by(ColumnVersion.ordinal)
            )
        )
        current = {column.name for column in columns}
        physical_names = {
            sqlglot.Dialect.get_or_raise(dialect).normalize_identifier(exp.to_identifier(column, quoted=True)).name
            for column in current
        }
        missing_required = used_columns.get(asset.id, set()) - physical_names
        if missing_required:
            raise Invalid(
                gettext("Ossie 导出结果无法通过再导入校验"),
                errors=[
                    {
                        "layer": "catalog",
                        "path": f"$.datasets[{dataset_index}].fields",
                        "reason": gettext(
                            "导出口径引用了当前资产中不存在的源列：{dataset}.{column}", dataset=name, column=column
                        ),
                    }
                    for column in sorted(missing_required)
                ],
            )
        keys = {key.casefold() for key in dataset.get("primary_key", [])}
        keys.update(key.casefold() for group in dataset.get("unique_keys", []) for key in group)
        for relationship in originals_for_path.values():
            for side in ("from", "to"):
                if relationship[side] == name:
                    keys.update(key.casefold() for key in relationship[f"{side}_columns"])
        for field in fields[:]:
            dialects = list(iter_expression_dialects(field["expression"]))
            selected_dialect = next((entry for entry in dialects if entry["dialect"] == dialect), None)
            if selected_dialect is None:
                selected_dialect = next(
                    (entry for entry in dialects if entry["dialect"] in ("OSSIE_SQL_2026", "ANSI_SQL")), None
                )
            if selected_dialect is None:
                continue
            try:
                tree = sqlglot.parse_one(selected_dialect["expression"], read=dialect)
            except (SqlglotError, ValueError):
                continue
            refs = list(tree.find_all(exp.Column)) if tree is not None else []
            missing = [column for column in refs if physical_column_name(column.this, current, dialect) is None]
            used = (
                "dimension" in field
                or field["name"].casefold() in keys
                or any(
                    sqlglot.Dialect.get_or_raise(dialect).normalize_identifier(column.this.copy()).name
                    in used_columns.get(asset.id, set())
                    for column in missing
                )
            )
            if missing and not used:
                fields.remove(field)
                logger.warning(
                    "%s",
                    gettext(
                        "已从导出中移除引用已删除源列的未使用字段：{dataset}.{field}", dataset=name, field=field["name"]
                    ),
                )
        field_names: set[str] = set()
        renamed_fields = {}
        for field in fields:
            previous = field["name"]
            field["name"] = _unique_name(previous, field_names)
            renamed_fields[previous] = field["name"]
        if "primary_key" in dataset:
            dataset["primary_key"] = [renamed_fields.get(key, key) for key in dataset["primary_key"]]
        if "unique_keys" in dataset:
            dataset["unique_keys"] = [
                [renamed_fields.get(key, key) for key in group] for group in dataset["unique_keys"]
            ]
        for relationship in originals.values():
            for side in ("from", "to"):
                if relationship[side] == name:
                    relationship[f"{side}_columns"] = [
                        renamed_fields.get(key, key) for key in relationship[f"{side}_columns"]
                    ]
        names = {field["name"] for field in fields}
        reserved = {value.casefold() for value in names | {column.name for column in columns}}
        for column in columns:
            field_name = column.name if column.name in names else _matching(column.name, names)
            if field_name is not None:
                field = next(field for field in fields if field["name"] == field_name)
                physical = _field_column(dataset, field, dialect)
                if physical is not None and physical_column_name(physical.this, {column.name}, dialect) == column.name:
                    column_fields.setdefault(name, {})[column.name] = field_name
                    continue
                identity = next(
                    (
                        field["name"]
                        for field in fields
                        if field["name"].isidentifier()
                        and (physical := _field_column(dataset, field, dialect)) is not None
                        and physical_column_name(physical.this, {column.name}, dialect) == column.name
                    ),
                    None,
                )
                if identity is not None:
                    column_fields.setdefault(name, {})[column.name] = identity
                    continue
                stem = re.sub(r"[^A-Za-z0-9_]+", "_", column.name).strip("_") or "column"
                if not stem[0].isalpha() and stem[0] != "_":
                    stem = f"column_{stem}"
                stem = f"{stem}__column"
                field_name = _unique_name(stem, reserved)
                column_fields.setdefault(name, {})[column.name] = field_name
            else:
                field_name = _unique_name(column.name, field_names)
                column_fields.setdefault(name, {})[column.name] = field_name
            if field_name not in names:
                # Include collected columns without guessing logical datatypes from physical types.
                field: dict[str, Any] = {
                    "name": field_name,
                    "expression": {
                        "dialects": [
                            {
                                "dialect": dialect,
                                "expression": exp.column(column.name, quoted=True).sql(dialect=dialect),
                            }
                        ]
                    },
                }
                if column.comment:
                    field["description"] = column.comment
                fields.append(field)
                names.add(field_name)
                reserved.add(field_name.casefold())

    for version_id, (aliases, _) in resolved.items():
        version = versions[version_id]
        dialect = _dialect(session, aliases["b"])
        for dim in [*(version.dimensions or []), {"column": version.time_column, "is_time": True}]:
            try:
                column = sqlglot.parse_one(dim.get("column") or "", read=dialect)
            except (SqlglotError, ValueError):
                continue
            if not isinstance(column, exp.Column) or (column.table or "b") not in aliases:
                continue
            name = aliases_by_version[version_id][column.table or "b"]
            fields = datasets[name]["fields"]
            field_name = dim.get("name") or column.name
            field = next((f for f in fields if f["name"] == field_name), None)
            if field is not None:
                physical = _field_column(datasets[name], field, dialect)
                if physical is None or physical_column_name(physical.this, {column.name}, dialect) is None:
                    field = None
            if field is None:
                field_name = _unique_name(field_name, {field["name"].casefold() for field in fields})
                field = {
                    "name": field_name,
                    "expression": {
                        "dialects": [
                            {
                                "dialect": dialect,
                                "expression": exp.column(column.name, quoted=True).sql(dialect=dialect),
                            }
                        ]
                    },
                }
                fields.append(field)
            if field is not None:
                dimension = field.setdefault("dimension", {})
                if "is_time" in dim:
                    dimension["is_time"] = bool(dim["is_time"])
                elif "is_time" not in dimension:
                    dimension["is_time"] = field.get("datatype") in ("Date", "Time", "DateTime", "DateTimeTz")
                if dim.get("label"):
                    field["label"] = dim["label"]

    relationships: dict[str, dict[str, Any]] = {}
    # Reuse imported relationship metadata only where its current catalogue path remains approved.
    for path in approved_paths:
        original = originals_for_path.get(path.id, {})
        if path.id in paths or original:
            item = deepcopy(original)
            item.update(
                {
                    "name": path.code,
                    "from": source_names[path.left_asset_id],
                    "to": source_names[path.right_asset_id],
                    "from_columns": [
                        column_fields.get(source_names[path.left_asset_id], {}).get(column, column)
                        for column in path.left_columns
                    ],
                    "to_columns": [
                        column_fields.get(source_names[path.right_asset_id], {}).get(column, column)
                        for column in path.right_columns
                    ],
                }
            )
            relationships[path.code] = item

    for metric, version, components in selected:
        _, original = originals_by_version[version.id]
        item = deepcopy(original)
        item.update({"name": metric.code, "description": version.description})
        original_context = original.get("ai_context", {})
        item["ai_context"] = (
            deepcopy(original_context) if isinstance(original_context, dict) else {"instructions": original_context}
        )
        item["ai_context"].update(metric_ai_context(metric))
        item["ai_context"] = {k: v for k, v in item["ai_context"].items() if v is not None}
        if version.kind == "derived":
            dialect, expression = _expand_derived(session, version, components, aliases_by_version, column_fields)
        else:
            asset = resolved[version.id][0]["b"]
            dialect = _dialect(session, asset)
            preaggregate_keys = {
                step["alias"]: set(step["path"].right_columns)
                for step in resolved[version.id][1]
                if step["pre_aggregate"]
            }
            if preaggregate_keys:
                columns = sqlglot.parse_one(version.expression, read=dialect).find_all(exp.Column)
                if any(
                    column.table in preaggregate_keys and column.name not in preaggregate_keys[column.table]
                    for column in columns
                ):
                    raise GovernanceError("口径表达式引用预聚合生成的虚拟字段，无法安全导出；请调整口径定义")
            try:
                folded = fold_filters(version.expression, version.filters, dialect)
            except ValueError as exc:
                raise Invalid(
                    str(exc),
                    errors=[
                        {"layer": "model", "path": f"$.metrics[{len(out['metrics'])}].expression", "reason": str(exc)}
                    ],
                ) from exc
            expression = _expression(folded, dialect, aliases_by_version[version.id], column_fields)
        original_expression = deepcopy(original.get("expression", {}))
        dataset_scope = (version.publish_check or {}).get("ossie", {}).get("dataset_name")
        if dataset_scope:
            # All exported metrics live at model scope; known SQL must use dataset.field.
            for entry in iter_expression_dialects(original_expression):
                read = _read_dialect(entry["dialect"])
                if read is not None:
                    try:
                        promoted = _expression(
                            entry["expression"], read, {"b": dataset_scope, dataset_scope: dataset_scope}
                        )
                    except (SqlglotError, ValueError):
                        continue  # Unparseable alternatives cannot pass the equivalence check below.
                    if entry["dialect"] == "OSSIE_SQL_2026" and "sql" in original_expression:
                        original_expression["sql"] = promoted
                    else:
                        entry["expression"] = promoted
        dialects = []
        stale_alternatives = False
        for entry in original_expression.get("dialects", []):
            if "sql" in original_expression and entry["dialect"] == "OSSIE_SQL_2026":
                # A shadowed entry must not become authoritative when stale sql is omitted.
                stale_alternatives = True
                continue
            if entry["dialect"] == dialect:
                stale_alternatives |= not _same_expression(entry["expression"], dialect, expression, dialect)
                continue
            read = _read_dialect(entry["dialect"])
            if read is None or _same_expression(entry["expression"], read, expression, dialect):
                dialects.append(entry)
            else:
                stale_alternatives = True
        dialects.insert(0, {"dialect": dialect, "expression": expression})
        item["expression"] = {"dialects": dialects}
        if "sql" in original_expression:
            if _same_expression(original_expression["sql"], "", expression, dialect):
                item["expression"]["sql"] = original_expression["sql"]
            else:
                stale_alternatives = True
        data = _avelis_data(original)
        if stale_alternatives:
            data["original_expression"] = deepcopy(original["expression"])
        data.update(
            {
                "kind": version.kind,
                "name": metric.name,
                "aliases": metric.aliases or [],
                "base_dataset": aliases_by_version.get(version.id, {}).get("b"),
                "expression": version.expression,
                **{
                    key: getattr(version, key)
                    for key in (
                        "time_column",
                        "timezone",
                        "time_format",
                        "data_grain",
                        "time_aggregation",
                        "filters",
                        "dimensions",
                        "grain",
                        "dedupe_basis",
                        "unit",
                        "unit_scale",
                        "value_kind",
                        "polarity",
                    )
                },
                "material_change_ratio": float(version.material_change_ratio)
                if version.material_change_ratio is not None
                else None,
                "joins": [
                    {
                        "relationship": step["path"].code,
                        "alias": step["alias"],
                        "from": step["from"],
                        "pre_aggregate": step["pre_aggregate"],
                    }
                    for step in resolved.get(version.id, ({}, []))[1]
                ],
                "components": [
                    {"metric_code": m.code, "version_no": c.version_no, "expression": c.expression}
                    for m, c in components
                ],
            }
        )
        unknown = (version.publish_check or {}).get("ossie", {}).get("unknown_dialects", [])
        if unknown:
            data["unknown_dialects"] = deepcopy(unknown)
        if version.kind == "derived":
            data["formula"] = version.expression
        item["custom_extensions"] = [
            e for e in original.get("custom_extensions", []) if e.get("vendor_name") != "avelis"
        ]
        item["custom_extensions"].append(
            {"vendor_name": "avelis", "data": json.dumps(data, ensure_ascii=False, sort_keys=True)}
        )
        out["metrics"].append(item)
    out["datasets"] = list(datasets.values())
    out["relationships"] = list(relationships.values())
    errors = validate_structure(out)
    if errors:
        paths = [error["path"] for error in errors]
        raise Invalid(
            Msg("{key} 取值无效：{value}", key="Ossie", value=", ".join(paths)),
            spec_commit=OSSIE_SPEC_COMMIT,
            paths=paths,
            errors=errors,
        )
    _check_reimport(session, actor, out)
    return out
