"""StarRocks 连接器（真实数据库）。按 docs/starrocks-notes.md 的实机核验结论实现。

- 视图定义取 information_schema.views.VIEW_DEFINITION（规范化后全限定）；
- 内表身份用 tables_config.TABLE_ID；视图与外部表 TABLE_ID 为 0，不作为身份；
- 旧式 ENGINE=mysql 外部表从 SHOW CREATE TABLE 读取 host/port/database/table（口令被 StarRocks 置空）；
- JDBC Catalog 从 SHOW CREATE CATALOG 读取 jdbc_uri（不显示口令），表结构从 <catalog>.information_schema 读取；
- 问数执行：会话设置 query_timeout 与 query_mem_limit，错误 5024 归为超时。
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlsplit

import pymysql

from avelis.connectors.base import ObservedAsset, ObservedColumn, QueryError, QueryResult, Snapshot

IDENT = re.compile(r"^[A-Za-z0-9_\-]+$")
PROP = re.compile(r'"([A-Za-z0-9_.]+)"\s*=\s*"((?:[^"\\]|\\.)*)"')
INTERNAL_CATALOG = "default_catalog"


def quote_ident(name: str) -> str:
    return "`" + name.replace("`", "``") + "`"


def _checked(names: list[str], label: str) -> list[str]:
    for name in names:
        if not IDENT.match(name):
            raise ValueError(f"{label} 名称不合法：{name!r}")
    return names


def _in_list(names: list[str]) -> str:
    return ",".join("'" + n + "'" for n in names)


def parse_properties(ddl: str) -> dict[str, str]:
    return {k: v for k, v in PROP.findall(ddl)}


def parse_jdbc_uri(uri: str) -> dict[str, Any]:
    """jdbc:mysql://host:port[/db]?... → 端点。"""
    if not uri.startswith("jdbc:"):
        return {}
    parts = urlsplit(uri[len("jdbc:"):])
    return {"scheme": parts.scheme, "host": parts.hostname, "port": parts.port or 3306,
            "database": parts.path.strip("/") or None}


