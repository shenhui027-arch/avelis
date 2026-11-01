"""历史报告的读取投影：原报告不变，引用原值不能经报告与导出绕过撤权。"""

import json
from copy import deepcopy

from sqlalchemy import JSON, MetaData, create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Session

from avelis.analysis import dataset_access, export, service
from avelis.analysis.dataset_projection import RESTRICTED_TEXT, restrict_report, restrict_verification
from avelis.analysis.verify import ground_report
from avelis.db import Base
from avelis.models import AnalysisDataset, AnalysisRun, User

SECRET_MEMBER = "受限成员甲"
DATA = {
    "D1": {"kind": "governed", "columns": ["member", "value"],
           "rows": [{"member": SECRET_MEMBER, "value": "7654321"}],
           "units": {"value": {"kind": "level"}}},
    "D2": {"kind": "governed", "columns": ["member", "value"],
           "rows": [{"member": "可见成员乙", "value": "4321"}],
           "units": {"value": {"kind": "level"}}},
}


def report_data():
    hidden = {"dataset": "D1", "column": "value", "match": {"member": SECRET_MEMBER}}
    visible = {"dataset": "D2", "column": "value"}
    return ground_report({
        "title": ["成员：", {"dataset": "D1", "label": "member"}],
        "summary": ["受限值：", hidden, "；可见值：", visible],
        "method": ["统计：", {"dataset": "D1", "distinct": "member"}],
        "unexplained": ["仍需分析"],
        "claims": [
            {"type": "fact", "parts": ["结果：", hidden], "refs": ["D1"]},
            {"type": "inference", "parts": ["可见结果：", visible], "refs": ["D2"],
             "premise": ["前提：", hidden], "validation": ["复核：", visible]},
        ],
        "conclusion": "ranked_reasons",
        "causes": [
            {"type": "member", "entity": {"evidence_id": "D1#1"}, "direction": "up",
             "contribution": hidden, "evidence": ["D1"]},
            {"type": "member", "entity": {"evidence_id": "D2#1"}, "direction": "up",
             "contribution": visible, "evidence": ["D2"]},
        ],
    }, DATA, {})


def assert_no_hidden_values(value):
    text = json.dumps(value, ensure_ascii=False)
    assert SECRET_MEMBER not in text
    assert all(hidden not in text for hidden in ("7654321", "7,654,321", "765.43万", "7.65M"))


def test_projection_redacts_all_stored_reference_forms_without_mutating_report():
    report, _ = report_data()
    assert report["fields"]["summary"]["segments"][1]["text"] == "765.43万"
    original = deepcopy(report)
    projected = restrict_report(report, {"D1"})

    assert report == original
    assert_no_hidden_values(projected)
    assert projected["title"] == f"成员：{RESTRICTED_TEXT}"
    assert projected["summary"] == f"受限值：{RESTRICTED_TEXT}；可见值：4321"
    assert projected["method"] == f"统计：{RESTRICTED_TEXT}"
    assert projected["unexplained"] == report["unexplained"]
    hidden = projected["fields"]["summary"]["segments"][1]
    assert hidden["kind"] == "restricted" and hidden["ref"] == "D1"
    assert not {"raw", "path", "label"} & hidden.keys()
    assert projected["fields"]["summary"]["parts"][1] == RESTRICTED_TEXT
    assert projected["claims"][1]["text"] == report["claims"][1]["text"]
    assert projected["claims"][1]["premise"] == f"前提：{RESTRICTED_TEXT}"
    assert projected["claims"][1]["validation"] == report["claims"][1]["validation"]
    assert projected["causes"][0]["restricted"] and projected["causes"][0]["contribution"] is None
    assert projected["causes"][1] == report["causes"][1]


def test_legacy_claim_and_failed_reference_errors_are_withheld():
    report = {"claims": [{"type": "fact", "text": "结果 7654321", "refs": ["D1"],
                           "premise": SECRET_MEMBER}],
              "fields": {"summary": {"text": "错误结果", "parts": [{"dataset": "D1", "column": "value"}],
                                      "segments": [{"kind": "error", "part": 1, "text": "[?]",
                                                    "path": f"D1.value[member={SECRET_MEMBER}]",
                                                    "error": "错误原值 7654321"}],
                                      "errors": ["错误原值 7654321"]}}}
    original = deepcopy(report)
    projected = restrict_report(report, {"D1"})
    assert_no_hidden_values(projected)
    assert projected["claims"][0]["text"] == projected["summary"] == RESTRICTED_TEXT
    assert projected["fields"]["summary"]["errors"] == []
    assert report == original


def test_lookup_summary_is_withheld_and_unrestricted_report_is_unchanged():
    report = {"kind": "lookup", "answer_id": "answer", "title": "查询结果", "summary": "结果 7654321"}
    assert restrict_report(report, set()) is report
    projected = restrict_report(report, set(), lookup_restricted=True)
    assert projected["title"] == report["title"] and projected["answer_id"] == "answer"
    assert projected["summary"] == RESTRICTED_TEXT and report["summary"] == "结果 7654321"


