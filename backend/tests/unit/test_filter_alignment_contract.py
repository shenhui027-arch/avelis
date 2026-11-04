"""筛选对齐的公开响应：保留原始证据，给前端稳定的类型与可点候选。"""

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from avelis.api.ask import answer_view
from avelis.api.schemas import AnalysisRunOut, AnswerOut, FilterAlignmentOut
from avelis.i18n import gettext, locale_context

ALIGNMENT = {
    "dimension": "region",
    "original": "华东区",
    "value": "华东",
    "method": "normalized",
    "asset_id": None,
    "table": None,
    "column": None,
}


def test_answer_view_exposes_typed_alignment_without_removing_evidence():
    answer = SimpleNamespace(
        id="answer",
        question="查询",
        asked_by="user",
        asked_at=datetime.now(timezone.utc),
        status="blocked",
        message=None,
        error_code=None,
        model_adapter="fake",
        intent={},
        plan={},
        sql=None,
        metric_version_ids=[],
        result={},
        evidence={"filter_alignment": [ALIGNMENT]},
        freshness={},
        executed_at=None,
        duration_ms=None,
        cache_hit=False,
        recheck_status=None,
        thread_id=None,
        steps=[],
        explanation=None,
        explanation_source=None,
    )
    response = AnswerOut.model_validate(answer_view(answer))
    assert response.evidence["filter_alignment"] == [ALIGNMENT]
    assert response.filter_alignment[0].model_dump() == ALIGNMENT
    answer.evidence = {}
    assert AnswerOut.model_validate(answer_view(answer)).filter_alignment == []


def test_analysis_schema_preserves_alignment_and_structured_clarification():
    pending = {
        "kind": "clarify",
        "question": "请选择取值",
        "dimension": "region",
        "options": ["华东"],
    }
    response = AnalysisRunOut.model_validate(
        {
            "id": "run",
            "conversation_id": "conversation",
            "turn": 1,
            "question": "查询",
            "status": "waiting",
            "trigger": "manual",
            "created_at": "2026-11-04T00:00:00Z",
            "filter_alignment": [ALIGNMENT],
            "pending": pending,
        }
    )
    assert response.filter_alignment[0].model_dump() == ALIGNMENT
    assert response.pending == pending
    contract = FilterAlignmentOut.model_json_schema()
    assert contract["properties"]["method"]["enum"] == ["exact", "normalized", "dimension_table"]


@pytest.mark.parametrize("locale", ["en", "zh-TW"])
def test_alignment_messages_are_translated_without_changing_member_values(locale):
    message = "筛选值“{original}”需要确认（维度：{dimension}）；候选：{candidates}。"
    with locale_context(locale):
        rendered = gettext(message, original="华东区", dimension="region", candidates="华东")
        assert "华东区" in rendered and "华东" in rendered and "region" in rendered
        assert rendered != message.format(original="华东区", dimension="region", candidates="华东")
        hint = "可在维表中维护别名并建立关联路径。"
        assert gettext(hint) != hint
        for error in ("请按待确认项提交筛选候选", "筛选回答包含未知、重复的待确认项或无效候选"):
            assert gettext(error) != error