class StarRocksConnector:
    kind = "starrocks"
    dialect = "starrocks"
    is_test_adapter = False

    def __init__(self, host: str, port: int, user: str, password: str, options: dict[str, Any] | None = None):
        self._conn_args = dict(host=host, port=port, user=user, password=password)
        self.options = options or {}

    def _connect(self, read_timeout: int = 60) -> pymysql.Connection:
        return pymysql.connect(
            **self._conn_args, autocommit=True, charset="utf8mb4", connect_timeout=5,
            read_timeout=read_timeout, write_timeout=30, cursorclass=pymysql.cursors.DictCursor,
        )

    # --- 采集 ---------------------------------------------------------------------

    def server_identity(self) -> dict[str, Any]:
        conn = self._connect()
        try:
            return self._server_identity(conn.cursor())
        finally:
            conn.close()

    def _server_identity(self, cur) -> dict[str, Any]:
        cur.execute("SELECT current_version() AS v")
        identity: dict[str, Any] = {"version": cur.fetchone()["v"],
                                    "endpoint": f"{self._conn_args['host']}:{self._conn_args['port']}"}
        try:
            cur.execute("SHOW FRONTENDS")
            rows = cur.fetchall()
            if rows:
                identity["cluster_id"] = str(rows[0].get("ClusterId"))
        except pymysql.MySQLError:
            identity["cluster_id"] = None  # 需要 OPERATE 或 NODE 权限；默认不授予
        return identity

    def snapshot(self) -> Snapshot:
        databases = _checked(list(self.options.get("databases", [])), "库")
        catalogs = _checked(list(self.options.get("catalogs", [])), "Catalog")
        if not databases:
            raise ValueError("数据源未配置要采集的库（options.databases）")
        conn = self._connect(read_timeout=120)
        warnings: list[str] = []
        try:
            cur = conn.cursor()
            identity = self._server_identity(cur)
            assets = self._internal_assets(cur, databases, warnings)
            catalog_info = []
            for catalog in catalogs:
                info, catalog_assets = self._catalog_assets(cur, catalog, warnings)
                catalog_info.append(info)
                assets.extend(catalog_assets)
        finally:
            conn.close()
        return Snapshot(server_identity=identity, assets=assets, catalogs=catalog_info, warnings=warnings)

    def _internal_assets(self, cur, databases: list[str], warnings: list[str]) -> list[ObservedAsset]:
        in_dbs = _in_list(databases)
        cur.execute(
            "SELECT TABLE_SCHEMA, TABLE_NAME, TABLE_TYPE, ENGINE, CREATE_TIME, TABLE_COMMENT "
            f"FROM information_schema.tables WHERE TABLE_SCHEMA IN ({in_dbs})"
        )
        tables = cur.fetchall()
        cur.execute(
            "SELECT TABLE_SCHEMA, TABLE_NAME, TABLE_ENGINE, TABLE_MODEL, PRIMARY_KEY, SORT_KEY, TABLE_ID "
            f"FROM information_schema.tables_config WHERE TABLE_SCHEMA IN ({in_dbs})"
        )
        config = {(r["TABLE_SCHEMA"], r["TABLE_NAME"]): r for r in cur.fetchall()}
        cur.execute(
            "SELECT TABLE_SCHEMA, TABLE_NAME, COLUMN_NAME, ORDINAL_POSITION, COLUMN_TYPE, IS_NULLABLE, "
            f"COLUMN_COMMENT, COLUMN_KEY FROM information_schema.columns WHERE TABLE_SCHEMA IN ({in_dbs})"
        )
        columns: dict[tuple[str, str], list[ObservedColumn]] = {}
        for r in cur.fetchall():
            columns.setdefault((r["TABLE_SCHEMA"], r["TABLE_NAME"]), []).append(ObservedColumn(
                name=r["COLUMN_NAME"], data_type=r["COLUMN_TYPE"], ordinal=int(r["ORDINAL_POSITION"]),
                nullable=r["IS_NULLABLE"] == "YES", comment=r["COLUMN_COMMENT"] or None,
                is_key=bool(r["COLUMN_KEY"]),
            ))
        cur.execute(
            "SELECT TABLE_SCHEMA, TABLE_NAME, VIEW_DEFINITION "
            f"FROM information_schema.views WHERE TABLE_SCHEMA IN ({in_dbs})"
        )
        views = {(r["TABLE_SCHEMA"], r["TABLE_NAME"]): r["VIEW_DEFINITION"] for r in cur.fetchall()}

        assets = []
        for t in tables:
            key = (t["TABLE_SCHEMA"], t["TABLE_NAME"])
            cfg = config.get(key, {})
            table_type, engine = t["TABLE_TYPE"], (t["ENGINE"] or "")
            props: dict[str, Any] = {"create_time": str(t["CREATE_TIME"]) if t["CREATE_TIME"] else None,
                                     "engine": engine or cfg.get("TABLE_ENGINE") or None}
            definition = None
            if table_type == "VIEW":
                kind = "view"
                definition = views.get(key)
                if definition is None:
                    warnings.append(f"视图 {key[0]}.{key[1]} 未读到定义（权限不足或已删除）")
            elif table_type == "MATERIALIZED VIEW":
                kind = "materialized_view"
            elif engine.upper() == "MYSQL":
                kind = "external_table"
                props["external"] = self._mysql_external(cur, *key, warnings)
            else:
                kind = "table"
            table_id = cfg.get("TABLE_ID") or 0
            model = cfg.get("TABLE_MODEL") or None
            pk = [c.strip().strip("`") for c in (cfg.get("PRIMARY_KEY") or "").split(",") if c.strip()]
            assets.append(ObservedAsset(
                catalog=INTERNAL_CATALOG, db=key[0], name=key[1], kind=kind,
                columns=sorted(columns.get(key, []), key=lambda c: c.ordinal),
                physical_id=str(table_id) if kind == "table" and table_id else None,
                table_model=model, key_columns=pk if model in ("PRIMARY_KEYS", "UNIQUE_KEYS") else [],
                unique_keys=[pk] if pk and model in ("PRIMARY_KEYS", "UNIQUE_KEYS") else [],
                comment=t["TABLE_COMMENT"] or None, definition=definition, properties=props,
            ))
        return assets

    def _mysql_external(self, cur, db: str, name: str, warnings: list[str]) -> dict[str, Any] | None:
        try:
            cur.execute(f"SHOW CREATE TABLE {quote_ident(db)}.{quote_ident(name)}")
            ddl = cur.fetchone()["Create Table"]
        except pymysql.MySQLError as exc:
            warnings.append(f"外部表 {db}.{name} 无法读取定义：{exc.args[-1]}")
            return None
        props = parse_properties(ddl)
        return {"engine": "mysql", "host": props.get("host"), "port": int(props.get("port") or 3306),
                "database": props.get("database"), "table": props.get("table"), "via": "external_table"}

    def _catalog_assets(self, cur, catalog: str, warnings: list[str]) -> tuple[dict, list[ObservedAsset]]:
        cur.execute(f"SHOW CREATE CATALOG {quote_ident(catalog)}")
        props = parse_properties(cur.fetchone()["Create Catalog"])
        endpoint = parse_jdbc_uri(props.get("jdbc_uri", ""))
        info = {"name": catalog, "type": props.get("type"), "endpoint": endpoint,
                "driver_class": props.get("driver_class"), "driver_checksum": props.get("checksum")}
        if props.get("type") != "jdbc" or endpoint.get("scheme") != "mysql":
            warnings.append(f"Catalog {catalog} 类型 {props.get('type')} 未接入（v0.1 只接 JDBC MySQL）")
            return info, []
        wanted = _checked(list(self.options.get("catalog_databases", {}).get(catalog, [])), "Catalog 库")
        q = quote_ident(catalog)
        where = f"TABLE_SCHEMA IN ({_in_list(wanted)})" if wanted else \
            "TABLE_SCHEMA NOT IN ('information_schema','mysql','performance_schema','sys')"
        cur.execute(f"SELECT TABLE_SCHEMA, TABLE_NAME, TABLE_TYPE, TABLE_COMMENT FROM {q}.information_schema.tables "
                    f"WHERE {where}")
        tables = cur.fetchall()
        cur.execute(f"SELECT TABLE_SCHEMA, TABLE_NAME, COLUMN_NAME, ORDINAL_POSITION, COLUMN_TYPE, IS_NULLABLE "
                    f"FROM {q}.information_schema.columns WHERE {where}")
        columns: dict[tuple[str, str], list[ObservedColumn]] = {}
        for r in cur.fetchall():
            columns.setdefault((r["TABLE_SCHEMA"], r["TABLE_NAME"]), []).append(ObservedColumn(
                name=r["COLUMN_NAME"], data_type=r["COLUMN_TYPE"], ordinal=int(r["ORDINAL_POSITION"]),
                nullable=r["IS_NULLABLE"] == "YES",
            ))
        assets = []
        for t in tables:
            key = (t["TABLE_SCHEMA"], t["TABLE_NAME"])
            assets.append(ObservedAsset(
                catalog=catalog, db=key[0], name=key[1], kind="catalog_table",
                columns=sorted(columns.get(key, []), key=lambda c: c.ordinal), comment=t["TABLE_COMMENT"] or None,
                properties={"external": {"engine": "mysql", "host": endpoint.get("host"), "port": endpoint.get("port"),
                                         "database": key[0], "table": key[1], "via": f"catalog:{catalog}"}},
            ))
        return info, assets

    # --- 只读执行 ------------------------------------------------------------------

    def execute_select(self, sql: str, *, timeout_seconds: int, mem_limit_bytes: int, max_rows: int) -> QueryResult:
        conn = self._connect(read_timeout=timeout_seconds + 15)
        try:
            cur = conn.cursor(pymysql.cursors.Cursor)
            cur.execute(f"SET query_timeout = {int(timeout_seconds)}")
            cur.execute(f"SET query_mem_limit = {int(mem_limit_bytes)}")
            cur.execute(sql)
            columns = [d[0] for d in cur.description or []]
            rows = cur.fetchmany(max_rows + 1)
            truncated = len(rows) > max_rows
            return QueryResult(columns=columns, rows=[list(r) for r in rows[:max_rows]], truncated=truncated)
        except pymysql.MySQLError as exc:
            errno = exc.args[0] if exc.args and isinstance(exc.args[0], int) else None
            message = str(exc.args[-1]) if exc.args else str(exc)
            raise QueryError(_classify(errno, message), errno, message) from exc
        finally:
            conn.close()

    def freshness(self, tables: list[tuple[str, str]]) -> dict[str, Any]:
        """内表最近可见版本时间（partitions_meta.VISIBLE_VERSION_TIME，StarRocks 会话时区）。"""
        if not tables:
            return {}
        conn = self._connect()
        try:
            cur = conn.cursor()
            out: dict[str, Any] = {}
            for db, name in tables:
                if not (IDENT.match(db) and IDENT.match(name)):
                    continue
                cur.execute("SELECT MAX(VISIBLE_VERSION_TIME) AS t FROM information_schema.partitions_meta "
                            "WHERE DB_NAME = %s AND TABLE_NAME = %s", (db, name))
                row = cur.fetchone()
                out[f"{db}.{name}"] = str(row["t"]) if row and row["t"] else None
            return out
        finally:
            conn.close()


def _classify(errno: int | None, message: str) -> str:
    if errno == 5024 or "timeout" in message.lower():
        return "timeout"
    if errno in (1044, 1045, 1142, 1143, 5203) or "Access denied" in message:
        return "permission"
    if errno == 1064 and "cannot be resolved" in message or errno in (1051, 1054, 1146):
        return "invalid_object"
    return "other"
