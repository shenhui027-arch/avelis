"""SQL Server 2022 Developer 验收（真实实例，演示容器 shop-mssql）：通用引擎场景见 engine_suite.py。"""

from __future__ import annotations

import pytest

from tests.acceptance.engine_suite import EngineProfile, EngineSuite

pytestmark = [pytest.mark.acceptance, pytest.mark.engines]


class SqlServer(EngineProfile):
    kind = "sqlserver"
    label = "SQL Server"
    port_key = "SHOP_MSSQL_PORT"
    collect_keys = ("avelis_collector", "SHOP_MSSQL_COLLECTOR_PASSWORD")
    query_keys = ("avelis_reader", "SHOP_MSSQL_READER_PASSWORD")
    options = {"database": "storedb", "schemas": ["store"]}
    missing_option = ({"database": "storedb"}, "采集的模式")
    catalog = "storedb"
    sql_from = "FROM storedb.store.city_daily_sales AS b"  # 三段名：库作为 catalog 段
    grain_sql_name = "[storedb].[store].[returns]"
    sales_unique_key = True
    rename = ("EXEC sp_rename 'store.returns', 'refunds'",)
    recreate = ("DROP TABLE store.refunds",
                "CREATE TABLE store.refunds (return_id BIGINT NOT NULL PRIMARY KEY, sale_id BIGINT NOT NULL, "
                "return_date DATE NOT NULL, amount DECIMAL(18, 2) NOT NULL)")
    replace_view = ("CREATE OR ALTER VIEW store.city_daily_sales AS "
                    "SELECT s.sale_date, h.city, SUM(s.amount) AS sales_amount, COUNT(DISTINCT s.sale_id) AS sales_count "
                    "FROM store.sales AS s INNER JOIN store.shops AS h ON h.shop_id = s.shop_id "
                    "GROUP BY s.sale_date, h.city",)
    sleep_sql = "WAITFOR DELAY '00:00:05'; SELECT 1 AS x"
    write_sql = "INSERT INTO store.sales VALUES (9999, '2026-09-15', 1, N'paid', 1.00)"

    def running(self, env):
        import demo

        return demo.engines_running(env)["sqlserver"]

    def seed(self, env):
        import demo

        demo.seed_mssql(env)

    def ddl(self, env, *statements):
        import demo

        conn = demo.mssql_connect(env)
        try:
            cur = conn.cursor()
            for statement in statements:
                cur.execute(statement)
        finally:
            conn.close()

    def connector(self, env, purpose):
        from avelis.connectors.sqlserver import SqlServerConnector

        user, key = self.collect_keys if purpose == "collect" else self.query_keys
        return SqlServerConnector("127.0.0.1", int(env[self.port_key]), user, env[key], self.options)


class TestSqlServer(EngineSuite):
    profile = SqlServer()
