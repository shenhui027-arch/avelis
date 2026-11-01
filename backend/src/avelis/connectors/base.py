"""连接器接口与采集快照。

连接器只读：采集用 collect 账号读元数据，问数用 query 账号执行已校验的 SELECT。
快照只含元数据，不含业务数据，也不含任何口令。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from avelis.i18n import gettext


@dataclass
class ObservedColumn:
    name: str
    data_type: str
    ordinal: int
    nullable: bool = True
    comment: str | None = None
    is_key: bool = False


@dataclass
class ObservedAsset:
    catalog: str
    db: str
    name: str
    kind: str  # table | view | materialized_view | external_table | catalog_table
    columns: list[ObservedColumn]
    physical_id: str | None = None
    table_model: str | None = None
    key_columns: list[str] = field(default_factory=list)
    unique_keys: list[list[str]] = field(default_factory=list)
    comment: str | None = None
    definition: str | None = None
    # external：{"host","port","database","table","engine"}；不含口令
    properties: dict[str, Any] = field(default_factory=dict)

    @property
    def qualified(self) -> str:
        return f"{self.catalog}.{self.db}.{self.name}"

    def content_hash(self, definition_hash: str | None) -> str:
        """决定是否产生新的资产版本。数据新鲜度、行数等随数据变化的属性不进入哈希。"""
        payload = {
            "kind": self.kind,
            "name": [self.catalog, self.db, self.name],
            "table_model": self.table_model,
            "keys": self.key_columns,
            "unique": sorted(self.unique_keys),
            "comment": self.comment,
            "columns": [[c.ordinal, c.name, c.data_type, c.nullable, c.comment, c.is_key] for c in self.columns],
            "definition": definition_hash,
            "external": self.properties.get("external"),
        }
        return sha256_json(payload)


@dataclass
class Snapshot:
    server_identity: dict[str, Any]
    assets: list[ObservedAsset]
    catalogs: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    # 审计游标：连接器在读快照的同一事务里记下的位置（如 PostgreSQL pg_current_snapshot()），归因按它判断事务可见性
    audit_cursor: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class QueryResult:
    columns: list[str]
    rows: list[list[Any]]
    truncated: bool
    warnings: list[str] = field(default_factory=list)


class QueryError(Exception):
    """数据库执行错误。code 为归一化后的类别（timeout / permission / invalid_object / other），
    message 保留数据库原始报错，不改写成看似成功的结果。"""

    def __init__(self, code: str, db_errno: int | str | None, message: str):
        super().__init__(message)
        self.code = code
        self.db_errno = db_errno
        self.message = message


class Connector(Protocol):
    kind: str
    dialect: str
    is_test_adapter: bool

    def snapshot(self) -> Snapshot: ...

    def server_identity(self) -> dict[str, Any]: ...


class QueryConnector(Protocol):
    def execute_select(
        self, sql: str, *, timeout_seconds: int, mem_limit_bytes: int, max_rows: int, timezone: str | None = None
    ) -> QueryResult: ...

    def freshness(self, tables: list[tuple[str, str]]) -> dict[str, Any]: ...

    def describe_objects(
        self, objects: list[tuple[str, str]], *, timeout_seconds: int | None = None
    ) -> dict[tuple[str, str], dict[str, Any]]: ...


def sha256_json(payload: Any) -> str:
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def checked_timezone(name: str) -> str:
    """只接受可解析的 IANA 时区，不能把未知名称当成本地时区。"""
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, TypeError) as exc:
        raise QueryError("other", None, gettext("业务时区无效：{name}", name=name)) from exc
    return name


def current_timezone_offset(name: str) -> str:
    """数据库没有时区表时，明确回退到执行时刻的固定偏移。"""
    offset = datetime.now(ZoneInfo(checked_timezone(name))).utcoffset()
    minutes = int(offset.total_seconds() / 60)
    sign = "+" if minutes >= 0 else "-"
    hours, minutes = divmod(abs(minutes), 60)
    return f"{sign}{hours:02d}:{minutes:02d}"
