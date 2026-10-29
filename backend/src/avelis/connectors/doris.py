"""Apache Doris 连接器（真实数据库，MySQL 协议）。按 Doris 4.1 实机核验结论实现（M7）。

- 资产名三段：internal（内部 Catalog）. 库. 表；只采配置的库（options.databases）。
- 采集账号只需库上的 SHOW_VIEW_PRIV：information_schema 的 tables / columns / views / table_options /
  metadata_name_ids 全部可见，读业务数据会被拒绝。
- 物理身份用 metadata_name_ids.TABLE_ID：表改名不变，删后重建会变；视图 ALTER VIEW、CREATE OR REPLACE VIEW 都不变
  （与 StarRocks 不同，Doris 的视图也有稳定 ID）。
- 视图定义取 information_schema.views.VIEW_DEFINITION，写法与 StarRocks 相同（`internal`.`db`.`alias`.`col`），
  解析前用同一个别名前缀规整。
- 键模型取 table_options.TABLE_MODEL：UNI（唯一键）作为数据库保证的唯一键；DUP、AGG 不作为粒度依据。
- 问数执行：会话设 query_timeout 与 exec_mem_limit；Doris 的错误号统一为 1105，按报错内容归类
  （query timeout 为超时，Access denied / command denied 为权限，Unknown column / does not exist 为对象无法解析）。
- 查询账号需 SELECT_PRIV 与 SHOW_VIEW_PRIV（执行前核对要读视图定义）。
- 数据新鲜度：information_schema.tables.UPDATE_TIME（写入后更新，Doris 会话时区）。
"""

from __future__ import annotations

from typing import Any

import pymysql

from avelis.connectors.base import ObservedAsset, ObservedColumn, QueryError, QueryResult, Snapshot
from avelis.connectors.starrocks import IDENT, _checked, _in_list

INTERNAL_CATALOG = "internal"
UNIQUE_MODELS = ("UNI",)


