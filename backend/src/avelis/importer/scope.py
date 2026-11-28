"""导入血缘里“未匹配”的读取来源与数据源采集范围的关系。

血缘来自数据库本身：读取来源没对上已采集资产时，不手工登记资产，而是判断它所在的库是否在数据源的采集范围内；
不在范围内时可一键加入范围并采集，采集回来后血缘自然对上。范围按读取时数据源的当前配置判断（导入之后范围可能已改）。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from avelis import audit
from avelis.models import Datasource, User

# 引擎配置项里表示采集范围的键：库（StarRocks / MySQL / Doris / ClickHouse / Hive）或模式（PostgreSQL / SQL Server / Oracle）
SCOPE_KEYS = ("databases", "schemas")
# 一次最多加入的库 / 模式数，防止误操作把整批未匹配都加进来
MAX_ADD = 20


def scope_key(engine: Any) -> str | None:
    """engine.options 中第一个出现的范围键；引擎没有范围配置（如测试适配器）时返回 None。"""
    # TODO(codex): 按 SCOPE_KEYS 顺序在 engine.options（OptionField，属性 key）里查找
    raise NotImplementedError


def ref_database(ref: str) -> str | None:
    """表引用里的库 / 模式名：取按点分隔后的倒数第二段（`db.table`、`catalog.db.table`），去掉反引号、双引号、方括号；
    只有一段（没有库名）时返回 None。"""
    # TODO(codex): 实现
    raise NotImplementedError


def annotate_unmatched(items: list[dict], datasource: Datasource, engine: Any) -> list[dict]:
    """给未匹配条目补充范围信息，返回新列表（不修改入参）：每条加 `database`（ref_database 的结果）；
    role == "source" 的条目再加 `in_scope`（库在当前范围内，不区分大小写）与 `addable`（引擎有范围键、库名非空且不在范围内）。
    role == "target"（写入目标，多是尚未建表的设计时目标）只加 database。"""
    # TODO(codex): 实现
    raise NotImplementedError


def add_to_scope(session: Session, user: User, datasource: Datasource, engine: Any, names: list[str]) -> list[str]:
    """把库 / 模式加入数据源采集范围，返回实际新增的名字（按传入顺序去重，已在范围内的跳过，比较不区分大小写）。
    - 引擎没有范围键时抛 ValueError；names 为空、超过 MAX_ADD 或含空白名时抛 ValueError。
    - Oracle（engine.kind == "oracle"）的模式名按大写保存，其余原样保存。
    - 写回时给 datasource.options 赋一个新的 dict（JSON 列需要整体替换才会被识别为变更）。
    - 有新增时记审计：audit.record(session, user, "datasource.scope_add", "datasource", datasource.id, key=<范围键>, added=<新增列表>)。
    不负责提交事务，也不触发采集（由接口层决定）。"""
    # TODO(codex): 实现
    raise NotImplementedError
