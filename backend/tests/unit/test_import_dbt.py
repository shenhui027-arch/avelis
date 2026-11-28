"""dbt manifest.json 转 SQL：模型类型与物化、编译 SQL 字段兼容、目标名回退、跳过与提示、无效输入。"""

import json

import pytest

from avelis.governance.errors import Invalid
from avelis.importer import dbt


def node(unique_id, *, resource_type="model", materialized="table", code="select 1 as a", key="compiled_code",
         relation_name=None, database="analytics", schema="public", alias=None, name=None, path=None):
    item = {
        "unique_id": unique_id,
        "resource_type": resource_type,
        "config": {"materialized": materialized},
        "relation_name": relation_name,
        "database": database,
        "schema": schema,
        "alias": alias,
        "name": name or unique_id.split(".")[-1],
        "original_file_path": path or f"models/{unique_id.split('.')[-1]}.sql",
    }
    if code is not None:
        item[key] = code
    return item


def manifest(*nodes, adapter="postgres", version="https://schemas.getdbt.com/dbt/manifest/v12.json"):
    return json.dumps({
        "metadata": {"dbt_schema_version": version, "adapter_type": adapter},
        "nodes": {n["unique_id"]: n for n in nodes},
        "sources": {},
    })


def test_is_manifest_name():
    assert dbt.is_manifest_name("manifest.json")
    assert dbt.is_manifest_name("target/Manifest.JSON")
    assert dbt.is_manifest_name("proj\\target\\manifest.json")
    assert not dbt.is_manifest_name("catalog.json")
    assert not dbt.is_manifest_name("manifest.json.bak")


def test_models_become_create_statements_in_unique_id_order():
    text = manifest(
        node("model.shop.orders_daily", relation_name='"analytics"."mart"."orders_daily"',
             code="select order_date, sum(amount) as amount\nfrom \"analytics\".\"stg\".\"orders\"\ngroup by 1;  "),
        node("model.shop.customers", materialized="view", relation_name='"analytics"."mart"."customers"',
             code="select id, name from raw.customers", key="compiled_sql"),
        node("snapshot.shop.orders_snap", resource_type="snapshot", materialized="snapshot",
             relation_name='"analytics"."snap"."orders_snap"', code="select * from raw.orders"),
    )
    out = dbt.manifest_to_sql(text)
    assert out.models == 3
    assert out.sql == (
        '-- dbt model model.shop.customers (models/customers.sql)\n'
        'CREATE VIEW "analytics"."mart"."customers" AS\n'
        'select id, name from raw.customers\n'
        ';\n'
        '\n'
        '-- dbt model model.shop.orders_daily (models/orders_daily.sql)\n'
        'CREATE TABLE "analytics"."mart"."orders_daily" AS\n'
        'select order_date, sum(amount) as amount\nfrom "analytics"."stg"."orders"\ngroup by 1\n'
        ';\n'
        '\n'
        '-- dbt snapshot snapshot.shop.orders_snap (models/orders_snap.sql)\n'
        'CREATE TABLE "analytics"."snap"."orders_snap" AS\n'
        'select * from raw.orders\n'
        ';\n'
    )
    assert out.notes == ["已从 dbt manifest 转换 3 个模型（适配器：postgres）"]


def test_relation_name_falls_back_to_database_schema_alias():
    out = dbt.manifest_to_sql(manifest(
        node("model.p.a", relation_name=None, database="dw", schema="mart", alias="a_alias"),
        node("model.p.b", relation_name="", database=None, schema="ods", alias=None, name="b"),
    ))
    assert "CREATE TABLE dw.mart.a_alias AS" in out.sql
    assert "CREATE TABLE ods.b AS" in out.sql


def test_skips_ephemeral_uncompiled_and_non_model_nodes_with_notes():
    out = dbt.manifest_to_sql(manifest(
        node("model.p.kept", relation_name="dw.kept"),
        node("model.p.inline", materialized="ephemeral", relation_name=None),
        node("model.p.raw", code=None, relation_name="dw.raw"),
        node("seed.p.countries", resource_type="seed", materialized="seed", code=None, relation_name="dw.countries"),
        node("test.p.not_null", resource_type="test", materialized="test", relation_name=None),
        adapter=None,
    ))
    assert out.models == 1
    assert "dw.kept" in out.sql and "inline" not in out.sql and "countries" not in out.sql
    assert out.notes == [
        "已从 dbt manifest 转换 1 个模型（适配器：未知）",
        "已跳过 1 个 ephemeral 模型（已内联到下游模型）",
        "已跳过 1 个没有编译 SQL 的模型",
    ]


@pytest.mark.parametrize(
    "text",
    ["not json", json.dumps({"nodes": {}}), json.dumps({"metadata": {"dbt_schema_version": "https://x/catalog/v1.json"}}),
     json.dumps([1, 2])],
)
def test_rejects_non_manifest(text):
    with pytest.raises(Invalid, match="不是有效的 dbt manifest.json"):
        dbt.manifest_to_sql(text)


def test_rejects_manifest_without_compiled_sql():
    with pytest.raises(Invalid, match="请先运行 dbt compile"):
        dbt.manifest_to_sql(manifest(node("model.p.a", code=None, relation_name="dw.a")))
