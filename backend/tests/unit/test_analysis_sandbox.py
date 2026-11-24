"""受控只读 Python 计算沙箱单元测试（M3D）：AST 注入拦截、步数/超时上限、输出上限、常量假设传播、
多数据集合并与证据核验。"""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from avelis.analysis import sandbox
from avelis.analysis.sandbox import SandboxError
from avelis.analysis.verify import ground_report
from avelis.connectors.base import QueryResult
from tests.unit.test_analysis_explore_tool import FakeConnector, explore_box
from tests.unit.test_analysis_toolbox import make_box


@pytest.mark.parametrize(
    "code",
    [
        "import os\nresult = 1",
        "from math import sin\nresult = 1",
        "result = __import__('os').listdir('.')",
        "result = open('/etc/passwd').read()",
        "result = ().__class__.__base__.__subclasses__()",
        "result = eval('1+1')",
        "result = exec('x=1')",
        "while True:\n    pass",
        "def f():\n    return 1\nresult = f()",
        "class X:\n    pass\nresult = 1",
        "with open('a') as f:\n    result = 1",
        "try:\n    x = 1\nexcept Exception:\n    x = 2\nresult = x",
        "x = _secret\nresult = 1",
        "result = rows[0]._private",
        "args = [1, 2]\nresult = sum(*args)",
        "result = len(range(20000))",
    ],
)
def test_sandbox_blocks_unsafe_ast_and_builtins(code):
    with pytest.raises(SandboxError):
        sandbox.run(code, {"D1": [{"a": 1, "b": 2}]})


def test_sandbox_enforces_step_limit_and_timeout(monkeypatch):
    with pytest.raises(SandboxError, match="步数"):
        sandbox.run(
            "total = 0\nfor i in range(500):\n    for j in range(500):\n        total += 1\nresult = total",
            {"D1": [{"a": 1}]},
        )

    clock = iter([100.0, 100.0, 100.5, 101.0])
    monkeypatch.setattr(sandbox.time, "monotonic", lambda: next(clock, 102.0))
    with pytest.raises(SandboxError, match="超时"):
        sandbox.run(
            "total = 0\nfor i in range(100):\n    total += i\nresult = total",
            {"D1": [{"a": 1}]},
            timeout_seconds=0.1,
        )


def test_sandbox_enforces_output_caps():
    with pytest.raises(SandboxError, match="行数"):
        sandbox.run(
            "result = [{'i': i} for i in range(501)]",
            {"D1": [{"a": 1}]},
        )
    with pytest.raises(SandboxError, match="列数"):
        sandbox.run(
            "result = {f'c{i}': i for i in range(31)}",
            {"D1": [{"a": 1}]},
        )
    with pytest.raises(SandboxError, match="体积"):
        sandbox.run(
            "result = [{'s': 'x' * 200} for i in range(200)]",
            {"D1": [{"a": 1}]},
        )


def test_sandbox_constants_whitelist_and_assumption_flag():
    clean = sandbox.run(
        "result = [{'region': r['region'], 'pct': round(float(r['amt']) * 100 / 1000, 1)} for r in rows]",
        {"D1": [{"region": "华东", "amt": Decimal("250")}]},
    )
    assert clean.assumption is False
    assert clean.non_whitelist_constants == []
    assert clean.source_evidence_ids == ["D1#1"]

    tainted = sandbox.run(
        "result = [{'region': r['region'], 'est': float(r['amt']) * 0.3} for r in rows]",
        {"D1": [{"region": "华东", "amt": Decimal("250")}]},
    )
    assert tainted.assumption is True
    assert tainted.non_whitelist_constants == ["0.3"]


def test_sandbox_input_rows_are_isolated_copies():
    original = [{"region": "华东", "amt": 100}]
    res = sandbox.run(
        "rows[0]['amt'] = 999\nresult = {'v': rows[0]['amt']}",
        {"D1": original},
    )
    assert original[0]["amt"] == 100
    assert res.rows == [[999]]


