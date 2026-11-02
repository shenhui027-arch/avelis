"""固定 Ossie 结构与模型内部引用校验；不连接数据源或执行表达式。"""

from __future__ import annotations

import json
from collections.abc import Iterator
from copy import deepcopy
from functools import lru_cache
from importlib.resources import files
from typing import Any, NoReturn

import sqlglot
from jsonschema import Draft202012Validator
from referencing import Registry
from referencing.exceptions import NoSuchResource
from sqlglot import exp
from sqlglot.errors import SqlglotError

from .constants import OSSIE_SPEC_COMMIT

ValidationError = dict[str, str]


def _deny_remote_schema(uri: str) -> NoReturn:
    raise NoSuchResource(ref=uri)


@lru_cache(maxsize=2)
def _validator(envelope: bool) -> Draft202012Validator:
    commit = "88e0011" if envelope else OSSIE_SPEC_COMMIT
    schema = json.loads(files("avelis.interchange").joinpath(f"ossie_schema_{commit}.json").read_text(encoding="utf-8"))
    # Registry 没有网络加载器；即使将来误加外部 $ref，也只能失败。
    return Draft202012Validator(schema, registry=Registry(retrieve=_deny_remote_schema))


def validate_structure(document: Any) -> list[ValidationError]:
    """严格校验官方原文；旧 envelope 先校验旧版，再转成新版校验。"""
    envelope = isinstance(document, dict) and "semantic_model" in document
    if envelope:
        errors = [
            {"layer": "structure", "path": error.json_path, "reason": error.message}
            for error in sorted(_validator(True).iter_errors(document), key=lambda error: error.json_path)
        ]
        if errors:
            return errors
        if len(document["semantic_model"]) != 1:
            return [{"layer": "structure", "path": "$.semantic_model", "reason": "Ossie 文件必须恰好包含一个语义模型"}]
        document = normalize_document(document)
    return [
        {"layer": "structure", "path": error.json_path, "reason": error.message}
        for error in sorted(_validator(False).iter_errors(document), key=lambda error: error.json_path)
    ]


def normalize_document(document: dict[str, Any]) -> dict[str, Any]:
    """结构校验通过后转成 root 形式；不修改调用方上传的对象。"""
    if "semantic_model" not in document:
        return deepcopy(document)
    models = document["semantic_model"]
    if not isinstance(models, list) or len(models) != 1 or not isinstance(models[0], dict):
        raise ValueError("Ossie 文件必须恰好包含一个语义模型")
    return {"version": document.get("version"), **deepcopy(models[0])}


def iter_expression_dialects(expression: dict[str, Any]) -> Iterator[dict[str, str]]:
    """按规范选择 portable SQL 原式，保留其他方言及调用方对象。"""
    if "sql" in expression:
        yield {"dialect": "OSSIE_SQL_2026", "expression": expression["sql"]}
    for dialect in expression.get("dialects", []):
        if "sql" not in expression or dialect["dialect"] != "OSSIE_SQL_2026":
            yield dialect


def iter_metrics(document: dict[str, Any]) -> Iterator[tuple[str | None, dict[str, Any], str]]:
    """依次返回 (dataset 名或 None, 指标对象, root JSON 路径)。"""
    for index, metric in enumerate(document.get("metrics", [])):
        yield None, metric, f"$.metrics[{index}]"
    for dataset_index, dataset in enumerate(document.get("datasets", [])):
        for metric_index, metric in enumerate(dataset.get("metrics", [])):
            yield dataset["name"], metric, f"$.datasets[{dataset_index}].metrics[{metric_index}]"


def _duplicates(items: list[dict[str, Any]], path: str, label: str) -> list[ValidationError]:
    errors = []
    seen = set()
    for index, item in enumerate(items):
        key = item["name"].casefold()
        if key in seen:
            errors.append({"layer": "model", "path": f"{path}[{index}].name", "reason": f"{label}重名：{item['name']}"})
        seen.add(key)
    return errors


def _matching(identifier: exp.Expression | str, names: set[str]) -> str | None:
    name = identifier.name if isinstance(identifier, exp.Expression) else identifier
    if isinstance(identifier, exp.Identifier) and identifier.quoted:
        return name if name in names else None
    return next((candidate for candidate in names if candidate.casefold() == name.casefold()), None)


def _read_dialect(name: str) -> str | None:
    # 仅兼容规范旧示例中的已知拼法；自由方言名保持大小写敏感。
    aliases = {"ANSI_SQL": "", "OSSIE_SQL_2026": "", "SNOWFLAKE": "snowflake", "BIGQUERY": "bigquery"}
    aliases["DATABRICKS"] = "databricks"
    if name in aliases:
        return aliases[name]
    return name if name in sqlglot.Dialect.classes else None


def _component_codes(metric: dict[str, Any]) -> set[str]:
    """派生原式可引用外部锁定组成口径；可见性与版本由目录层核对。"""
    codes = set()
    for extension in metric.get("custom_extensions", []):
        if extension.get("vendor_name") != "avelis":
            continue
        try:
            data = json.loads(extension.get("data", ""))
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(data, dict) or data.get("kind") != "derived":
            continue
        components = data.get("components")
        if not isinstance(components, list):
            continue
        codes.update(
            component["metric_code"]
            for component in components
            if isinstance(component, dict) and isinstance(component.get("metric_code"), str)
        )
    return codes


