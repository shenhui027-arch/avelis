"""导出只翻译产品固定标签，保存的正文、元数据与数据值保持原文。"""

from __future__ import annotations

import csv
import io
import re

import pytest

from avelis.analysis import export
from avelis.i18n import locale_context
from tests.unit.test_analysis_export import _pdf_story, _story_text, sample_view


@pytest.mark.parametrize("audience", ["brief", "full"])
@pytest.mark.parametrize("renderer", [export.to_markdown, export.to_html])
def test_export_translates_fixed_titles_without_changing_saved_body(audience, renderer):
    view = sample_view()
    body = "这段正文保留原文，成员甲和指标甲也保留原文。"
    view["report"]["summary"] = body
    with locale_context("en"):
        rendered = renderer(view, audience=audience)
    assert "Core metrics" in rendered
    assert "核心指标" not in rendered
    assert body in rendered
    assert view["report"]["title"] in rendered
    if renderer is export.to_html:
        assert '<html lang="en">' in rendered


def test_html_uses_taiwan_labels_and_language():
    with locale_context("zh-TW"):
        rendered = export.to_html(sample_view())
    assert '<html lang="zh-TW">' in rendered
    assert "核心指標" in rendered
    assert "核心指标" not in rendered


def test_pdf_fixed_titles_follow_the_request_language(monkeypatch):
    view = sample_view()
    with locale_context("en"):
        rendered = "\n".join(_story_text(_pdf_story(monkeypatch, view, audience="full")))
    assert "Core metrics" in rendered
    assert "核心指标" not in rendered
    assert view["report"]["summary"] in rendered


def test_source_csv_translates_headers_but_preserves_source_text():
    view = sample_view()
    with locale_context("en"):
        rendered = export.sources_csv(view).decode("utf-8-sig")
    rows = list(csv.reader(io.StringIO(rendered)))
    assert len(rows[0]) == len(export.SOURCE_COLUMNS)
    assert not any(re.search(r"[\u4e00-\u9fff]", label) for label in rows[0])
    with locale_context("zh-CN"):
        original = list(csv.reader(io.StringIO(export.sources_csv(view).decode("utf-8-sig"))))
    # 位置、类型、量纲等产品标签可翻译，显示值、来源名、路径、单位及原值必须相同。
    preserved = (2, 3, 4, 5, 7, 8, 11)
    assert [[row[index] for index in preserved] for row in rows[1:]] == [
        [row[index] for index in preserved] for row in original[1:]]


def test_fixed_label_collision_does_not_translate_metadata_or_members():
    dataset = {"kind": "calc", "columns": ["member", "current"], "rows": [["本期", "核心指标"]],
               "meta": {"column_docs": {"member": {"comment": "核心指标"}}}}
    with locale_context("en"):
        table = export._table_data(dataset, brief=True)
    assert table[0][0] == "核心指标"
    assert table[0][1] != "本期"
    assert table[1] == ["本期", "核心指标"]