def test_toolbox_sandbox_calc_multi_dataset_merge_and_grounding(monkeypatch):
    box = make_box(monkeypatch=monkeypatch)
    d1 = box._dataset(
        "governed",
        "销售",
        ["region", "sales"],
        [["华东", Decimal("200")], ["华南", Decimal("100")]],
        meta={"units": {"sales": {"kind": "level", "unit": "元"}}, "metric_codes": ["ec_gmv"]},
    )
    d2 = box._dataset(
        "governed",
        "预算",
        ["region", "budget"],
        [["华东", Decimal("160")], ["华南", Decimal("120")]],
        meta={"units": {"budget": {"kind": "level", "unit": "元"}}, "metric_codes": ["ec_budget"]},
    )
    code = (
        "bmap = {r['region']: (r['budget'], evidence_id(r)) for r in D2}\n"
        "out = []\n"
        "for r in D1:\n"
        "    b, beid = bmap[r['region']]\n"
        "    out.append({'region': r['region'], 'gap': Decimal(r['sales']) - Decimal(b), 'src': evidence_id(r)})\n"
        "result = out\n"
    )
    raw = json.loads(
        box.dispatch(
            "sandbox_calc",
            {"datasets": [f"D{d1.seq}", f"D{d2.seq}"], "label": "销售与预算差额", "code": code, "purpose": "合并两表"},
        )
    )
    assert "error" not in raw
    ds = box.datasets()[raw["dataset"]]
    assert ds.kind == "calc" and ds.meta["assumption"] is False and ds.meta["explore"] is False
    assert ds.meta["source_evidence_ids"] == ["D2#1", "D2#2", "D1#1", "D1#2"]
    assert ds.meta["metric_codes"] == ["ec_budget", "ec_gmv"]

    data = {
        ref: {
            "kind": item.kind,
            "columns": list(item.columns),
            "rows": [dict(zip(item.columns, r)) for r in item.rows],
            "summary": item.meta.get("summary"),
            "truncated": item.truncated,
            **{
                k: item.meta.get(k)
                for k in ("units", "summary_units", "explore", "assumption", "column_evidence", "metric_codes")
            },
        }
        for ref, item in box.datasets().items()
    }
    report = {
        "title": "区域差额分析",
        "summary": "核对销售与预算",
        "conclusion": "ranked_reasons",
        "claims": [
            {
                "type": "calc",
                "parts": [
                    {"dataset": raw["dataset"], "label": "region", "row": 1},
                    " 差额 ",
                    {"dataset": raw["dataset"], "column": "gap", "match": {"region": "华东"}},
                ],
            }
        ],
        "causes": [
            {
                "type": "member",
                "entity": {"evidence_id": f"{raw['dataset']}#1"},
                "direction": "up",
                "contribution": {"dataset": raw["dataset"], "column": "gap", "row": 1},
                "evidence": [raw["dataset"]],
            }
        ],
    }
    rendered, verification = ground_report(report, data, None)
    assert verification["ok"] is True, verification
    assert rendered["claims"][0]["segments"][2]["raw"] == "40"


def test_toolbox_sandbox_calc_taints_assumption_from_explore_column_and_constants(monkeypatch):
    result = QueryResult(columns=["region", "fake", "amount"], rows=[["华东", 42, 100]], truncated=False)
    box, _ = explore_box(monkeypatch, connector=FakeConnector(result=result))
    out = json.loads(
        box.dispatch(
            "query_sql",
            {"purpose": "探索", "sql": "SELECT region, 42 AS fake, amount FROM d1.facts"},
        )
    )
    ref = out["dataset"]
    calc_col_taint = json.loads(
        box.dispatch(
            "sandbox_calc",
            {
                "datasets": [ref],
                "label": "引用假设列",
                "code": "result = [{'region': r['region'], 'v': r['fake']} for r in rows]",
                "purpose": "test",
            },
        )
    )
    assert box.datasets()[calc_col_taint["dataset"]].meta["assumption"] is True
    assert box.datasets()[calc_col_taint["dataset"]].meta["explore"] is True

    calc_const_taint = json.loads(
        box.dispatch(
            "sandbox_calc",
            {
                "datasets": [ref],
                "label": "使用非白名单常量",
                "code": "result = [{'region': r['region'], 'v': float(r['amount']) * 0.3} for r in rows]",
                "purpose": "test",
            },
        )
    )
    tainted_ref = calc_const_taint["dataset"]
    assert box.datasets()[tainted_ref].meta["assumption"] is True

    ds = box.datasets()[tainted_ref]
    data = {
        tainted_ref: {
            "kind": ds.kind,
            "columns": list(ds.columns),
            "rows": [dict(zip(ds.columns, r)) for r in ds.rows],
            "summary": ds.meta.get("summary"),
            "truncated": ds.truncated,
            **{k: ds.meta.get(k) for k in ("units", "summary_units", "explore", "assumption", "column_evidence")},
        }
    }
    _, verification = ground_report(
        {
            "title": "t",
            "summary": "s",
            "claims": [{"type": "calc", "parts": ["估算 ", {"dataset": tainted_ref, "column": "v", "row": 1}]}],
        },
        data,
        None,
    )
    assert verification["claims"][0]["status"] == "unverified"
    assert "假设计算" in verification["claims"][0]["problems"][0]


