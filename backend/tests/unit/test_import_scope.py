"""未匹配读取来源与采集范围：库名解析、范围判断、加入范围（只改范围键、Oracle 大写、去重、审计）。"""

from types import SimpleNamespace

import pytest

from avelis.importer import scope


def engine(*keys, kind="starrocks"):
    return SimpleNamespace(kind=kind, options=tuple(SimpleNamespace(key=k) for k in keys))


def datasource(**options):
    return SimpleNamespace(id="ds-1", options=dict(options))


@pytest.mark.parametrize(
    ("ref", "expected"),
    [
        ("ec_trade.orders", "ec_trade"),
        ("default_catalog.ec_dw.dwd_order", "ec_dw"),
        ("`ods`.`events`", "ods"),
        ('"Sales"."Orders"', "Sales"),
        ("[dbo].[t1]", "dbo"),
        ("orders", None),
        ("", None),
    ],
)
def test_ref_database(ref, expected):
    assert scope.ref_database(ref) == expected


def test_scope_key_follows_engine_options():
    assert scope.scope_key(engine("host", "databases", "tls")) == "databases"
    assert scope.scope_key(engine("schemas", "service")) == "schemas"
    assert scope.scope_key(engine("endpoint")) is None


def test_annotate_unmatched_marks_sources_against_current_scope():
    items = [
        {"ref": "ods.events", "role": "source", "count": 3, "statement_ids": ["s1"]},
        {"ref": "EC_TRADE.orders", "role": "source", "count": 1, "statement_ids": ["s2"]},
        {"ref": "orders", "role": "source", "count": 1, "statement_ids": ["s3"]},
        {"ref": "tmp.staging", "role": "target", "count": 2, "statement_ids": ["s4"]},
    ]
    before = [dict(item) for item in items]
    out = scope.annotate_unmatched(items, datasource(databases=["ec_trade", "ec_dw"]), engine("databases"))
    assert items == before  # 不修改入参
    assert out[0] | {} == before[0] | {"database": "ods", "in_scope": False, "addable": True}
    assert out[1] | {} == before[1] | {"database": "EC_TRADE", "in_scope": True, "addable": False}  # 不区分大小写
    assert out[2] | {} == before[2] | {"database": None, "in_scope": False, "addable": False}
    assert out[3] == before[3] | {"database": "tmp"}  # 写入目标只补库名


def test_annotate_unmatched_without_scope_key_is_never_addable():
    out = scope.annotate_unmatched(
        [{"ref": "ods.events", "role": "source", "count": 1, "statement_ids": []}], datasource(), engine("endpoint")
    )
    assert out[0]["in_scope"] is False and out[0]["addable"] is False


@pytest.fixture
def audits(monkeypatch):
    calls = []
    monkeypatch.setattr(scope.audit, "record", lambda *args, **kwargs: calls.append((args, kwargs)))
    return calls


def test_add_to_scope_appends_missing_names_and_audits(audits):
    ds = datasource(databases=["ec_trade"], tls={"mode": "verify"})
    original = ds.options
    added = scope.add_to_scope(object(), "user", ds, engine("databases"), ["ods", "EC_TRADE", "ods", "dim"])
    assert added == ["ods", "dim"]
    assert ds.options == {"databases": ["ec_trade", "ods", "dim"], "tls": {"mode": "verify"}}
    assert ds.options is not original  # 整体替换，JSON 列才会被识别为变更
    assert original == {"databases": ["ec_trade"], "tls": {"mode": "verify"}}
    ((args, kwargs),) = audits
    assert args[2:] == ("datasource.scope_add", "datasource", "ds-1")
    assert kwargs == {"key": "databases", "added": ["ods", "dim"]}


def test_add_to_scope_uppercases_oracle_schemas(audits):
    ds = datasource(schemas=["SALES"])
    assert scope.add_to_scope(object(), "user", ds, engine("schemas", kind="oracle"), ["hr", "sales"]) == ["HR"]
    assert ds.options["schemas"] == ["SALES", "HR"]


def test_add_to_scope_nothing_new_does_not_audit_or_rewrite(audits):
    ds = datasource(databases=["ods"])
    original = ds.options
    assert scope.add_to_scope(object(), "user", ds, engine("databases"), ["ODS"]) == []
    assert ds.options is original and audits == []


@pytest.mark.parametrize("names", [[], [" "], ["ok", ""], [f"db{i}" for i in range(scope.MAX_ADD + 1)]])
def test_add_to_scope_rejects_bad_input(audits, names):
    with pytest.raises(ValueError):
        scope.add_to_scope(object(), "user", datasource(databases=[]), engine("databases"), names)
    assert audits == []


def test_add_to_scope_requires_scope_key(audits):
    with pytest.raises(ValueError):
        scope.add_to_scope(object(), "user", datasource(), engine("endpoint"), ["ods"])
