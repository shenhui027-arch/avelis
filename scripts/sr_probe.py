"""StarRocks 3.5.14 实机核验：采集、身份、外部表、权限与资源限制。

每一项都在演示容器里真实执行，结果（成功、返回、报错原文）原样记录，
供 docs/starrocks-notes.md 引用。破坏性试验只在独立的 avelis_probe 库中进行。
口令不会写进结果：凡是出现 .env 里口令的地方都替换为 <redacted>。
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

import pymysql
import sqlglot
from sqlglot.optimizer.qualify import qualify

PROBE_DB = "avelis_probe"
DEMO_SCHEMAS = "('shop','ads','dim')"
SCHEMA = {
    "shop": {
        "orders": {"order_id": "BIGINT", "order_date": "DATE", "region": "VARCHAR", "customer_id": "BIGINT",
                   "status": "VARCHAR", "paid_amount": "DECIMAL(18,2)", "refund_amount": "DECIMAL(18,2)"},
    }
}
SEED_VIEW_SQL = (
    "SELECT order_date, region, SUM(paid_amount) AS sales_amount "
    "FROM shop.orders GROUP BY order_date, region"
)


def run_probes(env, sr_connect, mysql_connect) -> dict:
    secrets = [v for k, v in env.items() if k.endswith("_PASSWORD") and v]
    catalog_secret = env["SHOP_MYSQL_CATALOG_PASSWORD"]
    report = {"started_at": datetime.now(timezone.utc).isoformat(), "probes": {}}

    def redact(value):
        text = str(value)
        for secret in secrets:
            text = text.replace(secret, "<redacted>")
        return text

    def record(name, fn):
        started = time.monotonic()
        try:
            entry = {"ok": True, "result": fn()}
        except Exception as exc:  # noqa: BLE001 - every probe records its own failure
            entry = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        entry["seconds"] = round(time.monotonic() - started, 2)
        report["probes"][name] = _redact_tree(entry, redact)
        print(f"[{'ok ' if entry['ok'] else 'ERR'}] {name}", flush=True)

    root = sr_connect(env)

    def rows(sql, conn=root, limit=50):
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(sql)
            return cur.fetchall()[:limit]

    def scalar(sql, conn=root):
        with conn.cursor() as cur:
            cur.execute(sql)
            row = cur.fetchone()
            return row[0] if row else None

    def execute(sql, conn=root):
        with conn.cursor() as cur:
            cur.execute(sql)

    # 1. Version and cluster identity
    record("version", lambda: scalar("SELECT current_version()"))
    record("frontends", lambda: rows("SHOW FRONTENDS"))
    record("backends", lambda: [{k: r[k] for k in ("BackendId", "IP", "Alive", "Version") if k in r}
                                 for r in rows("SHOW BACKENDS")])
    record("proc_dbs", lambda: rows("SHOW PROC '/dbs'"))

    # 2. View definition formats and fingerprint stability
    def view_formats():
        show = rows("SHOW CREATE VIEW shop.daily_sales")[0]
        info = rows("SELECT VIEW_DEFINITION FROM information_schema.views "
                    "WHERE TABLE_SCHEMA = 'shop' AND TABLE_NAME = 'daily_sales'")
        definition = info[0]["VIEW_DEFINITION"] if info else None
        return {
            "show_create_view": show.get("Create View"),
            "information_schema_view_definition": definition,
            "fingerprint_seed": _canonical(SEED_VIEW_SQL),
            "fingerprint_information_schema": _canonical(definition) if definition else None,
            "fingerprint_matches_seed": bool(definition) and _canonical(definition) == _canonical(SEED_VIEW_SQL),
        }
    record("view_definition_formats", view_formats)

    # 3. Table metadata: ids, models, keys, comments, freshness
    record("tables_config", lambda: rows(
        "SELECT TABLE_SCHEMA, TABLE_NAME, TABLE_ENGINE, TABLE_MODEL, PRIMARY_KEY, DISTRIBUTE_KEY, TABLE_ID "
        f"FROM information_schema.tables_config WHERE TABLE_SCHEMA IN {DEMO_SCHEMAS} "
        "ORDER BY TABLE_SCHEMA, TABLE_NAME"))
    record("information_schema_tables", lambda: rows(
        "SELECT TABLE_SCHEMA, TABLE_NAME, TABLE_TYPE, ENGINE, CREATE_TIME, UPDATE_TIME, TABLE_COMMENT "
        f"FROM information_schema.tables WHERE TABLE_SCHEMA IN {DEMO_SCHEMAS} "
        "ORDER BY TABLE_SCHEMA, TABLE_NAME"))
    record("information_schema_columns_orders", lambda: rows(
        "SELECT COLUMN_NAME, ORDINAL_POSITION, COLUMN_TYPE, IS_NULLABLE, COLUMN_KEY, COLUMN_COMMENT "
        "FROM information_schema.columns WHERE TABLE_SCHEMA = 'shop' AND TABLE_NAME = 'orders' "
        "ORDER BY ORDINAL_POSITION"))
    record("partitions_meta", lambda: rows(
        "SELECT DB_NAME, TABLE_NAME, PARTITION_NAME, VISIBLE_VERSION, VISIBLE_VERSION_TIME, ROW_COUNT "
        "FROM information_schema.partitions_meta WHERE DB_NAME = 'shop'"))
    record("object_dependencies", lambda: rows("SELECT * FROM sys.object_dependencies LIMIT 20"))

    # 4. Identity under rename and drop-recreate (isolated probe database)
    def proc_tables(db_name):
        db_id = next(r["DbId"] for r in rows("SHOW PROC '/dbs'", limit=500) if r["DbName"] == db_name)
        return {r["TableName"]: r for r in rows(f"SHOW PROC '/dbs/{db_id}'", limit=500)}

    def table_ids(name):
        cfg = rows(f"SELECT TABLE_ID FROM information_schema.tables_config "
                   f"WHERE TABLE_SCHEMA = '{PROBE_DB}' AND TABLE_NAME = '{name}'")
        proc = proc_tables(PROBE_DB).get(name, {})
        info = rows(f"SELECT CREATE_TIME FROM information_schema.tables "
                    f"WHERE TABLE_SCHEMA = '{PROBE_DB}' AND TABLE_NAME = '{name}'")
        return {"tables_config_id": cfg[0]["TABLE_ID"] if cfg else None,
                "proc_table_id": proc.get("TableId"), "proc_type": proc.get("Type"),
                "create_time": info[0]["CREATE_TIME"] if info else None}

    def identity():
        execute(f"DROP DATABASE IF EXISTS {PROBE_DB} FORCE")
        execute(f"CREATE DATABASE {PROBE_DB}")
        ddl = (f"CREATE TABLE {PROBE_DB}.{{name}} (id BIGINT NOT NULL, v INT) PRIMARY KEY (id) "
               f"DISTRIBUTED BY HASH(id) BUCKETS 1 PROPERTIES ('replication_num' = '1')")
        execute(ddl.format(name="t_identity"))
        out = {"created": table_ids("t_identity")}
        execute(f"ALTER TABLE {PROBE_DB}.t_identity RENAME t_renamed")
        out["after_rename"] = table_ids("t_renamed")
        execute(f"DROP TABLE {PROBE_DB}.t_renamed FORCE")
        execute(ddl.format(name="t_renamed"))
        out["after_drop_recreate_same_name"] = table_ids("t_renamed")
        execute(f"CREATE VIEW {PROBE_DB}.v_identity AS SELECT id FROM {PROBE_DB}.t_renamed")
        out["view_created"] = table_ids("v_identity")
        execute(f"DROP VIEW {PROBE_DB}.v_identity")
        execute(f"CREATE VIEW {PROBE_DB}.v_identity AS SELECT id FROM {PROBE_DB}.t_renamed")
        out["view_after_drop_recreate"] = table_ids("v_identity")
        return out
    record("identity_rename_and_recreate", identity)

    # 5. Dropping a column that a view depends on
    def drop_column_under_view():
        execute(f"CREATE TABLE {PROBE_DB}.t_cols (k BIGINT NOT NULL, a INT, b INT) DUPLICATE KEY (k) "
                f"DISTRIBUTED BY HASH(k) BUCKETS 1 PROPERTIES ('replication_num' = '1')")
        execute(f"INSERT INTO {PROBE_DB}.t_cols VALUES (1, 10, 20)")
        execute(f"CREATE VIEW {PROBE_DB}.v_cols AS SELECT k, SUM(b) AS sb FROM {PROBE_DB}.t_cols GROUP BY k")
        before = rows(f"SELECT * FROM {PROBE_DB}.v_cols")
        execute(f"ALTER TABLE {PROBE_DB}.t_cols DROP COLUMN b")
        jobs = []
        for _ in range(30):
            jobs = rows(f"SHOW ALTER TABLE COLUMN FROM {PROBE_DB}")
            if all(j.get("State") in ("FINISHED", "CANCELLED") for j in jobs):
                break
            time.sleep(2)
        out = {"before": before, "alter_jobs": jobs}
        try:
            out["query_after"] = rows(f"SELECT * FROM {PROBE_DB}.v_cols")
        except pymysql.MySQLError as exc:
            out["query_after_error"] = str(exc)
        try:
            out["show_create_after"] = rows(f"SHOW CREATE VIEW {PROBE_DB}.v_cols")[0].get("Create View")
        except pymysql.MySQLError as exc:
            out["show_create_after_error"] = str(exc)
        out["columns_after"] = [r["COLUMN_NAME"] for r in rows(
            f"SELECT COLUMN_NAME FROM information_schema.columns "
            f"WHERE TABLE_SCHEMA = '{PROBE_DB}' AND TABLE_NAME = 't_cols' ORDER BY ORDINAL_POSITION")]
        return out
    record("drop_column_under_view", drop_column_under_view)

    # 6. External access: JDBC catalog and the legacy MySQL external table
    def catalog_meta():
        text = " ".join(str(v) for v in rows("SHOW CREATE CATALOG shop_mysql")[0].values())
        return {
            "show_catalogs": rows("SHOW CATALOGS"),
            "password_visible_in_show_create": catalog_secret in text,
            "show_create_catalog": text,
            "databases": rows("SHOW DATABASES FROM shop_mysql"),
            "tables": rows("SHOW TABLES FROM shop_mysql.shop_dim"),
            "desc_dim_product": rows("DESC shop_mysql.shop_dim.dim_product"),
        }
    record("catalog_metadata", catalog_meta)
    record("catalog_information_schema", lambda: rows(
        "SELECT TABLE_NAME, TABLE_TYPE FROM shop_mysql.information_schema.tables WHERE TABLE_SCHEMA = 'shop_dim'"))

    def legacy_external_table():
        text = " ".join(str(v) for v in rows("SHOW CREATE TABLE dim.ext_region")[0].values())
        return {
            "password_visible_in_show_create": catalog_secret in text,
            "show_create_table": text,
            "information_schema_tables": rows(
                "SELECT TABLE_NAME, TABLE_TYPE, ENGINE FROM information_schema.tables "
                "WHERE TABLE_SCHEMA = 'dim' AND TABLE_NAME = 'ext_region'"),
            "tables_config": rows(
                "SELECT TABLE_NAME, TABLE_ENGINE, TABLE_MODEL, TABLE_ID FROM information_schema.tables_config "
                "WHERE TABLE_SCHEMA = 'dim'"),
            "rows": rows("SELECT * FROM dim.ext_region ORDER BY region_code"),
        }
    record("mysql_external_table", legacy_external_table)

    record("view_over_catalog", lambda: {
        "definition": rows("SHOW CREATE VIEW ads.sales_by_category")[0].get("Create View"),
        "rows": rows("SELECT * FROM ads.sales_by_category ORDER BY order_date, category_name"),
    })
    record("view_over_external_table", lambda: {
        "definition": rows("SHOW CREATE VIEW ads.region_board")[0].get("Create View"),
        "rows": rows("SELECT * FROM ads.region_board ORDER BY region_code, order_date"),
    })

    # 7. Read-only account, timeouts and memory limits
    def reader_boundaries():
        reader = sr_connect(env, "avelis_reader")
        out = {"session_variables": rows(
            "SHOW VARIABLES WHERE Variable_name IN "
            "('query_timeout','query_mem_limit','exec_mem_limit','sql_select_limit','new_planner_optimize_timeout')",
            conn=reader)}
        for label, sql in [
            ("insert", "INSERT INTO shop.orders VALUES (9999,'2026-01-01','测试',1,'x',1,0)"),
            ("drop", "DROP TABLE shop.orders"),
            ("create_db", "CREATE DATABASE reader_should_not_create"),
            ("select_view", "SELECT COUNT(*) FROM shop.daily_sales"),
            ("select_view_over_catalog", "SELECT COUNT(*) FROM ads.sales_by_category"),
            ("select_catalog_table_direct", "SELECT COUNT(*) FROM shop_mysql.shop_dim.dim_product"),
            ("select_external_table", "SELECT COUNT(*) FROM dim.ext_region"),
        ]:
            try:
                out[label] = {"ok": True, "rows": rows(sql, conn=reader)}
            except pymysql.MySQLError as exc:
                out[label] = {"ok": False, "error": str(exc)}
        try:
            execute("SET query_timeout = 2", conn=reader)
            started = time.monotonic()
            try:
                rows("SELECT sleep(5)", conn=reader)
                out["timeout"] = {"timed_out": False, "seconds": round(time.monotonic() - started, 2)}
            except pymysql.MySQLError as exc:
                out["timeout"] = {"timed_out": True, "seconds": round(time.monotonic() - started, 2),
                                  "error": str(exc)}
        finally:
            reader.close()
        return out
    record("reader_boundaries", reader_boundaries)

    def collector_visibility():
        collector = sr_connect(env, "avelis_collector")
        out = {}
        for label, sql in [
            ("views", "SELECT TABLE_SCHEMA, TABLE_NAME, LENGTH(VIEW_DEFINITION) AS len FROM information_schema.views "
                      f"WHERE TABLE_SCHEMA IN {DEMO_SCHEMAS}"),
            ("tables_config", "SELECT TABLE_SCHEMA, TABLE_NAME, TABLE_ID FROM information_schema.tables_config "
                              f"WHERE TABLE_SCHEMA IN {DEMO_SCHEMAS}"),
            ("show_catalogs", "SHOW CATALOGS"),
            ("show_create_catalog", "SHOW CREATE CATALOG shop_mysql"),
            ("show_create_external_table", "SHOW CREATE TABLE dim.ext_region"),
            ("show_proc_dbs", "SHOW PROC '/dbs'"),
            ("show_frontends", "SHOW FRONTENDS"),
        ]:
            try:
                out[label] = {"ok": True, "rows": rows(sql, conn=collector)}
            except pymysql.MySQLError as exc:
                out[label] = {"ok": False, "error": str(exc)}
        collector.close()
        return out
    record("collector_visibility", collector_visibility)

    # 8. The MySQL side seen directly (for external-table identity and grain)
    def mysql_side():
        conn = mysql_connect(env, "avelis_collector")
        out = {
            "identity": rows("SELECT @@server_uuid AS server_uuid, @@hostname AS hostname, @@port AS port, "
                             "@@version AS version", conn=conn),
            "keys": rows("SELECT TABLE_NAME, INDEX_NAME, NON_UNIQUE, SEQ_IN_INDEX, COLUMN_NAME "
                         "FROM information_schema.statistics WHERE TABLE_SCHEMA = 'shop_dim' "
                         "ORDER BY TABLE_NAME, INDEX_NAME, SEQ_IN_INDEX", conn=conn),
            "columns": rows("SELECT TABLE_NAME, COLUMN_NAME, COLUMN_TYPE FROM information_schema.columns "
                            "WHERE TABLE_SCHEMA = 'shop_dim' ORDER BY TABLE_NAME, ORDINAL_POSITION",
                            conn=conn, limit=200),
        }
        conn.close()
        return out
    record("mysql_side", mysql_side)

    execute(f"DROP DATABASE IF EXISTS {PROBE_DB} FORCE")
    root.close()
    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    return report


def _canonical(sql: str) -> str:
    expression = qualify(sqlglot.parse_one(sql, read="starrocks"), schema=SCHEMA, dialect="starrocks")
    return expression.sql(dialect="starrocks", normalize=True)


def _redact_tree(value, redact):
    if isinstance(value, dict):
        return {k: _redact_tree(v, redact) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact_tree(v, redact) for v in value]
    if isinstance(value, str):
        return redact(value)
    return value