@pytest.mark.parametrize(
    "code",
    [
        "a = [0] * 10**8\nresult = len(a)",
        "s = 'a' * 10**8\nresult = len(s)",
        "t = (0,) * 10**8\nresult = len(t)",
        "a = 10**8 * [0]\nresult = len(a)",
        "a = [0] * -1\nresult = len(a)",
        "a = [0]\na *= 10**8\nresult = len(a)",
        "a = [0]\nfor i in range(25):\n    a = a + a\nresult = len(a)",
        "s = 'a'\nfor i in range(25):\n    s += s\nresult = len(s)",
        "a = [0] * 1000\nfor i in range(10):\n    a.extend(a)\nresult = len(a)",
        "s = f\"{'a':>100000000}\"\nresult = len(s)",
        "s = f\"{Decimal('1e100000'):f}\"\nresult = len(s)",
        "s = f\"{Decimal('1e50000000'):%}\"\nresult = len(s)",
        "s = f\"{Decimal('1e-50000000'):%}\"\nresult = len(s)",
        "s = f\"{Decimal('1e50000000'):,%}\"\nresult = len(s)",
        "s = f\"{Decimal('1e50000000'):.2%}\"\nresult = len(s)",
        "x = 10 ** 100\nfor i in range(16):\n    x = x * x\nresult = 1",
        "x = 10 ** 100\nfor i in range(16):\n    x *= x\nresult = 1",
        "result = ((10**100)**100)**100",
        "x = int(Decimal('1e50000000'))\nresult = 1",
        "x = round(Decimal('1e50000000'))\nresult = 1",
        "x = int(Decimal('1e100000'))\nresult = 1",
        "x = round(Decimal('1e100000'))\nresult = 1",
        "x = int('f' * 64000, 16)\nresult = 1",
        "x = int('f' * 5000, 16)\nresult = 1",
    ],
)
def test_sandbox_blocks_resource_exhaustion_bombs(code):
    with pytest.raises(SandboxError):
        sandbox.run(code, {"D1": [{"a": 1}]})


def test_sandbox_allows_valid_large_int_math():
    res2 = sandbox.run("result = {'v': str(2 ** 100)}", {"D1": [{"a": 1}]})
    assert res2.rows == [[str(2**100)]]
    res100 = sandbox.run("result = {'v': str(100 ** 100)}", {"D1": [{"a": 1}]})
    assert res100.rows == [[str(100**100)]]
    assert res100.assumption is False
    res_int_dec = sandbox.run("result = {'v': int(Decimal('12345.67'))}", {"D1": [{"a": 1}]})
    assert res_int_dec.rows == [[12345]]
    res_round_dec = sandbox.run("result = {'v': round(Decimal('12345.67'))}", {"D1": [{"a": 1}]})
    assert res_round_dec.rows == [[12346]]
    res_hex = sandbox.run("result = {'v': int('ff', 16)}", {"D1": [{"a": 1}]})
    assert res_hex.rows == [[255]]