def _metric_references(
    metric: dict[str, Any],
    scope: str | None,
    path: str,
    fields: dict[str, set[str]],
    metric_names: set[str],
    scoped_metrics: dict[str, set[str]],
) -> list[ValidationError]:
    errors: list[ValidationError] = []
    references = metric_names | _component_codes(metric)
    expression = metric["expression"]
    for dialect in iter_expression_dialects(expression):
        read = _read_dialect(dialect["dialect"])
        if read is None:
            continue  # 未知语言保留原文，不能把专有语法当 SQL 引用解析。
        try:
            tree = sqlglot.parse_one(dialect["expression"], read=read)
        except (SqlglotError, ValueError):
            continue  # 执行语义与语法能否发布，留给现有发布检查。
        if tree is None:
            continue
        if "sql" in expression and dialect["dialect"] == "OSSIE_SQL_2026":
            expression_path = f"{path}.expression.sql"
        else:
            index = expression["dialects"].index(dialect)
            expression_path = f"{path}.expression.dialects[{index}].expression"
        if tree.find(exp.Query) is not None:
            errors.append(
                {
                    "layer": "model",
                    "path": expression_path,
                    "reason": "子查询的引用无法静态确定，请先维护为单个指标表达式",
                }
            )
            continue
        reported = set()
        available = fields.get(scope, set()) if scope else set().union(*fields.values())
        for column in tree.find_all(exp.Column):
            reason = None
            parts = column.parts
            portable = dialect["dialect"] == "OSSIE_SQL_2026"
            if portable and scope and len(parts) != 1:
                reason = "数据集作用域指标只能用单段名称引用本数据集字段"
            elif portable and scope and not column.is_star:
                if _matching(column.this, available) is None:
                    reason = f"指标引用的字段不存在：{column.name}"
            elif portable and not scope and len(parts) == 1 and not column.is_star:
                if _matching(column.this, _component_codes(metric)) is None:
                    reason = "模型作用域指标须用数据集.字段引用字段；不能直接引用其他指标"
            elif len(parts) > 2:
                reason = "多段字段的引用无法静态确定，请先维护为数据集.字段形式"
            elif len(parts) == 2:
                dataset = _matching(parts[0], set(fields))
                if dataset is None:
                    reason = f"指标引用的数据集不存在：{parts[0].name}"
                elif (
                    not column.is_star
                    and _matching(
                        parts[1], fields[dataset] | (set() if portable else scoped_metrics.get(dataset, set()))
                    )
                    is None
                ):
                    reason = f"指标引用的字段不存在：{parts[0].name}.{parts[1].name}"
            elif not column.is_star:
                if _matching(column.this, available | references) is None:
                    reason = f"指标引用的字段或指标不存在：{column.name}"
            if reason and reason not in reported:
                errors.append({"layer": "model", "path": expression_path, "reason": reason})
                reported.add(reason)
    return errors


def validate_model(document: dict[str, Any]) -> list[ValidationError]:
    """结构校验通过后，检查名称、关系与已知 SQL 方言的静态引用。"""
    document = normalize_document(document)
    datasets = document.get("datasets", [])
    relationships = document.get("relationships", [])
    errors = _duplicates(datasets, "$.datasets", "数据集")
    errors.extend(_duplicates(relationships, "$.relationships", "关系"))
    fields: dict[str, set[str]] = {}
    scoped_metrics: dict[str, set[str]] = {}
    for index, dataset in enumerate(datasets):
        dataset_fields = dataset.get("fields", [])
        errors.extend(_duplicates(dataset_fields, f"$.datasets[{index}].fields", "字段"))
        fields[dataset["name"]] = {field["name"] for field in dataset_fields}
        scoped_metrics[dataset["name"]] = {metric["name"] for metric in dataset.get("metrics", [])}
        field_names = {field["name"].casefold() for field in dataset_fields}
        for metric_index, metric in enumerate(dataset.get("metrics", [])):
            if metric["name"].casefold() in field_names:
                errors.append(
                    {
                        "layer": "model",
                        "path": f"$.datasets[{index}].metrics[{metric_index}].name",
                        "reason": f"数据集字段与指标重名：{metric['name']}",
                    }
                )
    # Avelis 的 metric code 全局唯一，导入时也保持这一目录约束。
    seen_metrics = set()
    metrics = list(iter_metrics(document))
    for _, metric, path in metrics:
        key = metric["name"].casefold()
        if key in seen_metrics:
            errors.append({"layer": "model", "path": f"{path}.name", "reason": f"指标重名：{metric['name']}"})
        seen_metrics.add(key)
    for index, relationship in enumerate(relationships):
        path = f"$.relationships[{index}]"
        if len(relationship["from_columns"]) != len(relationship["to_columns"]):
            errors.append({"layer": "model", "path": path, "reason": "关系两端列数必须一致"})
        for side in ("from", "to"):
            dataset = _matching(relationship[side], set(fields))
            if dataset is None:
                errors.append({"layer": "model", "path": f"{path}.{side}", "reason": "关系引用的数据集不存在"})
                continue
            for column_index, column in enumerate(relationship[f"{side}_columns"]):
                if _matching(column, fields[dataset]) is None:
                    errors.append(
                        {
                            "layer": "model",
                            "path": f"{path}.{side}_columns[{column_index}]",
                            "reason": f"关系引用的字段不存在：{column}",
                        }
                    )
    metric_names = {metric["name"] for _, metric, _ in metrics}
    for scope, metric, path in metrics:
        errors.extend(_metric_references(metric, scope, path, fields, metric_names, scoped_metrics))
    return errors
