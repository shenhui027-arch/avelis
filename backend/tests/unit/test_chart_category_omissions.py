"""类目展示截断的规格、导出和完整数据回看契约。"""

import json
import re
from copy import deepcopy

import pytest
from reportlab.graphics.shapes import Drawing, String

from avelis.analysis import dataset_access, export
from avelis.analysis.figures import category_omission_note, dataset_charts, report_figures
from avelis.api.schemas import ChartSpec
from avelis.i18n import locale_context
from tests.unit.test_analysis_export import _flowables, _pdf_story, _story_text
from tests.unit.test_analysis_figures import choice, view
from tests.unit.test_analysis_toolbox import make_box


def category_dataset(*, count=25, additive=False, multiple=False, comparison=False):
    columns = ["区域", "转化率"]
    unit = {"kind": "level" if additive else "ratio", "additive": additive}
    units = {"转化率": unit}
    rows = [[f"区域{i}", i / 100] for i in range(count)]
    if comparison:
        columns += ["转化率·上一期", "转化率·变化", "转化率·变化率"]
        units.update(
            {
                "转化率·上一期": unit,
                "转化率·变化": {"kind": "delta", "of_ratio": not additive, "additive": additive},
                "转化率·变化率": {"kind": "rate_change"},
            }
        )
        rows = [[*row, 0.1, row[1] - 0.1, row[1] / 0.1 - 1] for row in rows]
    elif multiple:
        columns += ["数量"]
        units["数量"] = {"kind": "count", "additive": True}
        rows = [[*row, 100] for row in rows]
    return {
        "ref": "D1",
        "kind": "calc",
        "title": "区域指标",
        "columns": columns,
        "rows": rows,
        "row_count": count,
        "meta": {"units": units},
    }


@pytest.mark.parametrize("multiple,comparison", [(False, False), (True, False), (False, True)])
@pytest.mark.parametrize("top_n", [None, 7])
def test_nonadditive_category_omissions_cover_all_truncation_paths(multiple, comparison, top_n):
    data = category_dataset(multiple=multiple, comparison=comparison)
    before = deepcopy(data)
    options = dataset_charts(data)
    preference = {"kind": options[0]["kind"], "top_n": top_n}
    options = dataset_charts(data, preference)
    for spec in options:
        # 显式偏好只改选中图种；其他候选仍保留默认展示上限。
        limit = top_n if spec["kind"] == preference["kind"] and top_n is not None else 20
        assert spec["omitted_categories"] == {
            "count": 25 - limit,
            "reason": "top_n" if limit != 20 else "display_limit",
        }
        assert spec["others"] is None
        assert len(spec["categories"]) == limit
        assert ChartSpec.model_validate(spec).model_dump(mode="json") == spec
    assert data == before and len(data["rows"]) == 25


@pytest.mark.parametrize("multiple,comparison", [(False, False), (True, False), (False, True)])
def test_additive_others_and_category_omissions_do_not_double_count(multiple, comparison):
    data = category_dataset(additive=True, multiple=multiple, comparison=comparison)
    for chart in dataset_charts(data, {"kind": dataset_charts(data)[0]["kind"], "top_n": 7}):
        if chart["others"]:
            assert chart["omitted_categories"] is None
            assert chart["others"]["count"] == 25 - (len(chart["categories"]) - 1)
        else:
            assert chart["omitted_categories"] == {"count": 5, "reason": "display_limit"}


@pytest.mark.parametrize("multiple,comparison", [(False, False), (True, False), (False, True)])
def test_no_omission_below_limit(multiple, comparison):
    assert all(
        chart["omitted_categories"] is None
        for chart in dataset_charts(category_dataset(count=20, multiple=multiple, comparison=comparison))
    )


@pytest.mark.parametrize("locale", ["zh-CN", "zh-TW", "en"])
@pytest.mark.parametrize("multiple", [False, True])
@pytest.mark.parametrize("top_n", [None, 7])
def test_pdf_html_and_markdown_explain_omission(monkeypatch, locale, multiple, top_n):
    data = category_dataset(multiple=multiple)
    kind = dataset_charts(data)[0]["kind"]
    source = view(data, [choice(kind=kind, top_n=top_n)])
    with locale_context(locale):
        chart = report_figures(source)[0]["chart"]
        note = category_omission_note(chart)
        expected = {"zh-CN": "个类目", "zh-TW": "個類目", "en": "categories"}[locale]
        assert expected in note and str(25 - (top_n or 20)) in note
        assert "Top N" in note if top_n else "Top N" not in note
        markup = export.to_html(source)
        svg = re.search(r"<svg\b.*?</svg>", markup, re.S).group()
        # 英文提示可能折行，但全文仍保留在 SVG 的文字节点里。
        assert note.replace(" ", "") in re.sub(r"<[^>]*>", "", svg).replace(" ", "").replace("\n", "")
        assert note in export.to_markdown(source)
        assert export.to_pdf(source).startswith(b"%PDF")
        story = _pdf_story(monkeypatch, source)
        texts = list(_story_text(story))
        for item in _flowables(story):
            if isinstance(item, Drawing):
                texts.extend(s.text for s in item.contents if isinstance(s, String))
        assert note.replace(" ", "") in "".join(texts).replace(" ", "")


def test_full_dataset_is_available_when_spec_table_has_only_display_rows(monkeypatch):
    monkeypatch.setattr(dataset_access, "can_read_dataset", lambda *args: True)
    box = make_box(monkeypatch=monkeypatch)
    data = category_dataset()
    box._dataset("calc", data["title"], data["columns"], data["rows"], meta=data["meta"])
    chart = dataset_charts(data)[0]
    assert len(chart["categories"]) == 20 and chart["omitted_categories"]["count"] == 5
    complete = json.loads(box.dispatch("view_dataset", {"ref": "D1", "limit": 60}))
    assert complete["rows"] == data["rows"] and complete["row_count"] == 25