@pytest.mark.parametrize(
    "code",
    [
        "f = lambda g: g(g)\nresult = f(f)",
        "x = 10 ** 100\ny = x ** 100\nresult = y",
    ],
)
def test_sandbox_wraps_recursion_and_serialization_errors(code):
    with pytest.raises(SandboxError):
        sandbox.run(code, {"D1": [{"a": 1}]})


def test_sandbox_string_encoded_constants_and_clean_dataset_passthrough():
    for code, expected in [
        ("result = [{'v': Decimal(r['a']) * Decimal('0.3')} for r in rows]", "0.3"),
        ("k = '0.' + '3'\nresult = [{'v': Decimal(k)}]", "0.3"),
        ("result = [{'v': float('0.3')}]", "0.3"),
        ("result = [{'v': int('42')}]", "42"),
        ("result = [{'v': number('0.3')}]", "0.3"),
        ("result = [{'v': Decimal('42')}]", "42"),
        ("result = [{'v': floor('42')}]", "42"),
        ("result = [{'v': mean(['0.3'])}]", "0.3"),
    ]:
        res = sandbox.run(code, {"D1": [{"a": "42"}]})
        assert res.assumption is True, code
        assert expected in res.non_whitelist_constants, (code, res.non_whitelist_constants)

    for code, ds in [
        ("result = [{'v': Decimal(r['a'])} for r in rows]", {"D1": [{"a": "0.3"}]}),
        ("result = [{'v': float(r['a'])} for r in rows]", {"D1": [{"a": "0.3"}]}),
        ("result = [{'v': number(r['a'])} for r in rows]", {"D1": [{"a": "0.3"}]}),
        ("result = [{'v': floor(r['a'])} for r in rows]", {"D1": [{"a": "42"}]}),
        ("result = [{'v': mean([r['a']])} for r in rows]", {"D1": [{"a": "0.3"}]}),
        ("result = [{'v': Decimal(r['a'].strip()) * Decimal('100')} for r in rows]", {"D1": [{"a": " 0.3 "}]}),
        ("result = [{'v': Decimal(r['a'])} for r in rows]", {"D1": [{"a": "42"}]}),
        ("result = [{'v': Decimal('100') + int('24')}]", {"D1": [{"a": "0.3"}]}),
    ]:
        res = sandbox.run(code, ds)
        assert res.assumption is False, (code, res.non_whitelist_constants)
        assert res.non_whitelist_constants == []


def test_toolbox_sandbox_calc_string_constant_and_dynamic_column_taint_grounding(monkeypatch):
    result = QueryResult(columns=["region", "fake", "amount"], rows=[["华东", 42, 100]], truncated=False)
    box, _ = explore_box(monkeypatch, connector=FakeConnector(result=result))
    ref = json.loads(
        box.dispatch("query_sql", {"purpose": "探索", "sql": "SELECT region, 42 AS fake, amount FROM d1.facts"})
    )["dataset"]

    def _ground(ds_ref: str, col_name: str):
        ds = box.datasets()[ds_ref]
        data = {
            ds_ref: {
                "kind": ds.kind,
                "columns": list(ds.columns),
                "rows": [dict(zip(ds.columns, r)) for r in ds.rows],
                "summary": ds.meta.get("summary"),
                "truncated": ds.truncated,
                **{k: ds.meta.get(k) for k in ("units", "summary_units", "explore", "assumption", "column_evidence")},
            }
        }
        return ground_report(
            {
                "title": "t",
                "summary": "s",
                "claims": [{"type": "calc", "parts": ["值 ", {"dataset": ds_ref, "column": col_name, "row": 1}]}],
            },
            data,
            None,
        )[1]

    # Decimal('0.3') bypass probe must mark assumption=True and fail grounding
    str_const_out = json.loads(
        box.dispatch(
            "sandbox_calc",
            {
                "datasets": [ref],
                "label": "字符串常量",
                "code": "result = [{'region': r['region'], 'v': Decimal(r['amount']) * Decimal('0.3')} for r in rows]",
                "purpose": "test",
            },
        )
    )
    assert box.datasets()[str_const_out["dataset"]].meta["assumption"] is True
    v1 = _ground(str_const_out["dataset"], "v")
    assert v1["claims"][0]["status"] == "unverified"
    assert "假设计算" in v1["claims"][0]["problems"][0]

    # Dynamic key r['fa' + 'ke'], r.get('fa' + 'ke'), r.values() must propagate column taint
    for code in [
        "k = 'fa' + 'ke'\nresult = [{'region': r['region'], 'v': r[k]} for r in rows]",
        "k = 'fa' + 'ke'\nresult = [{'region': r['region'], 'v': r.get(k)} for r in rows]",
        "result = [{'region': r['region'], 'v': list(r.values())[1]} for r in rows]",
    ]:
        dyn_out = json.loads(
            box.dispatch(
                "sandbox_calc",
                {"datasets": [ref], "label": "动态列读取", "code": code, "purpose": "test"},
            )
        )
        assert box.datasets()[dyn_out["dataset"]].meta["assumption"] is True, code
        v2 = _ground(dyn_out["dataset"], "v")
        assert v2["claims"][0]["status"] == "unverified"
        assert "假设计算" in v2["claims"][0]["problems"][0]

    # Clean column access without touching 'fake' keeps assumption=False
    clean_out = json.loads(
        box.dispatch(
            "sandbox_calc",
            {
                "datasets": [ref],
                "label": "仅用正常列",
                "code": "result = [{'region': r['region'], 'v': r['amount']} for r in rows]",
                "purpose": "test",
            },
        )
    )
    assert box.datasets()[clean_out["dataset"]].meta["assumption"] is False


