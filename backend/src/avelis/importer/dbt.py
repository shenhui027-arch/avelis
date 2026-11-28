"""dbt 编译产物 manifest.json → 可解析的 SQL。

manifest 里每个模型都有编译后的 SQL（compiled_code，旧版本叫 compiled_sql）和物化目标（relation_name）。
把每个模型转成一条 `CREATE TABLE|VIEW <目标> AS <编译后的 SQL>`，前面加一行注释标明来自哪个模型，拼成一个 SQL 文本，
交给现有的 SQL 导入流程：列级血缘、语句状态、定位原文、未匹配表都直接复用，不另起一套。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from avelis.governance.errors import Invalid

DBT_LANGUAGE = "dbt"
MANIFEST_NAME = "manifest.json"
# 转成 SQL 的节点类型；seed 没有 SQL，test / analysis 不产生表
CONVERTED_TYPES = ("model", "snapshot")


@dataclass
class DbtConversion:
    sql: str
    models: int
    notes: list[str] = field(default_factory=list)


def is_manifest_name(filename: str) -> bool:
    """文件名（取最后一段路径，不区分大小写）是否为 manifest.json。"""
    return filename.replace("\\", "/").rsplit("/", 1)[-1].lower() == MANIFEST_NAME


def manifest_to_sql(text: str) -> DbtConversion:
    """把 manifest.json 文本转成 SQL。规则：
    - JSON 解析失败，或 `metadata.dbt_schema_version` 不含 "manifest"，抛 Invalid("不是有效的 dbt manifest.json")。
    - 按 unique_id 排序遍历 `nodes`，只处理 resource_type ∈ CONVERTED_TYPES 的节点：
      - `config.materialized == "ephemeral"` 跳过（它已作为 CTE 内联进下游模型的编译 SQL），计数；
      - 编译 SQL 取 `compiled_code`，没有再取 `compiled_sql`；都没有时跳过并计数；
      - 目标取 `relation_name`；为空时用 database、schema、alias（没有 alias 用 name）中非空的部分以 "." 连接；
      - `config.materialized == "view"` 生成 CREATE VIEW，其余（table、incremental、snapshot 等）生成 CREATE TABLE；
      - 每个模型输出一行来源注释 `-- dbt <resource_type> <unique_id> (<original_file_path>)`，
        然后是 `<CREATE …> <目标> AS`、去掉首尾空白与末尾分号的编译 SQL、单独一行的 `;`；模型之间空一行。
    - 一个模型都没转出来时抛 Invalid("manifest.json 里没有编译后的 SQL，请先运行 dbt compile 或 dbt build")。
    - notes（中文，按顺序，计数为 0 的不写）：
      `已从 dbt manifest 转换 {n} 个模型（适配器：{adapter_type}）`（metadata.adapter_type 缺失时写“未知”）、
      `已跳过 {n} 个 ephemeral 模型（已内联到下游模型）`、`已跳过 {n} 个没有编译 SQL 的模型`。
    """
    try:
        manifest = json.loads(text)
    except (ValueError, RecursionError) as exc:
        raise Invalid("不是有效的 dbt manifest.json") from exc
    metadata = manifest.get("metadata") if isinstance(manifest, dict) else None
    version = metadata.get("dbt_schema_version") if isinstance(metadata, dict) else None
    if not isinstance(version, str) or "manifest" not in version:
        raise Invalid("不是有效的 dbt manifest.json")

    nodes = manifest.get("nodes", {})
    if not isinstance(nodes, dict):
        raise Invalid("不是有效的 dbt manifest.json")

    statements: list[str] = []
    ephemeral = uncompiled = 0
    for unique_id, node in sorted(nodes.items()):
        if not isinstance(node, dict) or node.get("resource_type") not in CONVERTED_TYPES:
            continue
        config = node.get("config")
        materialized = config.get("materialized") if isinstance(config, dict) else None
        if materialized == "ephemeral":
            ephemeral += 1
            continue

        sql = ""
        for key in ("compiled_code", "compiled_sql"):
            code = node.get(key)
            if isinstance(code, str):
                sql = code.strip().rstrip(";").rstrip()
                if sql:
                    break
        if not sql:
            uncompiled += 1
            continue

        target = node.get("relation_name") or ".".join(
            part
            for part in (node.get("database"), node.get("schema"), node.get("alias") or node.get("name"))
            if isinstance(part, str) and part
        )
        kind = "VIEW" if materialized == "view" else "TABLE"
        path = node.get("original_file_path") or ""
        statements.append(f"-- dbt {node['resource_type']} {unique_id} ({path})\nCREATE {kind} {target} AS\n{sql}\n;\n")

    models = len(statements)
    if not models:
        raise Invalid("manifest.json 里没有编译后的 SQL，请先运行 dbt compile 或 dbt build")
    adapter = metadata.get("adapter_type") or "未知"
    notes = [f"已从 dbt manifest 转换 {models} 个模型（适配器：{adapter}）"]
    if ephemeral:
        notes.append(f"已跳过 {ephemeral} 个 ephemeral 模型（已内联到下游模型）")
    if uncompiled:
        notes.append(f"已跳过 {uncompiled} 个没有编译 SQL 的模型")
    return DbtConversion("\n".join(statements), models, notes)