class DorisConnector:
    kind = "doris"
    dialect = "doris"
    is_test_adapter = False

    def __init__(self, host: str, port: int, user: str, password: str, options: dict[str, Any] | None = None):
        self._conn_args = dict(host=host, port=port, user=user, password=password)
        self.options = options or {}

    def _connect(self, read_timeout: int = 60) -> pymysql.Connection:
        return pymysql.connect(**self._conn_args, autocommit=True, charset="utf8mb4", connect_timeout=5,
                               read_timeout=read_timeout, write_timeout=30, cursorclass=pymysql.cursors.DictCursor)

    # --- 采集 ---------------------------------------------------------------------

    def server_identity(self) -> dict[str, Any]:
        conn = self._connect()
        try:
            return self._server_identity(conn.cursor())
        finally:
            conn.close()

    def _server_identity(self, cur) -> dict[str, Any]:
        cur.execute("SELECT @@version_comment AS comment, version() AS v")
        row = cur.fetchone() or {}
        identity: dict[str, Any] = {"version": row.get("comment") or row.get("v"),
                                    "endpoint": f"{self._conn_args['host']}:{self._conn_args['port']}"}
        try:
            cur.execute("SHOW FRONTENDS")
            rows = cur.fetchall()
            identity["cluster_id"] = str(rows[0].get("ClusterId")) if rows else None
        except pymysql.MySQLError:
            identity["cluster_id"] = None  # 需要 ADMIN / NODE 权限；默认不授予
        if identity.get("cluster_id"):
            identity["instance_id"] = f"doris:{identity['cluster_id']}"
        return identity

    def snapshot(self) -> Snapshot:
        databases = _checked(list(self.options.get("databases", [])), "库")
        if not databases:
            raise ValueError("数据源未配置要采集的库（options.databases）")
        warnings: list[str] = []
        conn = self._connect(read_timeout=120)
        try:
            cur = conn.cursor()
            identity = self._server_identity(cur)
            in_dbs = _in_list(databases)
            cur.execute("SELECT TABLE_SCHEMA, TABLE_NAME, TABLE_TYPE, ENGINE, CREATE_TIME, TABLE_COMMENT "
                        f"FROM information_schema.tables WHERE TABLE_SCHEMA IN ({in_dbs})")
            tables = cur.fetchall()
            cur.execute("SELECT TABLE_SCHEMA, TABLE_NAME, TABLE_MODEL, TABLE_MODEL_KEY "
                        f"FROM information_schema.table_options WHERE TABLE_SCHEMA IN ({in_dbs})")
            models = {(r["TABLE_SCHEMA"], r["TABLE_NAME"]): r for r in cur.fetchall()}
            ids = self._ids(cur, databases)
            columns = self._columns(cur, in_dbs, with_comments=True)
            views = self._views(cur, in_dbs)
        finally:
            conn.close()
        assets = []
        for t in tables:
            key = (t["TABLE_SCHEMA"], t["TABLE_NAME"])
            table_type = t["TABLE_TYPE"]
            definition = None
            if table_type == "VIEW":
                kind = "view"
                definition = views.get(key)
                if definition is None:
                    warnings.append(f"视图 {key[0]}.{key[1]} 未读到定义（需要 SHOW_VIEW_PRIV）")
            elif table_type == "BASE TABLE" and (t["ENGINE"] or "").lower() == "doris":
                kind = "table"
            else:
                kind = "table"
                warnings.append(f"{key[0]}.{key[1]} 的类型 {table_type} / {t['ENGINE']} 按普通表采集")
            model = models.get(key, {})
            table_model = model.get("TABLE_MODEL") or None
            model_key = [c.strip().strip("`") for c in (model.get("TABLE_MODEL_KEY") or "").split(",") if c.strip()]
            unique = model_key if table_model in UNIQUE_MODELS else []
            assets.append(ObservedAsset(
                catalog=INTERNAL_CATALOG, db=key[0], name=key[1], kind=kind,
                columns=[ObservedColumn(name=c[0], data_type=c[1], ordinal=c[2], nullable=c[3], comment=c[4],
                                        is_key=c[5]) for c in columns.get(key, [])],
                physical_id=ids.get(key), table_model=table_model, key_columns=unique,
                unique_keys=[unique] if unique else [], comment=t["TABLE_COMMENT"] or None, definition=definition,
                properties={"engine": t["ENGINE"], "model_key": model_key,
                            "create_time": str(t["CREATE_TIME"]) if t["CREATE_TIME"] else None},
            ))
        return Snapshot(server_identity=identity, assets=assets, warnings=warnings)

    @staticmethod
    def _ids(cur, databases: list[str]) -> dict[tuple[str, str], str]:
        cur.execute("SELECT DATABASE_NAME, TABLE_NAME, TABLE_ID FROM information_schema.metadata_name_ids "
                    f"WHERE CATALOG_NAME = 'internal' AND DATABASE_NAME IN ({_in_list(databases)})")
        return {(r["DATABASE_NAME"], r["TABLE_NAME"]): str(r["TABLE_ID"]) for r in cur.fetchall() if r["TABLE_ID"]}

    @staticmethod
    def _columns(cur, in_dbs: str, tables: str | None = None,
                 with_comments: bool = False) -> dict[tuple[str, str], list[tuple]]:
        where = f"TABLE_SCHEMA IN ({in_dbs})" + (f" AND TABLE_NAME IN ({tables})" if tables else "")
        cur.execute("SELECT TABLE_SCHEMA, TABLE_NAME, COLUMN_NAME, ORDINAL_POSITION, COLUMN_TYPE, IS_NULLABLE, "
                    f"COLUMN_COMMENT, COLUMN_KEY FROM information_schema.columns WHERE {where}")
        out: dict[tuple[str, str], list[tuple]] = {}
        for r in sorted(cur.fetchall(), key=lambda r: int(r["ORDINAL_POSITION"])):
            out.setdefault((r["TABLE_SCHEMA"], r["TABLE_NAME"]), []).append((
                r["COLUMN_NAME"], r["COLUMN_TYPE"], int(r["ORDINAL_POSITION"]), r["IS_NULLABLE"] == "YES",
                (r["COLUMN_COMMENT"] or None) if with_comments else None, r["COLUMN_KEY"] == "UNI"))
        return out

    @staticmethod
    def _views(cur, in_dbs: str, tables: str | None = None) -> dict[tuple[str, str], str]:
        where = f"TABLE_SCHEMA IN ({in_dbs})" + (f" AND TABLE_NAME IN ({tables})" if tables else "")
        cur.execute(f"SELECT TABLE_SCHEMA, TABLE_NAME, VIEW_DEFINITION FROM information_schema.views WHERE {where}")
        return {(r["TABLE_SCHEMA"], r["TABLE_NAME"]): r["VIEW_DEFINITION"] for r in cur.fetchall()
                if r["VIEW_DEFINITION"]}

    # --- 只读执行 ------------------------------------------------------------------

    def execute_select(self, sql: str, *, timeout_seconds: int, mem_limit_bytes: int, max_rows: int) -> QueryResult:
        conn = self._connect(read_timeout=timeout_seconds + 15)
        try:
            cur = conn.cursor(pymysql.cursors.Cursor)
            cur.execute(f"SET query_timeout = {int(timeout_seconds)}")
            if mem_limit_bytes:
                cur.execute(f"SET exec_mem_limit = {int(mem_limit_bytes)}")
            cur.execute(sql)
            columns = [d[0] for d in cur.description or []]
            rows = cur.fetchmany(max_rows + 1)
            return QueryResult(columns=columns, rows=[list(r) for r in rows[:max_rows]], truncated=len(rows) > max_rows)
        except pymysql.MySQLError as exc:
            raise _query_error(exc) from exc
        finally:
            conn.close()

    def describe_objects(self, objects: list[tuple[str, str]]) -> dict[tuple[str, str], dict[str, Any]]:
        """执行前核对：内部 Catalog 中给定库表当前的列（名称、类型，按位置）、视图定义与 TABLE_ID。"""
        names = {(db, name) for db, name in objects if IDENT.match(db) and IDENT.match(name)}
        out: dict[tuple[str, str], dict[str, Any]] = {k: {"columns": [], "definition": None, "physical_id": None}
                                                      for k in names}
        if not names:
            return out
        dbs = sorted({d for d, _ in names})
        in_dbs, in_tables = _in_list(dbs), _in_list(sorted({n for _, n in names}))
        conn = self._connect(read_timeout=30)
        try:
            cur = conn.cursor()
            for key, table_id in self._ids(cur, dbs).items():
                if key in out:
                    out[key]["physical_id"] = table_id
            for key, cols in self._columns(cur, in_dbs, in_tables).items():
                if key in out:
                    out[key]["columns"] = [[c[0], c[1]] for c in cols]
            for key, definition in self._views(cur, in_dbs, in_tables).items():
                if key in out:
                    out[key]["definition"] = definition
        except pymysql.MySQLError as exc:
            raise _query_error(exc) from exc
        finally:
            conn.close()
        return out

    def freshness(self, tables: list[tuple[str, str]]) -> dict[str, Any]:
        """内表最近写入时间（information_schema.tables.UPDATE_TIME，Doris 会话时区）；视图没有。"""
        names = [(d, n) for d, n in tables if IDENT.match(d) and IDENT.match(n)]
        if not names:
            return {}
        conn = self._connect()
        try:
            cur = conn.cursor()
            cur.execute("SELECT TABLE_SCHEMA, TABLE_NAME, TABLE_TYPE, UPDATE_TIME FROM information_schema.tables "
                        f"WHERE TABLE_SCHEMA IN ({_in_list(sorted({d for d, _ in names}))}) "
                        f"AND TABLE_NAME IN ({_in_list(sorted({n for _, n in names}))})")
            found = {f"{r['TABLE_SCHEMA']}.{r['TABLE_NAME']}": r for r in cur.fetchall()}
        except pymysql.MySQLError as exc:
            raise _query_error(exc) from exc
        finally:
            conn.close()
        out: dict[str, Any] = {}
        for d, n in names:
            r = found.get(f"{d}.{n}")
            out[f"{d}.{n}"] = str(r["UPDATE_TIME"]) if r and r["TABLE_TYPE"] == "BASE TABLE" and r["UPDATE_TIME"] else None
        return out


def _query_error(exc: pymysql.MySQLError) -> QueryError:
    errno = exc.args[0] if exc.args and isinstance(exc.args[0], int) else None
    message = str(exc.args[-1]) if exc.args else str(exc)
    text = message.lower()
    if "timeout" in text:
        kind = "timeout"
    elif "access denied" in text or "command denied" in text or errno in (1044, 1045, 1142):
        kind = "permission"
    elif "unknown column" in text or "does not exist" in text or "unknown table" in text or errno in (1054, 1146):
        kind = "invalid_object"
    else:
        kind = "other"
    return QueryError(kind, errno, message.replace("errCode = 2, detailMessage = ", "").strip()[:2000])