def test_toolbox_sandbox_calc_blocks_sql_string_literal_smuggling(monkeypatch):
    result = QueryResult(columns=["region", "sneaky", "amount"], rows=[["华东", "0.3", 100]], truncated=False)
    box, _ = explore_box(monkeypatch, connector=FakeConnector(result=result))
    ref = json.loads(
        box.dispatch(
            "query_sql",
            {"purpose": "探索", "sql": "SELECT region, '0.3' AS sneaky, amount FROM d1.facts"},
        )
    )["dataset"]
    out = json.loads(
        box.dispatch(
            "sandbox_calc",
            {
                "datasets": [ref],
                "label": "走私字符串常量",
                "code": (
                    "result = [{'region': r['region'], 'v': Decimal(r['amount']) * Decimal(r['sneaky'])} for r in rows]"
                ),
                "purpose": "test",
            },
        )
    )
    assert box.datasets()[out["dataset"]].meta["assumption"] is True

    clean_out = json.loads(
        box.dispatch(
            "sandbox_calc",
            {
                "datasets": [ref],
                "label": "仅用正常列",
                "code": "result = [{'region': r['region'], 'v': r['amount']} for r in rows]",
                "purpose": "test",
            },
        )
    )
    assert box.datasets()[clean_out["dataset"]].meta["assumption"] is False


def test_sandbox_taint_propagation_sum_and_resource_caps():
    # _DatasetStr.lower/upper/replace 保留数据集来源标记（assumption=False），而脚本字面量仍记为假设（True）
    from_dataset = sandbox.run(
        "result = Decimal(rows[0]['s'].lower().upper().replace('X', ''))",
        {"D1": [{"s": "0.25x"}]},
    )
    assert from_dataset.assumption is False
    from_literal = sandbox.run(
        "result = Decimal('0.25x'.lower().upper().replace('X', ''))",
        {"D1": [{"s": "0.25x"}]},
    )
    assert from_literal.assumption is True

    # safe_sum 禁止列表/字符串拼接，也禁止超大整数指数爆炸
    with pytest.raises(SandboxError):
        sandbox.run("result = sum([['a'], ['b']], [])", {"D1": [{"v": 1}]})
    with pytest.raises(SandboxError):
        sandbox.run("result = sum([10**2000, 1])", {"D1": [{"v": 1}]})

    # 切片赋值、set.add 与嵌套深度超限都会被拦截
    with pytest.raises(SandboxError):
        sandbox.run(
            f"xs = [1]\nxs[0:1] = list(range({sandbox.MAX_SEQ_LEN + 5}))\nresult = len(xs)",
            {"D1": [{"v": 1}]},
        )
    with pytest.raises(SandboxError):
        sandbox.run(
            f"s = set()\nfor i in range({sandbox.MAX_SEQ_LEN + 5}):\n    s.add(i)\nresult = len(s)",
            {"D1": [{"v": 1}]},
        )
    with pytest.raises(SandboxError):
        sandbox.run("x = 'a'\nfor _ in range(18):\n    x = x + x\nresult = len(f'{x}')", {"D1": [{"v": 1}]})


