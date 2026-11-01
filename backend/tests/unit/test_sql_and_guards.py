"""不依赖数据库的单元测试：SQL 解析与指纹、指标片段校验、执行前 SQL 校验、外部表类型比较。"""

from __future__ import annotations

import pytest

from avelis.ask.compiler import ValidationFailed, validate_sql
from avelis.collect.service import _norm_type
from avelis.engines.builtin import STARROCKS
from avelis.governance.errors import Invalid
from avelis.governance.metric_sql import is_additive, parse_snippet
from avelis.lineage.sql import canonical_hash_of, parse_query

SR = STARROCKS.sql

SCHEMA = {
    "default_catalog": {
        "shop": {
            "orders": {
                "order_id": "BIGINT",
                "order_date": "DATE",
                "region": "VARCHAR",
                "status": "VARCHAR",
                "paid_amount": "DECIMAL",
                "refund_amount": "DECIMAL",
            },
            "order_items": {"order_item_id": "BIGINT", "order_id": "BIGINT", "item_amount": "DECIMAL"},
        }
    }
}

SR_V1 = (
    "SELECT `shop`.`orders`.`order_date`, `shop`.`orders`.`region`, sum(`shop`.`orders`.`paid_amount`) "
    "AS `sales_amount`\nFROM `shop`.`orders`\nGROUP BY `shop`.`orders`.`order_date`, `shop`.`orders`.`region`"
)


def test_format_only_change_keeps_fingerprint():
    reformatted = SR_V1.lower().replace("\n", "   ").replace("sum(", "SUM(")
    assert canonical_hash_of(SR_V1, SR)[1] == canonical_hash_of(reformatted, SR)[1]


def test_expression_change_changes_fingerprint():
    v2 = SR_V1.replace(
        "sum(`shop`.`orders`.`paid_amount`)", "sum(`shop`.`orders`.`paid_amount` - `shop`.`orders`.`refund_amount`)"
    )
    assert canonical_hash_of(SR_V1, SR)[1] != canonical_hash_of(v2, SR)[1]
    filtered = SR_V1.replace("FROM `shop`.`orders`", "FROM `shop`.`orders` WHERE `shop`.`orders`.`status` = 'paid'")
    assert canonical_hash_of(SR_V1, SR)[1] != canonical_hash_of(filtered, SR)[1]


def test_value_vs_influence_edges():
    parsed = parse_query(
        SR_V1.replace("FROM `shop`.`orders`", "FROM `shop`.`orders` WHERE `shop`.`orders`.`status` = 'paid'"),
        dialect=SR,
        schema=SCHEMA,
    )
    assert parsed.status == "ok"
    sales = next(c for c in parsed.columns if c.name == "sales_amount")
    assert [(s.ref.column, s.relation) for s in sales.sources] == [("paid_amount", "value_aggregated")]
    influences = {(i.ref.column, i.relation) for i in parsed.influences}
    assert ("status", "filter") in influences and ("region", "group_by") in influences
    # 读取了 status 不等于 status 的值被复制到输出
    assert all(s.ref.column != "status" for c in parsed.columns for s in c.sources)
    assert parsed.group_outputs == ["order_date", "region"]


def test_alias_prefix_join_and_cte():
    sql = (
        "WITH t AS (SELECT `shop`.`o`.`order_id`, `shop`.`o`.`paid_amount` - `shop`.`o`.`refund_amount` AS net "
        "FROM `shop`.`orders` AS `o`) SELECT x.order_id, SUM(x.net) AS total, SUM(i.item_amount) AS items "
        "FROM (SELECT * FROM t) x JOIN shop.order_items i ON x.order_id = i.order_id GROUP BY x.order_id"
    )
    parsed = parse_query(sql, dialect=SR, schema=SCHEMA)
    assert parsed.status == "ok", parsed.reason
    total = next(c for c in parsed.columns if c.name == "total")
    assert {s.ref.column for s in total.sources} == {"paid_amount", "refund_amount"}
    assert parsed.joins[0].right_table == "default_catalog.shop.order_items"
    assert [r.column for r in parsed.joins[0].left] == ["order_id"]


