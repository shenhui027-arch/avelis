"""Trino 连接器（真实数据库，HTTP 协议，驱动 trino-python-client）。按 2026-11-28 在 Trino 482 上的实机核验实现。

- 一个数据源对应一个 Trino Catalog（选项 catalog），采集其中配置的模式（选项 schemas）。资产名三段：Catalog.模式.表，
  与 PostgreSQL 的“库.模式.表”同形；生成的 SQL 写全三段。视图里引用的其他 Catalog 的表记为未采集的来源，
  可以为那个 Catalog 另建数据源。
- 采集只读元数据，不读业务数据：
  - 表与视图：<catalog>.information_schema.tables（table_type 为 BASE TABLE / VIEW）；
  - 列：system.jdbc.columns（type_name、is_nullable、remarks 即列注释、ordinal_position）——information_schema.columns
    没有注释列；
  - 表注释：system.metadata.table_comments；
  - 视图定义：<catalog>.information_schema.views.view_definition。Trino 保存的是规范化后的查询文本，表名保持建视图时的
    写法（不写 Catalog 与模式时相对视图所在的 Catalog 与模式），按 trino 方言解析，默认 Catalog 与模式即视图自己的。
  - 物化视图（system.metadata.materialized_views）需要 Iceberg 等连接器，联调环境没有，未接入：按普通表采集。
- 键：Trino 不暴露主键与唯一约束，不报告键。物理身份：没有表 ID，表与视图按名称对应。
  实例身份：只记录协调节点版本与地址，不给 instance_id。
- 问数执行（Trino 没有只读会话；只读依赖 Avelis 的单条 SELECT 校验与只有 SELECT 权限的查询账号，即 Trino 访问控制）：
  - 超时：会话属性 query_max_run_time（含排队时间，服务端强制，超时报 EXCEEDED_TIME_LIMIT）；客户端请求超时比它多
    CLIENT_SLACK_SECONDS，只作兜底；
  - 内存：mem_limit_bytes > 0 时设会话属性 query_max_memory（超出报 EXCEEDED_GLOBAL_MEMORY_LIMIT）；
  - 行数：fetchmany(max_rows + 1)，取够后取消剩余的查询；
  - 业务时区：连接参数 timezone（X-Trino-Time-Zone）。
- 认证：NONE（只传用户名，服务端未开启认证时）与 PASSWORD（HTTP Basic，Trino 要求走 HTTPS）。
- TLS：tls_mode 为 disable / prefer 时走 http（Trino 不做协商升级，prefer 等同 disable）；require 走 https 不校验证书；
  verify-ca / verify-full 走 https 并校验（tls_ca_cert 给出 CA 文件时用它校验）。
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import closing, contextmanager, suppress
from typing import Any

from requests.exceptions import RequestException, Timeout

from avelis.connectors.base import ObservedAsset, ObservedColumn, QueryError, QueryResult, Snapshot, checked_timezone
from avelis.connectors.tls import parse_tls_options
from avelis.i18n import gettext

IDENT = re.compile(r"^[A-Za-z0-9_]+$")
AUTH_MODES = ("NONE", "PASSWORD")
DEFAULT_USER = "avelis"
CLIENT_SLACK_SECONDS = 15  # 客户端请求超时比服务端 query_max_run_time 多出的秒数
# Trino 的错误名 → Avelis 的错误类别；未列出的归为 other
ERROR_KINDS = {
    "TABLE_NOT_FOUND": "invalid_object",
    "COLUMN_NOT_FOUND": "invalid_object",
    "SCHEMA_NOT_FOUND": "invalid_object",
    "CATALOG_NOT_FOUND": "invalid_object",
    "PERMISSION_DENIED": "permission",
    "EXCEEDED_TIME_LIMIT": "timeout",
}
KINDS = {"BASE TABLE": "table", "VIEW": "view"}
_COLUMN_METADATA_SQL = (
    "SELECT table_schem, table_name, column_name, type_name, is_nullable, remarks, ordinal_position "
    "FROM system.jdbc.columns "
)


def _checked(names: list[str], label: str) -> list[str]:
    for name in names:
        if not IDENT.fullmatch(name):
            raise ValueError(f"{label} 名称不合法：{name!r}")
    return names


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


class TrinoConnector:
    kind = "trino"
    dialect = "trino"
    is_test_adapter = False

    def __init__(self, host: str, port: int, user: str, password: str, options: dict[str, Any] | None = None):
        self.options = options or {}
        auth = str(self.options.get("auth") or "NONE").upper()
        if auth not in AUTH_MODES:
            raise ValueError(f"Trino 认证方式不支持：{auth}（可选 {'、'.join(AUTH_MODES)}）")
        self.auth = auth
        self.catalog = str(self.options.get("catalog") or "").strip()
        self.host, self.port = host, port
        self.user, self.password = user or DEFAULT_USER, password
        self.tls = parse_tls_options(self.options)
        if auth == "PASSWORD":
            if not (user and password):
                raise ValueError("Trino 认证方式 PASSWORD 需要账号与口令")
            if self.tls.mode in ("disable", "prefer"):
                raise ValueError(
                    "Trino 认证方式 PASSWORD 需要 HTTPS：请把 TLS 模式设为 require、verify-ca 或 verify-full"
                )

    def _connect(self, *, timeout_seconds: int = 60, session_properties: dict[str, str] | None = None, timezone=None):
        """建一个 trino.dbapi 连接：http / https 与证书校验按 tls_mode，PASSWORD 用 BasicAuthentication。"""
        import trino

        http_scheme = "http" if self.tls.mode in ("disable", "prefer") else "https"
        tls_kwargs = {}
        if http_scheme == "https":
            tls_kwargs["verify"] = False if self.tls.mode == "require" else self.tls.ca_cert or True
        auth = trino.auth.BasicAuthentication(self.user, self.password) if self.auth == "PASSWORD" else None
        return trino.dbapi.connect(
            host=self.host,
            port=self.port,
            user=self.user,
            auth=auth,
            http_scheme=http_scheme,
            catalog=self.catalog or None,
            request_timeout=timeout_seconds,
            session_properties=session_properties or {},
            timezone=timezone,
            source="avelis",
            **tls_kwargs,
        )

    def _checked_catalog(self) -> str:
        if not IDENT.fullmatch(self.catalog):
            raise ValueError("数据源未配置 Trino Catalog（options.catalog）")
        return self.catalog

    @contextmanager
    def _metadata_cursor(self, **connect_kwargs: Any) -> Iterator[Any]:
        with closing(self._connect(**connect_kwargs)) as conn:
            cur = conn.cursor()
            try:
                yield cur
            finally:
                close = getattr(cur, "close", None)
                if close is not None:
                    with suppress(Exception):
                        close()

    # --- 采集 ---------------------------------------------------------------------

    def server_identity(self) -> dict[str, Any]:
        """协调节点版本与地址；不给 instance_id（Trino 没有集群 ID）。"""
        from trino.exceptions import Error, HttpError

        try:
            with self._metadata_cursor() as cur:
                return self._server_identity(cur)
        except (Error, HttpError, RequestException) as exc:
            raise RuntimeError(_message(exc)) from exc

    def _server_identity(self, cur: Any) -> dict[str, Any]:
        cur.execute("SELECT node_version FROM system.runtime.nodes WHERE coordinator LIMIT 1")
        rows = cur.fetchall()
        version = str(rows[0][0]) if rows and rows[0][0] is not None else None
        return {"version": version, "endpoint": f"{self.host}:{self.port}", "auth": self.auth}

    def snapshot(self) -> Snapshot:
        """采集 catalog 中配置的模式：表、视图、列、列注释、表注释与视图定义。见模块说明“采集”。"""
        from trino.exceptions import Error, HttpError

        catalog = self._checked_catalog()
        schemas = _checked(list(self.options.get("schemas") or []), "模式")
        if not schemas:
            raise ValueError("数据源未配置要采集的模式（options.schemas）")
        info_schema = f"{_quote(catalog)}.information_schema"
        catalog_literal = _literal(catalog)
        schema_list = ", ".join(_literal(schema) for schema in schemas)

        try:
            with self._metadata_cursor() as cur:
                identity = self._server_identity(cur)
                cur.execute(
                    f"SELECT schema_name FROM {info_schema}.schemata "
                    f"WHERE catalog_name = {catalog_literal} AND schema_name IN ({schema_list})"
                )
                visible = {schema for (schema,) in cur.fetchall()}
                missing = sorted(set(schemas) - visible)
                if missing:
                    raise RuntimeError(f"Trino 模式不可见：{'、'.join(missing)}")

                cur.execute(
                    f"SELECT table_schema, table_name, table_type FROM {info_schema}.tables "
                    f"WHERE table_catalog = {catalog_literal} AND table_schema IN ({schema_list})"
                )
                tables = cur.fetchall()
                cur.execute(
                    f"{_COLUMN_METADATA_SQL}WHERE table_cat = {catalog_literal} AND table_schem IN ({schema_list})"
                )
                column_rows = cur.fetchall()
                cur.execute(
                    "SELECT schema_name, table_name, comment FROM system.metadata.table_comments "
                    f"WHERE catalog_name = {catalog_literal} AND schema_name IN ({schema_list})"
                )
                comments = {(schema, name): comment for schema, name, comment in cur.fetchall()}
                cur.execute(
                    f"SELECT table_schema, table_name, view_definition FROM {info_schema}.views "
                    f"WHERE table_catalog = {catalog_literal} AND table_schema IN ({schema_list})"
                )
                definitions = {(schema, name): definition for schema, name, definition in cur.fetchall()}
        except (Error, HttpError, RequestException) as exc:
            raise RuntimeError(_message(exc)) from exc

        columns: dict[tuple[str, str], list[ObservedColumn]] = {}
        for schema, name, column, data_type, nullable, remarks, ordinal in column_rows:
            columns.setdefault((schema, name), []).append(
                ObservedColumn(column, data_type, ordinal, nullable=nullable == "YES", comment=remarks or None)
            )

        assets: list[ObservedAsset] = []
        warnings: list[str] = []
        for schema, name, table_type in tables:
            key = (schema, name)
            qualified = f"{catalog}.{schema}.{name}"
            kind = KINDS.get(table_type, "table")
            definition = definitions.get(key) or None
            if table_type not in KINDS:
                warnings.append(f"{qualified} 的类型 {table_type} 按普通表采集")
            if kind == "view" and definition is None:
                warnings.append(gettext("Trino 视图 {name} 未读取到定义；列级血缘不可用", name=qualified))
            assets.append(
                ObservedAsset(
                    catalog=catalog,
                    db=schema,
                    name=name,
                    kind=kind,
                    columns=sorted(columns.get(key, []), key=lambda column: column.ordinal),
                    comment=comments.get(key) or None,
                    definition=definition,
                )
            )
        return Snapshot(server_identity=identity, assets=assets, warnings=warnings)

    # --- 问数执行与执行前核对 -------------------------------------------------------

    def execute_select(
        self, sql: str, *, timeout_seconds: int, mem_limit_bytes: int, max_rows: int, timezone: str | None = None
    ) -> QueryResult:
        """执行调用层已校验的一条 SELECT。见模块说明“问数执行”。"""
        from trino.exceptions import Error, HttpError

        if timeout_seconds <= 0:
            raise QueryError("timeout", None, gettext("超时值必须为正数，未执行"))
        if max_rows < 0:
            raise QueryError("other", None, gettext("查询行数上限不能为负数，未执行"))
        if timezone:
            timezone = checked_timezone(timezone)
        session_properties = {"query_max_run_time": f"{timeout_seconds}s"}
        if mem_limit_bytes > 0:
            session_properties["query_max_memory"] = f"{int(mem_limit_bytes)}B"

        conn = None
        try:
            conn = self._connect(
                timeout_seconds=timeout_seconds + CLIENT_SLACK_SECONDS,
                session_properties=session_properties,
                timezone=timezone,
            )
            cur = conn.cursor()
            cur.execute(sql)
            rows = cur.fetchmany(max_rows + 1)
            columns = [entry[0] for entry in cur.description or []]
            truncated = len(rows) > max_rows
            if truncated:
                with suppress(Exception):
                    cur.cancel()
            return QueryResult(columns, [list(row) for row in rows[:max_rows]], truncated=truncated)
        except (Error, HttpError, RequestException) as exc:
            raise self._query_error(exc) from exc
        finally:
            if conn is not None:
                with suppress(Exception):
                    conn.close()

    def execute_explore(
        self,
        sql: str,
        *,
        timeout_seconds: int,
        max_rows: int,
        mem_limit_bytes: int = 0,
        timezone: str | None = None,
    ) -> QueryResult:
        """执行校验后重新生成的探索查询：与 execute_select 相同的限制。"""
        return self.execute_select(
            sql, timeout_seconds=timeout_seconds, mem_limit_bytes=mem_limit_bytes, max_rows=max_rows, timezone=timezone
        )

    def describe_objects(
        self, objects: list[tuple[str, str]], *, timeout_seconds: int | None = None
    ) -> dict[tuple[str, str], dict[str, Any]]:
        """执行前核对当前列与视图定义：(模式, 名) → 与采集同样来源的列与定义。"""
        from trino.exceptions import Error, HttpError

        if timeout_seconds is not None and timeout_seconds <= 0:
            raise QueryError("timeout", None, gettext("超时值必须为正数，未执行"))
        out: dict[tuple[str, str], dict[str, Any]] = {
            key: {"columns": [], "definition": None, "physical_id": None} for key in objects
        }
        valid = {(schema, name) for schema, name in out if IDENT.fullmatch(schema) and IDENT.fullmatch(name)}
        if not valid:
            return out

        catalog = self._checked_catalog()
        catalog_literal = _literal(catalog)
        pairs = ", ".join(f"ROW({_literal(schema)}, {_literal(name)})" for schema, name in sorted(valid))
        connect_kwargs = {}
        if timeout_seconds is not None:
            connect_kwargs = {
                "timeout_seconds": timeout_seconds + CLIENT_SLACK_SECONDS,
                "session_properties": {"query_max_run_time": f"{timeout_seconds}s"},
            }

        try:
            with self._metadata_cursor(**connect_kwargs) as cur:
                cur.execute(
                    f"{_COLUMN_METADATA_SQL}WHERE table_cat = {catalog_literal} "
                    f"AND ROW(table_schem, table_name) IN ({pairs})"
                )
                for schema, name, column, data_type, *_ in sorted(cur.fetchall(), key=lambda row: row[6]):
                    key = (schema, name)
                    if key in valid:
                        out[key]["columns"].append([column, data_type])
                cur.execute(
                    f"SELECT table_schema, table_name, view_definition FROM {_quote(catalog)}.information_schema.views "
                    f"WHERE table_catalog = {catalog_literal} AND ROW(table_schema, table_name) IN ({pairs})"
                )
                for schema, name, definition in cur.fetchall():
                    key = (schema, name)
                    if key in valid:
                        out[key]["definition"] = definition or None
        except (Error, HttpError, RequestException) as exc:
            raise self._query_error(exc) from exc
        return out

    def freshness(self, tables: list[tuple[str, str]]) -> dict[str, Any]:
        """Trino 不提供跨连接器一致的数据更新时间，不报告数据新鲜度。"""
        return {}

    @staticmethod
    def _query_error(exc: Exception) -> QueryError:
        """驱动异常归类：TrinoQueryError 按 error_name（ERROR_KINDS），db_errno 记 error_name，原文用 message。"""
        from trino.exceptions import TrinoConnectionError, TrinoQueryError

        if isinstance(exc, TrinoQueryError):
            error_name = exc.error_name
            return QueryError(ERROR_KINDS.get(error_name or "", "other"), error_name, (exc.message or str(exc))[:2000])
        # 驱动会把请求异常包装成 TrinoConnectionError，原异常留在异常链里。
        if isinstance(exc, Timeout) or (
            isinstance(exc, TrinoConnectionError) and isinstance(exc.__cause__ or exc.__context__, Timeout)
        ):
            return QueryError("timeout", None, _message(exc))
        return QueryError("other", None, _message(exc))


def _message(exc: Exception) -> str:
    """驱动异常的原文：TrinoQueryError 取 message，其余取 str(exc)，截到 2000 字。"""
    text = getattr(exc, "message", None) or str(exc)
    return str(text).strip()[:2000]


__all__ = ["AUTH_MODES", "ERROR_KINDS", "TrinoConnector"]