def test_sandbox_supports_complex_multi_dataset_decomp_and_caps():
    # 模拟真实多数据集结构拆解脚本（>400 AST 节点，含 setdefault/update/pop/insert/tuple.count/set.update/reversed）
    code = """
orders = {r['region']: float(r['订单数']) for r in D4}
aov = {r['region']: float(r['客单价']) for r in D4}
cat = {}
cats = []
seen = set()
seen.update([r['category'] for r in D6])
for r in D6:
    reg = r['region']
    c = r['category']
    if c not in cats:
        cats.append(c)
    cat.setdefault(reg, {})[c] = (float(r['类目销售额']), float(r['类目销量']))
base = '华东'
base_sales = sum(v[0] for v in cat[base].values())
out = []
tags = ('a', 'b', 'a')
for reg in list(reversed(['西南', '华南', '华东'])):
    reg_sales = sum(v[0] for v in cat[reg].values())
    row = {'region': reg, '客单价': round(aov[reg], 2), '总销售额': round(reg_sales, 2), 'tag_cnt': tags.count('a')}
    tmp = {'extra': 1, 'drop_me': 2}
    tmp.pop('drop_me')
    row.update(tmp)
    total_gap = 0.0
    for c in cats:
        s_reg, u_reg = cat[reg][c]
        s_b, u_b = cat[base][c]
        per_o_reg = s_reg / orders[reg]
        per_o_b = s_b / orders[base]
        gap = per_o_reg - per_o_b
        total_gap += gap
        row[c] = round(gap, 4)
        row[c + '_qty_ratio'] = round((u_reg / orders[reg]) - (u_b / orders[base]), 4)
        row[c + '_mix'] = round((s_reg / reg_sales) - (s_b / base_sales), 4)
    row['total_gap'] = round(total_gap, 4)
    out.insert(len(out), row)
result = out
"""
    import ast as _ast

    assert sum(1 for _ in _ast.walk(_ast.parse(code))) > 400
    d4 = [
        {"region": "华东", "订单数": 100, "客单价": 200},
        {"region": "华南", "订单数": 50, "客单价": 180},
        {"region": "西南", "订单数": 40, "客单价": 150},
    ]
    d6 = [
        {"region": "华东", "category": "数码", "类目销售额": 12000, "类目销量": 60},
        {"region": "华东", "category": "服饰", "类目销售额": 8000, "类目销量": 80},
        {"region": "华南", "category": "数码", "类目销售额": 5000, "类目销量": 25},
        {"region": "华南", "category": "服饰", "类目销售额": 4000, "类目销量": 40},
        {"region": "西南", "category": "数码", "类目销售额": 3000, "类目销量": 20},
        {"region": "西南", "category": "服饰", "类目销售额": 3000, "类目销量": 30},
    ]
    res = sandbox.run(code, {"D4": d4, "D6": d6})
    assert len(res.rows) == 3
    assert "D4#1" in res.source_evidence_ids and "D6#1" in res.source_evidence_ids

    # 扩容上限同样保护 dict.setdefault / dict.update / list.insert / set.update
    for bad_code in (
        f"d = {{}}\nfor i in range({sandbox.MAX_SEQ_LEN + 2}):\n    d.setdefault(i, 1)\nresult = len(d)",
        f"d = {{}}\nfor i in range({sandbox.MAX_SEQ_LEN + 2}):\n    d.update({{i: 1}})\nresult = len(d)",
        f"xs = []\nfor i in range({sandbox.MAX_SEQ_LEN + 2}):\n    xs.insert(0, i)\nresult = len(xs)",
        f"s = set()\ns.update(range({sandbox.MAX_SEQ_LEN + 2}))\nresult = len(s)",
    ):
        with pytest.raises(SandboxError):
            sandbox.run(bad_code, {"D1": [{"v": 1}]})
