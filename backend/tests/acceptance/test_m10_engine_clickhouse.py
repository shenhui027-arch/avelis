"""ClickHouse 26.8 验收（真实实例，演示容器 shop-ch）：通用引擎场景见 engine_suite.py。"""

from __future__ import annotations

import pytest

from tests.acceptance.engine_suite import EngineProfile, EngineSuite

pytestmark = [pytest.mark.acceptance, pytest.mark.engines]


class ClickHouse(EngineProfile):
    kind = "clickhouse"
    label = "ClickHouse"
    port_key = "SHOP_CH_HTTP_PORT"
    collect_keys = ("avelis_collector", "SHOP_CH_COLLECTOR_PASSWORD")
    query_keys = ("avelis_reader", "SHOP_CH_READER_PASSWORD")
    options = {"databases": ["store"]}
    missing_option = ({}, "采集的库")
    catalog = "clickhouse"
    sql_from = "FROM store.city_daily_sales AS b"  # 两级名：不写 Catalog 段
    grain_sql_name = '"store"."returns"'
    sales_unique_key = False  # MergeTree 排序键不保证唯一
    rename = ("RENAME TABLE store.returns TO store.refunds",)
    recreate = ("DROP TABLE store.refunds SYNC",
                "CREATE TABLE store.refunds (return_id UInt64, sale_id UInt64, return_date Date, "
                "amount Decimal(18, 2)) ENGINE = MergeTree ORDER BY return_id")
    replace_view = ("CREATE OR REPLACE VIEW store.city_daily_sales AS "
                    "SELECT s.sale_date, h.city, SUM(s.amount) AS sales_amount, COUNT(DISTINCT s.sale_id) AS sales_count "
                    "FROM store.sales AS s INNER JOIN store.shops AS h ON h.shop_id = s.shop_id "
                    "GROUP BY s.sale_date, h.city",)
    sleep_sql = "SELECT sleep(3) FROM numbers(3) SETTINGS max_block_size = 1"
    write_sql = "INSERT INTO store.sales VALUES (9999, '2026-09-15', 1, 'paid', 1.00)"

    def running(self, env):
        import demo

        return demo.engines_running(env)["clickhouse"]

    def seed(self, env):
        import demo

        demo.seed_clickhouse(env)

    def ddl(self, env, *statements):
        import demo

        client = demo.ch_connect(env)
        try:
            for statement in statements:
                client.command(statement)
        finally:
            client.close()

    def connector(self, env, purpose):
        from avelis.connectors.clickhouse import ClickHouseConnector

        user, key = self.collect_keys if purpose == "collect" else self.query_keys
        return ClickHouseConnector("127.0.0.1", int(env[self.port_key]), user, env[key], self.options)


class TestClickHouse(EngineSuite):
    profile = ClickHouse()
