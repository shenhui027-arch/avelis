"""产品代码与具体业务解耦（ADR 0010 第 5 点）：扫描 backend/src 与 frontend/src，出现演示数据集特有的词就失败。

电商数据集只是演示与评测场景：演示口径、示例问题的种子只能放在 scripts/、demo_setup.py（只由 bootstrap demo 调用）、
tests/、docs/ 与评测集里。词表随演示数据集一起维护，新增演示场景时同步补充。
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SCANNED = [ROOT / "backend" / "src", ROOT / "frontend" / "src"]
EXCLUDED = {"demo_setup.py", "openapi.json", "schema.d.ts"}  # 演示种子与自动生成的接口描述
SUFFIXES = {".py", ".ts", ".tsx", ".js", ".mjs", ".css", ".html", ".json", ".sql", ".po"}
# 演示数据集特有的词：库名、表名前缀、口径编码，以及演示用的业务词
DEMO_TERMS = [r"\bec_", r"\bdws_", r"ec_dw", r"paid_sales", r"orders_cnt", "成交额", "门店", "客单价", "华东", "华南",
              "华北", "西南", "客流", "抖音", "福田", "广州", "深圳", "商圈", "楼层", "投放", "实付", "daily_sales", "shop_dim",
              r"shop\.orders", r"\bmfg_", "production_report", "material_receipt", "maintenance_order",
              "恒达材料", "HD-20260820-A", "sales_amount", "order_date", r"'paid'", r"\bstore\b", r"\bSTORE\b"]
PATTERN = re.compile("|".join(DEMO_TERMS))
# 写死的日历日期（演示期间的节假日、默认窗口）：Python 的 date(年, ...) 与前端带引号的 YYYY-MM-DD 字面量
# （后端的带引号日期多是协议版本号，如模型接口的 API 版本，不扫）
DATE_LITERALS = {".py": re.compile(r"\bdate\(20\d\d,"), ".ts": re.compile(r"[\"'`]20\d\d-\d\d-\d\d[\"'`]")}
DATE_LITERALS[".tsx"] = DATE_LITERALS[".ts"]


def _files():
    for base in SCANNED:
        for path in base.rglob("*"):
            if (path.is_file() and path.suffix in SUFFIXES and path.name not in EXCLUDED
                    and "node_modules" not in path.parts and "dist" not in path.parts):
                yield path


def test_product_code_has_no_demo_business_terms():
    hits = []
    for path in _files():
        if path.name.endswith(".test.mjs"):  # 前端测试与后端 tests/ 同样属于测试数据，可以用演示词
            continue
        for no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            for m in PATTERN.finditer(line):
                hits.append(f"{path.relative_to(ROOT)}:{no}: “{m.group(0)}” in {line.strip()[:80]}")
    assert not hits, "产品代码里出现了演示数据集的词（ADR 0010）：\n" + "\n".join(hits)


def test_product_code_has_no_hardcoded_calendar_dates():
    """日期只能来自用户输入、数据或配置：写死的节假日、默认窗口会把演示期间带进其他场景。"""
    hits = []
    for path in _files():
        pattern = DATE_LITERALS.get(path.suffix)
        if pattern is None:
            continue
        for no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if m := pattern.search(line):
                hits.append(f"{path.relative_to(ROOT)}:{no}: “{m.group(0)}” in {line.strip()[:80]}")
    assert not hits, "产品代码里写死了日历日期：\n" + "\n".join(hits)


def test_product_code_does_not_import_demo_modules():
    importers = [str(p.relative_to(ROOT)) for p in _files() if p.suffix == ".py" and p.name != "demo_setup.py"
                 and re.search(r"^\s*(from\s+avelis\s+import\s+.*\bdemo_setup\b|from\s+avelis\.demo_setup|"
                               r"import\s+avelis\.demo_setup|import\s+(?:ec_data|mfg_data)|from\s+(?:ec_data|mfg_data))",
                               p.read_text(encoding="utf-8"), re.M)]
    allowed = {str(Path("backend/src/avelis/bootstrap.py"))}  # bootstrap demo 命令是演示的唯一入口
    assert set(importers) <= allowed, importers


def test_prompt_examples_come_from_visible_metrics():
    """系统提示里的写法示例取自当前用户可见的口径（按编码排序取第一个有维度的指标）；没有口径时用中性占位。"""
    from types import SimpleNamespace

    from avelis.analysis import agent

    budget = SimpleNamespace(tool_calls=10, queries=5)
    empty = agent.system_message(agent.prompt_example(SimpleNamespace(versions={})))
    assert '"column": "<指标>", "match": {"<维度>": "<成员>"}' in empty.content and "暂无可用口径" in empty.content
    catalog = SimpleNamespace(versions={
        "z_metric": (SimpleNamespace(name="指标乙"), SimpleNamespace(dimensions=[{"name": "dim_b"}])),
        "a_nodim": (SimpleNamespace(name="指标丙"), SimpleNamespace(dimensions=[])),
        "b_metric": (SimpleNamespace(name='指标"甲'), SimpleNamespace(dimensions=[{"name": "dim_a"}, {"name": "dim_c"}])),
    })
    example = agent.prompt_example(catalog)
    assert example["metric"] == "指标甲" and example["dimension"] == "dim_a" and example["member"] == "<成员>"
    text = agent.system_message(example).content
    assert '"column": "指标甲", "match": {"dim_a": "<成员>"}' in text and '"distinct": "dim_a"' in text
    assert not PATTERN.search(text)  # 提示词本身不含演示词


def test_example_questions_fall_back_to_metric_templates(monkeypatch):
    """示例问题：可见业务域有配置时用配置；都没有配置时按模板从可见口径生成（ADR 0010 第 3 点）。"""
    from types import SimpleNamespace

    from avelis.ask import examples
    from avelis.ask import service as ask_service

    domains = [SimpleNamespace(name="域甲", example_questions=[{"text": "问题一", "mode": "ask"},
                                                              {"text": "问题二", "mode": "analysis"}, "问题三"])]

    class Session:
        def scalars(self, stmt):
            return list(domains)

    monkeypatch.setattr(examples.access, "domain_ids", lambda s, u: {"d1"})
    assert [q["text"] for q in examples.example_questions(Session(), None, "ask")] == ["问题一", "问题三"]
    assert [q["text"] for q in examples.example_questions(Session(), None, "analysis")] == ["问题二", "问题三"]
    domains[0].example_questions = []
    catalog = SimpleNamespace(versions={
        "m2": (SimpleNamespace(name="指标乙"), SimpleNamespace(dimensions=[{"name": "d", "label": "维度乙"}])),
        "m1": (SimpleNamespace(name="指标甲"), SimpleNamespace(dimensions=[{"name": "d", "label": "维度甲"}]))})
    monkeypatch.setattr(ask_service, "visible_catalog", lambda s, u, with_values: catalog)
    asked = examples.example_questions(Session(), None, "ask")
    assert asked[0] == {"text": "指标甲 按维度甲的最近一期排名", "domain": None, "source": "generated"}
    analysed = [q["text"] for q in examples.example_questions(Session(), None, "analysis")]
    assert analysed[-1] == "指标甲 和 指标乙 按维度甲有什么关系？" and len(analysed) == 3
