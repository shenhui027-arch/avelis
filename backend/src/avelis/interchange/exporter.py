"""Map visible, published metric definitions to the pinned Ossie exchange model."""

from __future__ import annotations

import json
import re
from copy import deepcopy
from typing import Any

import sqlglot
from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlglot import exp
from sqlglot.errors import SqlglotError

from avelis.engines import engine_of
from avelis.governance import access
from avelis.governance.derived import parse_formula
from avelis.governance.errors import Forbidden, GovernanceError, NotFound
from avelis.governance.joins import effectively_approved
from avelis.governance.metrics import metric_ai_context, resolve_joins
from avelis.i18n import Msg
from avelis.models import Asset, ColumnVersion, Datasource, Domain, JoinPath, Metric, MetricVersion, User

from .constants import OSSIE_SPEC_COMMIT, OSSIE_SPEC_VERSION
from .validation import _read_dialect, iter_expression_dialects, validate_structure


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


def _expression(text: str, dialect: str, aliases: dict[str, str]) -> str:
    """Rename SQL column aliases using its AST, preserving quoted identifiers."""
    node = sqlglot.parse_one(text, read=dialect)
    if node is None:
        raise ValueError("SQL expression is empty")
    for column in node.find_all(exp.Column):
        alias = column.table or "b"
        if alias in aliases and not column.db and not column.catalog:
            column.set("table", exp.to_identifier(aliases[alias], quoted=not aliases[alias].isidentifier()))
    return node.sql(dialect=dialect)


def _same_expression(text: str, read: str, current: str, target: str) -> bool:
    """Retain executable alternatives only when their parsed meaning still matches."""
    try:
        original = sqlglot.parse_one(text, read=read)
        return original is not None and original == sqlglot.parse_one(current, read=target)
    except (SqlglotError, ValueError):
        return False


def _physical_columns(dataset: dict[str, Any], names: list[str], dialect: str) -> list[str] | None:
    """Resolve relationship field names to the direct physical columns they declare."""
    fields = {field["name"]: field for field in dataset.get("fields", [])}
    result = []
    for name in names:
        field = fields.get(name)
        if field is None:
            result.append(name)
            continue
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
            or column.table not in ("", dataset["name"])
        ):
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
                _expression(component.expression, dialect, aliases_by_version[component.id]), read=dialect
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
        document, original = _metadata(version)
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
            if name in datasets and datasets[name]["source"] != dataset["source"]:
                raise GovernanceError("所选口径包含同名但不同源的数据集，请分别导出或统一数据集命名", dataset=name)
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
            name = stem
            suffix = 2
            while name in datasets:
                name = f"{stem}_{suffix}"
                suffix += 1
            source_names[asset.id] = name
            datasets[name] = {"name": name, "source": _qualified(asset), "fields": []}
            dataset_assets[name] = asset
        return source_names[asset.id]

    aliases_by_version: dict[str, dict[str, str]] = {}
    paths: dict[str, JoinPath] = {}
    for version_id, (aliases, steps) in resolved.items():
        aliases_by_version[version_id] = {alias: dataset_name(asset) for alias, asset in aliases.items()}
        paths.update({step["path"].id: step["path"] for step in steps})

    # Include each collected column, but never guess logical datatype from its physical type.
    for name, dataset in datasets.items():
        asset = dataset_assets[name]
        dialect = _dialect(session, asset)
        fields = dataset.setdefault("fields", [])
        names = {field["name"] for field in fields}
        for column in session.scalars(
            select(ColumnVersion)
            .where(ColumnVersion.asset_version_id == asset.current_version_id)
            .order_by(ColumnVersion.ordinal)
        ):
            if column.name not in names:
                field: dict[str, Any] = {
                    "name": column.name,
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
                names.add(column.name)

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
            if field is None:
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

    relationships: dict[str, dict[str, Any]] = {}
    # Reuse imported relationship metadata only where its current catalogue path remains approved.
    for path in session.scalars(select(JoinPath).where(JoinPath.status == "approved")):
        if (
            path.left_asset_id in source_names
            and path.right_asset_id in source_names
            and effectively_approved(session, path)
        ):
            original = next(
                (
                    original
                    for original, left_id, right_id, left_columns, right_columns in original_relationships
                    if (
                        (
                            left_id == path.left_asset_id
                            and right_id == path.right_asset_id
                            and sorted(zip(left_columns, right_columns))
                            == sorted(zip(path.left_columns, path.right_columns))
                        )
                        or (
                            left_id == path.right_asset_id
                            and right_id == path.left_asset_id
                            and sorted(zip(right_columns, left_columns))
                            == sorted(zip(path.left_columns, path.right_columns))
                        )
                    )
                ),
                {},
            )
            if path.id in paths or original:
                item = deepcopy(original)
                item.update(
                    {
                        "name": path.code,
                        "from": source_names[path.left_asset_id],
                        "to": source_names[path.right_asset_id],
                        "from_columns": path.left_columns,
                        "to_columns": path.right_columns,
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
            dialect, expression = _expand_derived(session, version, components, aliases_by_version)
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
            expression = _expression(version.expression, dialect, aliases_by_version[version.id])
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
        raise GovernanceError(
            Msg("{key} 取值无效：{value}", key="Ossie", value=", ".join(paths)),
            spec_commit=OSSIE_SPEC_COMMIT,
            paths=paths,
        )
    return out