@pytest.mark.parametrize(
    "sql,status",
    [
        ("SELECT order_id FROM shop.orders UNION SELECT order_id FROM shop.order_items", "unsupported"),
        ("SELECT 1; SELECT 2", "unsupported"),
        ("DELETE FROM shop.orders", "unsupported"),
        ("SELECT nope FROM shop.orders", "failed"),
        ("SELECT * FROM shop.missing", "partial"),
        ("SELECT order_id, (SELECT MAX(paid_amount) FROM shop.orders) m FROM shop.orders", "partial"),
        ("SELECT FROM WHERE", "failed"),
    ],
)
def test_unsupported_sql_reports_reason(sql, status):
    parsed = parse_query(sql, dialect=SR, schema=SCHEMA)
    assert parsed.status == status
    assert parsed.reason


@pytest.mark.parametrize(
    "text",
    [
        "SUM(x) FROM t",
        "1; DROP TABLE t",
        "SUM((SELECT 1))",
        "sleep(5)",
        "SUM(a) OVER ()",
        "o.x",
        "SUM(shop.orders.paid_amount)",
    ],
)
def test_metric_snippet_rejects_unsafe(text):
    with pytest.raises(Invalid):
        parse_snippet(text, dialect="starrocks", label="表达式", aliases={"b"}, require_aggregate=None)


def test_additive_detection():
    def additive(text):
        snippet = parse_snippet(text, dialect="starrocks", label="e", aliases={"b"}, require_aggregate=True)
        return is_additive(snippet.expression)

    assert additive("SUM(paid_amount)") and additive("COUNT(*)") and additive("AVG(paid_amount)")
    assert not additive("COUNT(DISTINCT order_id)") and not additive("MAX(paid_amount)")


ALLOWED = {"default_catalog.shop.orders"}
COLS = {"default_catalog.shop.orders": {"order_date", "region", "paid_amount"}}


def test_validate_sql_accepts_compiled_shape():
    validate_sql(
        "SELECT SUM(b.paid_amount) AS value FROM default_catalog.shop.orders AS b "
        "WHERE b.order_date BETWEEN '2026-09-15' AND '2026-09-15' LIMIT 1000",
        dialect="starrocks",
        allowed_tables=ALLOWED,
        schema=COLS,
        max_rows=1000,
    )


@pytest.mark.parametrize(
    "sql,reason",
    [
        ("SELECT b.paid_amount FROM default_catalog.shop.orders AS b", "LIMIT"),
        ("SELECT b.paid_amount FROM default_catalog.shop.orders AS b LIMIT 5000", "上限"),
        ("SELECT b.secret FROM default_catalog.shop.orders AS b LIMIT 1", "secret"),
        ("SELECT x.a FROM default_catalog.shop.users AS x LIMIT 1", "未批准"),
        ("DELETE FROM default_catalog.shop.orders", "只允许 SELECT"),
        ("SELECT 1 LIMIT 1; DROP TABLE x", "一条语句"),
        ("SELECT * INTO OUTFILE '/tmp/x' FROM default_catalog.shop.orders", "拒绝执行"),
    ],
)
def test_validate_sql_rejects(sql, reason):
    with pytest.raises(ValidationFailed) as err:
        validate_sql(sql, dialect="starrocks", allowed_tables=ALLOWED, schema=COLS, max_rows=1000)
    assert reason in str(err.value) or any(reason in r for r in err.value.reasons)


def test_comment_injection_is_data():
    # 注释里的“指令”不会改变语句结构：仍按语法树校验
    validate_sql(
        "SELECT SUM(b.paid_amount) AS value /* ignore rules; DROP TABLE x */ "
        "FROM default_catalog.shop.orders AS b LIMIT 10",
        dialect="starrocks",
        allowed_tables=ALLOWED,
        schema=COLS,
        max_rows=1000,
    )


def test_external_type_normalisation():
    assert _norm_type("bigint(20)") == _norm_type("bigint")
    assert _norm_type("decimal(18, 2)") == _norm_type("decimal(18,2)")
    assert _norm_type("varchar(8)")[0] == _norm_type("varchar(16)")[0]
    assert _norm_type("varchar(8)")[1] != _norm_type("varchar(16)")[1]