def test_verification_projection_only_removes_errors_for_restricted_records():
    data = deepcopy(DATA)
    data["D1"]["rows"][0]["value"] = "-7654321"
    data["D1"]["units"]["value"] = {"kind": "delta"}
    wrong_direction = {"dataset": "D1", "column": "value", "direction": "up"}
    report, verification = ground_report({
        "title": ["分析结果"], "summary": ["变化：", wrong_direction],
        "claims": [{"type": "fact", "parts": [wrong_direction], "refs": ["D1"]},
                   {"type": "fact", "parts": ["可见结果：", {"dataset": "D2", "column": "value"}],
                    "refs": ["D2", "D3"]}],
    }, data, {})
    original = deepcopy(verification)
    assert "原值 -7654321" in verification["fields"][1]["problems"][0]
    assert "原值 -7654321" in verification["claims"][0]["problems"][0]
    projected_report = restrict_report(report, {"D1"})
    projected = restrict_verification(verification, projected_report)
    assert projected["fields"][1]["problems"] == projected["claims"][0]["problems"] == []
    assert projected["claims"][1] == verification["claims"][1]
    assert {k: projected[k] for k in ("ok", "status", "unverified", "unverified_fields", "sources")} == {
        k: verification[k] for k in ("ok", "status", "unverified", "unverified_fields", "sources")}
    view = {"id": "run", "question": "检查变化", "status": "succeeded", "report": projected_report,
            "verification": projected, "datasets": [], "steps": []}
    for text in (export.to_markdown(view), export.sources_csv(view).decode("utf-8-sig")):
        assert_no_hidden_values(text)
        assert "4321" in text
    assert verification == original


def test_offsetting_conclusion_problems_are_withheld_for_restricted_dependencies():
    values = ("-1234567", "2345678", "-7654321", "8765432")
    data = {"D1": {"kind": "governed", "columns": ["member", "value"],
                   "rows": [{"member": member, "value": value}
                            for member, value in zip(("减少项", "增加项", "最大减少项", "最大增加项"), values)],
                   "units": {"value": {"kind": "delta"}}}}
    report, verification = ground_report({
        "conclusion": "offsetting",
        "causes": [
            {"type": "member", "entity": {"evidence_id": f"D1#{i}"}, "direction": direction,
             "contribution": {"dataset": "D1", "column": "value", "match": {"member": member}},
             "evidence": ["D1"]}
            for i, (direction, member) in enumerate((("down", "减少项"), ("up", "增加项")), 1)
        ],
    }, data, {})
    original = deepcopy(verification)
    problems = json.dumps(verification["conclusion"]["problems"], ensure_ascii=False)
    assert all(value in problems for value in values)
    assert len(verification["conclusion"]["problems"]) == 2

    projected = restrict_verification(verification, restrict_report(report, {"D1"}))
    expected = deepcopy(verification)
    expected["conclusion"]["problems"] = [RESTRICTED_TEXT, RESTRICTED_TEXT]
    assert projected == expected  # 核验状态、计数与其他问题保留原值。
    assert all(value not in json.dumps(projected["conclusion"]) for value in values)
    assert restrict_verification(verification, restrict_report(report, set())) == original
    assert restrict_verification(verification, restrict_report(report, {"D2"})) == original
    assert verification == original


def test_run_view_projection_is_consumed_by_csv_and_markdown_exports(monkeypatch):
    engine = create_engine("sqlite://")
    metadata = MetaData()
    for source in Base.metadata.tables.values():
        table = source.to_metadata(metadata)
        for column in table.columns:
            if isinstance(column.type, JSONB):
                column.type = JSON()
    metadata.create_all(engine)
    report, verification = report_data()
    original, original_verification = deepcopy(report), deepcopy(verification)
    monkeypatch.setattr(dataset_access, "can_read_dataset", lambda s, u, d, known=None: d.seq != 1)
    with Session(engine, expire_on_commit=False) as session:
        user = User(id="reader", username="reader", display_name="阅读者", password_hash="unused")
        run = AnalysisRun(id="run", user_id=user.id, question="检查结果", trigger="manual", status="succeeded",
                          report=report, verification=verification)
        session.add_all([user, run, *[
            AnalysisDataset(run_id=run.id, seq=i, kind="governed", title=f"数据集 D{i}", columns=data["columns"],
                            rows=[[row[column] for column in data["columns"]] for row in data["rows"]])
            for i, data in enumerate(DATA.values(), 1)
        ]])
        session.commit()
        view = service.run_view(session, run, user=user)
        assert_no_hidden_values(view["report"])
        for text in (export.sources_csv(view).decode("utf-8-sig"), export.to_markdown(view)):
            assert_no_hidden_values(text)
            assert "4321" in text
        assert run.report == original and run.verification == original_verification
        session.expire_all()
        assert session.get(AnalysisRun, run.id).report == original
    engine.dispose()
