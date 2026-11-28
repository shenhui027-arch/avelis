"""导入血缘里“未匹配”的读取来源与数据源采集范围的关系。

血缘来自数据库本身：读取来源没对上已采集资产时，不手工登记资产，而是判断它所在的库是否在数据源的采集范围内；
不在范围内时可一键加入范围并采集，采集回来后血缘自然对上。范围按读取时数据源的当前配置判断（导入之后范围可能已改）。
"""

from __future__ import annotations

import re
import sys
from typing import Any

from sqlalchemy.orm import Session

from avelis import audit
from avelis.i18n import Msg
from avelis.models import Datasource, User

# 引擎配置项里表示采集范围的键：库（StarRocks / MySQL / Doris / ClickHouse / Hive）
# 或模式（PostgreSQL / SQL Server / Oracle）
SCOPE_KEYS = ("databases", "schemas")
# 一次最多加入的库 / 模式数，防止误操作把整批未匹配都加进来
MAX_ADD = 20
_DEFAULT_IDENT = re.compile(r"^[A-Za-z0-9_]+$")


def scope_key(engine: Any) -> str | None:
    """engine.options 中第一个出现的范围键；引擎没有范围配置（如测试适配器）时返回 None。"""
    keys = {option.key for option in engine.options}
    return next((key for key in SCOPE_KEYS if key in keys), None)


def ref_database(ref: str) -> str | None:
    """表引用里的库 / 模式名：`db.table` 或 `catalog.db.table` 的库段，去掉反引号、双引号、方括号。
    只有一段（没有库名）或超过三段时返回 None：拍平的引用丢了标识符边界（如表名本身含点），
    无法确定库名，宁可不给加入入口。"""
    parts = ref.split(".")
    if not 2 <= len(parts) <= 3:
        return None
    database = parts[-2].strip().strip('`"[]')
    return database if database.strip() else None


def valid_name(engine: Any, name: str) -> bool:
    """用采集器的标识符规则检查库 / 模式名，Oracle 按保存时的大写形式检查。"""
    collector = getattr(engine, "collector", None)
    module = sys.modules.get(getattr(collector, "__module__", ""))
    candidate = name.upper() if engine.kind == "oracle" else name
    # 优先用采集器实际调用的 _checked（MySQL 从 StarRocks 模块导入，自身没有 IDENT），其次 IDENT，最后保守规则
    checked = getattr(module, "_checked", None)
    if callable(checked):
        try:
            checked([candidate], "")
        except ValueError:
            return False
        return True
    ident = getattr(module, "IDENT", _DEFAULT_IDENT)
    return ident.fullmatch(candidate) is not None


def _scope_name(engine: Any, name: str) -> str:
    """范围名的比较形式：采集器按配置原样匹配（PostgreSQL 的 "Sales" 与 sales 是不同模式），所以精确比较；
    只有 Oracle 的模式名按大写保存与比较。"""
    return name.upper() if engine.kind == "oracle" else name


def _configured_names(datasource: Datasource, key: str) -> list[str]:
    names = (datasource.options or {}).get(key)
    # 兼容以字符串保存的范围，新增时按采集器要求写成列表。
    if isinstance(names, str):
        return [name for part in names.split(",") if (name := part.strip())]
    return names if isinstance(names, list) else []


def annotate_unmatched(items: list[dict], datasource: Datasource, engine: Any) -> list[dict]:
    """给未匹配条目补充范围信息，返回新列表（不修改入参）：每条加 `database`（ref_database 的结果）；
    role == "source" 的条目再加 `in_scope`（库在当前范围内，按 _scope_name 精确比较）
    与 `addable`（引擎有范围键、库名合法且可加入范围），并按需标出 `external_catalog` 或 `invalid_name`。
    role == "target"（写入目标，多是尚未建表的设计时目标）只加 database。"""
    key = scope_key(engine)
    configured = _configured_names(datasource, key) if key is not None else []
    known = {_scope_name(engine, name) for name in configured if isinstance(name, str)}
    dialect_for = getattr(engine, "dialect_for", None)
    dialect = dialect_for(datasource.options) if dialect_for is not None else None
    default_catalog = getattr(dialect, "default_catalog", None)
    annotated = []
    for item in items:
        ref = item["ref"]
        database = ref_database(ref)
        entry = {**item, "database": database}
        if item["role"] == "source":
            parts = ref.split(".")
            external_catalog = (
                len(parts) == 3
                and default_catalog is not None
                and parts[0].strip().strip('`"[]').casefold() != default_catalog.casefold()
            )
            invalid_name = database is not None and not valid_name(engine, database)
            in_scope = not external_catalog and database is not None and _scope_name(engine, database) in known
            entry["in_scope"] = in_scope
            entry["addable"] = (
                key is not None and database is not None and not (in_scope or external_catalog or invalid_name)
            )
            if external_catalog:
                entry["external_catalog"] = True
            if invalid_name:
                entry["invalid_name"] = True
        annotated.append(entry)
    return annotated


def add_to_scope(session: Session, user: User, datasource: Datasource, engine: Any, names: list[str]) -> list[str]:
    """把库 / 模式加入数据源采集范围，返回实际新增的名字（按传入顺序去重，已在范围内的跳过，按 _scope_name 精确比较）。
    - 引擎没有范围键时抛 ValueError；names 为空、超过 MAX_ADD 或含不合法的库 / 模式名时抛 ValueError。
    - Oracle（engine.kind == "oracle"）的模式名按大写保存，其余原样保存。
    - 写回时给 datasource.options 赋一个新的 dict（JSON 列需要整体替换才会被识别为变更）。
    - 有新增时记审计：audit.record(session, user, "datasource.scope_add", "datasource", datasource.id,
      key=<范围键>, added=<新增列表>)。
    不负责提交事务，也不触发采集（由接口层决定）。"""
    key = scope_key(engine)
    if key is None:
        raise ValueError(Msg("该数据源不支持配置采集范围"))
    if not 1 <= len(names) <= MAX_ADD:
        raise ValueError(Msg("一次只能加入 1 到 {limit} 个库或模式", limit=MAX_ADD))

    configured = _configured_names(datasource, key)
    known = {_scope_name(engine, name) for name in configured if isinstance(name, str)}
    added = []
    for name in names:
        if not valid_name(engine, name):
            raise ValueError(Msg("库或模式名 {name} 不符合采集器的命名规则", name=name))
        stored = _scope_name(engine, name)
        if stored in known:
            continue
        added.append(stored)
        known.add(stored)
    if added:
        # 整体替换才能让 ORM 识别 JSON 列变更。
        datasource.options = {**(datasource.options or {}), key: [*configured, *added]}
        audit.record(session, user, "datasource.scope_add", "datasource", datasource.id, key=key, added=added)
    return added
