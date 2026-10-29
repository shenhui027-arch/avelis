"""ClickHouse 连接器（真实数据库，HTTP 接口）。

- 资产名两级：库.表。Avelis 统一存为三段，Catalog 段用固定值 clickhouse；生成的 SQL 不写 Catalog 段。
- 采集只读 system.tables / system.columns（需要 SHOW TABLES、SHOW COLUMNS 权限，不需要读业务数据）。
- 视图定义取 system.tables.as_select；表的物理身份用 system.tables.uuid（Atomic 库：改名不变，删后重建会变）。
  视图不用 UUID：普通视图不支持 MODIFY QUERY，改定义只能 CREATE OR REPLACE（换新 UUID），视图按名称对应、
  改名按定义识别（与 StarRocks 视图 TABLE_ID 为 0 时的处理一致）。
- 排序键 / 主键不保证唯一（MergeTree 只按它排序，不去重），不作为粒度依据；ReplacingMergeTree 不加 FINAL 也会有重复。
- 列类型的 Nullable(...) 外壳拆为可空标记，类型名保留 ClickHouse 原样。
- 问数执行：查询账号为 readonly=2（只读、可调整查询设置），每次查询设 max_execution_time 与 max_memory_usage；
  错误码 159 归为超时，60 / 47 / 81 归为对象无法解析，497 / 164 归为权限。
- 数据新鲜度：MergeTree 表活动数据分片的最近修改时间（system.parts.modification_time，服务器时区）；
  需要查询账号可读 system.parts 的 database、table、active、modification_time 四列，未授权时不报告新鲜度。
- DDL 审计（ADR 0008）：system.query_log 中 type = QueryFinish、query_kind ∈ {Create, Alter, Drop, Rename} 的记录，
  对象取 tables 列。采集账号需该表 event_time、user、query_kind、type、tables、query 列的 SELECT；
  演示库再用行策略把它限制为只见 DDL 行。日志默认每 7.5 秒落盘一次。
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

import clickhouse_connect
from clickhouse_connect.driver.exceptions import ClickHouseError

from avelis.connectors.base import ObservedAsset, ObservedColumn, QueryError, QueryResult, Snapshot
from avelis.connectors.ddl import AuditUnavailable, DdlEvent

IDENT = re.compile(r"^[A-Za-z0-9_]+$")
CODE = re.compile(r"Code:\s*(\d+)")
CATALOG = "clickhouse"
NULLABLE = re.compile(r"^Nullable\((.*)\)$")


def _checked(names: list[str], label: str) -> list[str]:
    for name in names:
        if not IDENT.match(name):
            raise ValueError(f"{label} 名称不合法：{name!r}")
    return names


def _kind(engine: str) -> str:
    if engine == "View":
        return "view"
    if engine == "MaterializedView":
        return "materialized_view"
    return "table"


def _identity(uuid: str | None, kind: str) -> str | None:
    if kind != "table" or not uuid or uuid == "00000000-0000-0000-0000-000000000000":
        return None
    return uuid


def _type(raw: str) -> tuple[str, bool]:
    m = NULLABLE.match(raw)
    return (m.group(1), True) if m else (raw, False)


class ClickHouseConnector:
    kind = "clickhouse"
    dialect = "clickhouse"
    is_test_adapter = False
    ddl_audit_source = "clickhouse:system.query_log"
    ddl_identifier_fold = "exact"  # 库、表、列名区分大小写
    ddl_effect_lag_seconds = 0  # 元数据变更随语句完成生效

    def __init__(self, host: str, port: int, user: str, password: str, options: dict[str, Any] | None = None):
        self.options = options or {}
        self._conn_args = dict(host=host, port=port, username=user, password=password)

    def _client(self, timeout: int = 60):
        return clickhouse_connect.get_client(**self._conn_args, connect_timeout=5, send_receive_timeout=timeout,
                                             autogenerate_session_id=False)

    # --- 采集 ---------------------------------------------------------------------

    def server_identity(self) -> dict[str, Any]:
        client = self._client()
        try:
            return self._server_identity(client)
        finally:
            client.close()

    def _server_identity(self, client) -> dict[str, Any]:
        version, server_uuid = client.query("SELECT version(), toString(serverUUID())").result_rows[0]
        identity = {"version": version, "endpoint": f"{self._conn_args['host']}:{self._conn_args['port']}",
                    "server_uuid": server_uuid}
        if server_uuid and server_uuid != "00000000-0000-0000-0000-000000000000":
            identity["instance_id"] = f"ch:{server_uuid}"
        return identity

    def snapshot(self) -> Snapshot:
        databases = _checked(list(self.options.get("databases", [])), "库")
        if not databases:
            raise ValueError("数据源未配置要采集的库（options.databases）")
        warnings: list[str] = []
        client = self._client()
        try:
            identity = self._server_identity(client)
            tables = client.query(
                "SELECT database, name, engine, toString(uuid), comment, as_select, sorting_key, primary_key "
                "FROM system.tables WHERE database IN {dbs:Array(String)} AND NOT is_temporary "
                "ORDER BY database, name", parameters={"dbs": databases}).result_rows
            columns = self._columns(client, databases, with_comments=True)
        finally:
            client.close()
        assets = []
        for db, name, engine, uuid, comment, as_select, sorting_key, primary_key in tables:
            kind = _kind(engine)
            definition = (as_select or "").strip() or None
            if kind in ("view", "materialized_view") and definition is None:
                warnings.append(f"视图 {db}.{name} 未读到定义")
            assets.append(ObservedAsset(
                catalog=CATALOG, db=db, name=name, kind=kind,
                columns=[ObservedColumn(name=c[0], data_type=c[1], ordinal=c[2], nullable=c[3], comment=c[4])
                         for c in columns.get((db, name), [])],
                physical_id=_identity(uuid, kind),
                table_model=engine, key_columns=[], unique_keys=[], comment=comment or None,
                definition=definition if kind != "table" else None,
                properties={"engine": engine, "sorting_key": sorting_key, "primary_key": primary_key},
            ))
        return Snapshot(server_identity=identity, assets=assets, warnings=warnings)

    @staticmethod
    def _columns(client, databases: list[str], tables: list[str] | None = None,
                 with_comments: bool = False) -> dict[tuple[str, str], list[tuple]]:
        sql = ("SELECT database, table, name, type, position, comment FROM system.columns "
               "WHERE database IN {dbs:Array(String)}")
        params: dict[str, Any] = {"dbs": databases}
        if tables is not None:
            sql += " AND table IN {tables:Array(String)}"
            params["tables"] = tables
        out: dict[tuple[str, str], list[tuple]] = {}
        for db, table, name, raw_type, position, comment in client.query(
                sql + " ORDER BY database, table, position", parameters=params).result_rows:
            data_type, nullable = _type(raw_type)
            out.setdefault((db, table), []).append(
                (name, data_type, int(position), nullable, (comment or None) if with_comments else None))
        return out

    def ddl_events(self, since: datetime, until: datetime, cursors=None) -> list[DdlEvent]:
        databases = set(self.options.get("databases", []))
        client = self._client()
        try:
            rows = client.query(
                "SELECT toUnixTimestamp(event_time), user, query_kind, tables, query FROM system.query_log "
                "WHERE type = 'QueryFinish' AND query_kind IN ('Create', 'Alter', 'Drop', 'Rename') "
                "AND event_time >= toDateTime({since:Int64}) AND event_time <= toDateTime({until:Int64}) "
                "ORDER BY event_time", parameters={"since": _epoch(since), "until": _epoch(until)}).result_rows
        except ClickHouseError as exc:
            raise AuditUnavailable(_first_line(str(exc)), _audit_cause(exc)) from exc
        finally:
            client.close()
        events = []
        for ts, user, kind, tables, query in rows:
            objects = [tuple(t.split(".", 1)) for t in tables if "." in t]
            objects = [o for o in objects if not databases or o[0] in databases]
            if objects:
                events.append(DdlEvent(at=_from_epoch(ts), db_user=user, objects=objects,
                                       statement_type=str(kind).upper(), statement=query))
        return events

    # --- 只读执行 ------------------------------------------------------------------

    def execute_select(self, sql_text: str, *, timeout_seconds: int, mem_limit_bytes: int,
                       max_rows: int) -> QueryResult:
        settings: dict[str, Any] = {"max_execution_time": int(timeout_seconds)}
        if mem_limit_bytes:
            settings["max_memory_usage"] = int(mem_limit_bytes)
        client = self._client(timeout=int(timeout_seconds) + 10)
        try:
            result = client.query(sql_text, settings=settings)
            rows = [list(r) for r in result.result_rows[:max_rows + 1]]
            columns = list(result.column_names)
        except ClickHouseError as exc:
            raise _query_error(exc) from exc
        finally:
            client.close()
        return QueryResult(columns=columns, rows=rows[:max_rows], truncated=len(rows) > max_rows)

    def freshness(self, tables: list[tuple[str, str]]) -> dict[str, Any]:
        names = [(d, n) for d, n in tables if IDENT.match(d) and IDENT.match(n)]
        if not names:
            return {}
        client = self._client()
        try:
            rows = client.query(
                "SELECT database, table, toString(max(modification_time)) FROM system.parts "
                "WHERE active AND (database, table) IN {names:Array(Tuple(String, String))} "
                "GROUP BY database, table", parameters={"names": names}).result_rows
        except ClickHouseError as exc:
            error = _query_error(exc)
            if error.code == "permission":  # 未授予 system.parts 的读取权限：不报告新鲜度，而不是让问数失败
                return {}
            raise error from exc
        finally:
            client.close()
        found = {f"{d}.{t}": ts for d, t, ts in rows}
        return {f"{d}.{n}": found.get(f"{d}.{n}") for d, n in names}

    def describe_objects(self, objects: list[tuple[str, str]]) -> dict[tuple[str, str], dict[str, Any]]:
        """执行前核对：给定 库.表 当前的列（名称、类型，按位置）、视图定义与 UUID；对象不存在时列为空。"""
        names = [(d, n) for d, n in objects if IDENT.match(d) and IDENT.match(n)]
        out: dict[tuple[str, str], dict[str, Any]] = {k: {"columns": [], "definition": None, "physical_id": None}
                                                      for k in names}
        if not names:
            return out
        dbs, tables = sorted({d for d, _ in names}), sorted({n for _, n in names})
        client = self._client(timeout=30)
        try:
            for db, name, engine, uuid, as_select in client.query(
                    "SELECT database, name, engine, toString(uuid), as_select FROM system.tables "
                    "WHERE database IN {dbs:Array(String)} AND name IN {tables:Array(String)}",
                    parameters={"dbs": dbs, "tables": tables}).result_rows:
                if (db, name) in out:
                    out[(db, name)]["physical_id"] = _identity(uuid, _kind(engine))
                    if _kind(engine) != "table":
                        out[(db, name)]["definition"] = (as_select or "").strip() or None
            for key, cols in self._columns(client, dbs, tables).items():
                if key in out:
                    out[key]["columns"] = [[c[0], c[1]] for c in cols]
        except ClickHouseError as exc:
            raise _query_error(exc) from exc
        finally:
            client.close()
        return out


def _audit_cause(exc: ClickHouseError) -> str:
    m = CODE.search(str(exc))
    code = int(m.group(1)) if m else None
    if code in (497, 164, 516):
        return "no_permission"
    if code in (60, 81):  # 没有 query_log 表（未开启查询日志）或库
        return "not_enabled"
    if code in (47, 16):  # 缺列
        return "incompatible"
    return "error"


def _epoch(at: datetime) -> int:
    return int(at.replace(tzinfo=timezone.utc).timestamp())


def _from_epoch(ts: float) -> datetime:
    return datetime.fromtimestamp(float(ts), tz=timezone.utc).replace(tzinfo=None)


def _query_error(exc: ClickHouseError) -> QueryError:
    message = str(exc)
    m = CODE.search(message)
    code = int(m.group(1)) if m else None
    if code == 159 or (code is None and "timed out" in message.lower()):
        kind = "timeout"
    elif code in (497, 164, 516):
        kind = "permission"
    elif code in (60, 47, 81, 16):
        kind = "invalid_object"
    else:
        kind = "other"
    return QueryError(kind, code, _first_line(message))


def _first_line(message: str) -> str:
    # 去掉驱动附加的请求信息，只保留数据库原始报错
    text = message.strip()
    m = re.search(r"Code:\s*\d+\..*", text, re.S)
    return (m.group(0) if m else text).strip()[:2000]
