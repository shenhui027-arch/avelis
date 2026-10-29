"""Oracle Database 23ai Free 验收（真实实例，演示容器 shop-oracle，服务名 FREEPDB1）：通用引擎场景见 engine_suite.py。

Oracle 特有：标识符按大写保存；DATE 带时间部分（北京那笔 2026-09-15 10:30 下单，按日期过滤必须用区间才查得到）；
查询中不写服务名；采集账号读业务数据报 ORA-00942（对无权限的对象 Oracle 报“不存在”）。
"""

from __future__ import annotations

import pytest

from tests.acceptance.engine_suite import EngineProfile, EngineSuite

pytestmark = [pytest.mark.acceptance, pytest.mark.engines]


class Oracle(EngineProfile):
    kind = "oracle"
    label = "Oracle"
    port_key = "SHOP_ORACLE_PORT"
    collect_keys = ("avelis_collector", "SHOP_ORACLE_COLLECTOR_PASSWORD")
    query_keys = ("avelis_reader", "SHOP_ORACLE_READER_PASSWORD")
    options = {"service": "FREEPDB1", "schemas": ["STORE"]}
    missing_option = ({"service": "FREEPDB1"}, "采集的模式")
    catalog = "FREEPDB1"
    sql_from = "FROM STORE.CITY_DAILY_SALES b"  # 不写服务名；Oracle 的表别名不带 AS
    grain_sql_name = '"STORE"."RETURNS"'
    sales_unique_key = True
    rename = ("ALTER TABLE store.returns RENAME TO refunds",)
    recreate = ("DROP TABLE store.refunds PURGE",
                "CREATE TABLE store.refunds (return_id NUMBER(19) PRIMARY KEY, sale_id NUMBER(19) NOT NULL, "
                "return_date DATE NOT NULL, amount NUMBER(18, 2) NOT NULL)")
    replace_view = ("CREATE OR REPLACE VIEW store.city_daily_sales AS "
                    "SELECT s.sale_date, h.city, SUM(s.amount) AS sales_amount, COUNT(DISTINCT s.sale_id) AS sales_count "
                    "FROM store.sales s INNER JOIN store.shops h ON h.shop_id = s.shop_id "
                    "GROUP BY s.sale_date, h.city",)
    sleep_sql = "SELECT COUNT(*) FROM all_objects a, all_objects b, all_objects c"
    write_sql = "INSERT INTO store.sales VALUES (9999, DATE '2026-09-15', 1, 'paid', 1.00)"
    collector_denied = "invalid_object"

    def ident(self, name):
        return name.upper()

    def running(self, env):
        import demo

        return demo.engines_running(env)["oracle"]

    def seed(self, env):
        import demo

        demo.seed_oracle(env)

    def ddl(self, env, *statements):
        import demo

        with demo.oracle_connect(env) as conn, conn.cursor() as cur:
            for statement in statements:
                cur.execute(statement)

    def connector(self, env, purpose):
        from avelis.connectors.oracle import OracleConnector

        user, key = self.collect_keys if purpose == "collect" else self.query_keys
        return OracleConnector("127.0.0.1", int(env[self.port_key]), user, env[key], self.options)


class TestOracle(EngineSuite):
    profile = Oracle()
