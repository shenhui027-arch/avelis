"""Apache Doris 4.1 验收（真实实例，演示容器 shop-doris）：通用引擎场景见 engine_suite.py。"""

from __future__ import annotations

import pytest

from tests.acceptance.engine_suite import EngineProfile, EngineSuite

pytestmark = [pytest.mark.acceptance, pytest.mark.engines]


class Doris(EngineProfile):
    kind = "doris"
    label = "Doris"
    port_key = "SHOP_DORIS_QUERY_PORT"
    collect_keys = ("avelis_collector", "SHOP_DORIS_COLLECTOR_PASSWORD")
    query_keys = ("avelis_reader", "SHOP_DORIS_READER_PASSWORD")
    options = {"databases": ["store"]}
    missing_option = ({}, "采集的库")
    catalog = "internal"
    sql_from = "FROM internal.store.city_daily_sales AS b"
    grain_sql_name = "`internal`.`store`.`returns`"
    sales_unique_key = True  # UNIQUE KEY 模型
    rename = ("ALTER TABLE store.returns RENAME refunds",)
    recreate = (
        "DROP TABLE store.refunds FORCE",
        "CREATE TABLE store.refunds (return_id BIGINT NOT NULL, sale_id BIGINT NOT NULL, "
        "return_date DATE NOT NULL, amount DECIMAL(18, 2) NOT NULL) UNIQUE KEY (return_id) "
        'DISTRIBUTED BY HASH(return_id) BUCKETS 1 PROPERTIES ("replication_num" = "1")',
    )
    replace_view = (
        "ALTER VIEW store.city_daily_sales AS "
        "SELECT s.sale_date, h.city, SUM(s.amount) AS sales_amount, COUNT(DISTINCT s.sale_id) AS sales_count "
        "FROM store.sales s INNER JOIN store.shops h ON h.shop_id = s.shop_id "
        "GROUP BY s.sale_date, h.city",
    )
    sleep_sql = "SELECT sleep(5)"
    write_sql = "INSERT INTO store.sales VALUES (9999, '2026-09-15', 1, 'paid', 1.00)"

    def running(self, env):
        import demo

        return demo.engines_running(env)["doris"]

    def seed(self, env):
        import demo

        demo.seed_doris(env)

    def ddl(self, env, *statements):
        import demo

        conn = demo.doris_connect(env)
        try:
            with conn.cursor() as cur:
                for statement in statements:
                    cur.execute(statement)
        finally:
            conn.close()

    def connector(self, env, purpose):
        from avelis.connectors.doris import DorisConnector

        user, key = self.collect_keys if purpose == "collect" else self.query_keys
        return DorisConnector("127.0.0.1", int(env[self.port_key]), user, env[key], self.options)


class TestDoris(EngineSuite):
    profile = Doris()
