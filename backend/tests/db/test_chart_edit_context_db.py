"""续问改图的 PostgreSQL 回归：复制已保存结果，并随当前来源权限重新投影。"""

from __future__ import annotations

import json
import uuid
from copy import deepcopy
from types import SimpleNamespace

import pytest
from sqlalchemy import delete, select

from avelis import engines
from avelis.analysis import conversation_context as context
from avelis.ask import service as ask_service
from avelis.db import session_scope
from avelis.models import (
    AnalysisDataset,
    AnalysisRun,
    Answer,
    Asset,
    Datasource,
    Domain,
    DomainMember,
    Metric,
    MetricVersion,
    User,
)

pytestmark = pytest.mark.db


@pytest.fixture
def lookup_world(factory, monkeypatch):
    """来源和口径均存入独立 PostgreSQL 库；任何重新执行源查询都会使测试失败。"""
    owner, member = factory.user(("admin",)), factory.user()
    tag = uuid.uuid4().hex[:10]
    executions = []

    def no_query(*args, **kwargs):
        executions.append((args, kwargs))
        pytest.fail("续问改图不得重新执行取数或创建源连接器")

    monkeypatch.setattr(ask_service, "fulfil", no_query)
    monkeypatch.setattr(ask_service, "query_connector", no_query)
    monkeypatch.setattr(engines, "query_connector", no_query)
    with session_scope() as session:
        domain = Domain(code=f"dchart_{tag}", name="改图测试域", owner_id=owner)
        datasource = Datasource(name=f"chart_{tag}", kind="postgres", host="unused", port=1, options={})
        session.add_all([domain, datasource])
        session.flush()
        session.add(DomainMember(domain_id=domain.id, user_id=member, role="viewer"))
        asset = Asset(
            datasource_id=datasource.id,
            kind="table",
            catalog_name="catalog",
            db_name="chart_test",
            name=f"source_{tag}",
            domain_id=domain.id,
        )
        metric = Metric(code=f"chart_{tag}", name="度量", domain_id=domain.id, owner_id=owner, created_by=owner)
        session.add_all([asset, metric])
        session.flush()
        version = MetricVersion(
            metric_id=metric.id,
            version_no=1,
            status="published",
            description="已保存答案的锁定版本",
            base_asset_id=asset.id,
            expression="SUM(b.value)",
            time_column="day",
            timezone="UTC",
            dimensions=[{"name": "category", "column": "b.category", "label": "类目"}],
            unit="件",
            created_by=owner,
        )
        session.add(version)
        session.flush()
        preference = {
            "kind": "bar",
            "title": "已保存的类目图",
            "top_n": 3,
            "measures": ["value"],
            "sort": "value_desc",
            "marks": ["max"],
            "highlight": ["类目 154"],
        }
        result = {
            "columns": ["category", "value"],
            "rows": [[f"类目 {index}", index + 1] for index in range(155)],
            "truncated": True,
            "column_roles": {
                "category": {"role": "dimension", "label": "类目"},
                "value": {"role": "measure", "label": "度量", "additive": True},
            },
            "units": {"value": {"kind": "level", "unit": "件", "additive": True}},
            "polarity": {"value": "up_good"},
        }
        answer = Answer(
            question="按类目查看度量",
            asked_by=member,
            status="answered",
            model_adapter="test",
            intent={"metric_code": metric.code, "metric_codes": [metric.code], "dimensions": ["category"]},
            sql="SELECT category, SUM(value) AS value FROM saved_source GROUP BY category",
            metric_version_ids=[version.id],
            evidence={"chart": preference, "sources": [{"asset_id": asset.id}]},
            result=result,
        )
        session.add(answer)
        session.flush()
        parent = AnalysisRun(
            user_id=member,
            question=answer.question,
            trigger="manual",
            status="succeeded",
            report={"kind": "lookup", "title": "上一轮取数", "answer_id": answer.id},
        )
        session.add(parent)
        session.flush()
        run = AnalysisRun(
            user_id=member,
            parent_run_id=parent.id,
            conversation_id=parent.conversation_id,
            question="标出最高点",
            trigger="manual",
            status="queued",
        )
        session.add(run)
        session.flush()
        yield SimpleNamespace(
            session=session,
            user=session.get(User, member),
            domain=domain,
            parent=parent,
            run=run,
            answer=answer,
            version=version,
            result=deepcopy(result),
            preference=deepcopy(preference),
            executions=executions,
        )


def _datasets(world):
    return list(
        world.session.scalars(
            select(AnalysisDataset).where(AnalysisDataset.run_id == world.run.id).order_by(AnalysisDataset.seq)
        )
    )


def test_lookup_result_is_copied_in_full_without_query_execution(lookup_world):
    world = lookup_world
    before = deepcopy(world.answer.result)

    history = context.build_followup_context(world.session, world.user, world.parent, world.run)

    [copied] = _datasets(world)
    assert copied.kind == "governed" and copied.answer_id == world.answer.id
    assert copied.columns == world.result["columns"]
    assert copied.rows == world.result["rows"] and len(copied.rows) == 155
    assert copied.truncated is True
    assert copied.meta["copied_from"] == {"run_id": world.parent.id, "ref": "answer"}
    assert copied.meta["metric_version_ids"] == [world.version.id]
    assert copied.meta["intent"] == world.answer.intent
    assert copied.meta["sql"] == world.answer.sql
    assert copied.meta["units"] == world.result["units"]
    assert copied.meta["polarity"] == world.result["polarity"]
    assert copied.meta["column_roles"] == world.result["column_roles"]
    assert copied.meta["column_labels"] == {"category": "类目", "value": "度量"}
    assert history["chart_targets"] == [
        {"dataset": "D1", "title": "上一轮取数", "preference": world.preference, "current_kind": "bar"}
    ]
    world.session.expire(world.answer, ["result"])
    assert world.answer.result == before
    assert world.executions == []


def test_rebuilding_lookup_context_is_idempotent_and_retains_source(lookup_world):
    world = lookup_world
    first = context.build_followup_context(world.session, world.user, world.parent, world.run)
    [copied] = _datasets(world)
    first_id = copied.id

    second = context.build_followup_context(world.session, world.user, world.parent, world.run)

    [copied] = _datasets(world)
    assert copied.id == first_id and copied.seq == 1
    assert first["chart_targets"] == second["chart_targets"]
    assert second["copied_datasets"] == [
        {"run_id": world.parent.id, "ref": "answer", "new_ref": "D1", "title": "上一轮取数"}
    ]
    assert world.executions == []


def test_lookup_copy_bounds_dataset_title_without_changing_original_answer(lookup_world):
    world = lookup_world
    title = "类目" * 150
    world.parent.question = title
    world.parent.report = {**world.parent.report, "title": title}
    original_answer = {
        "question": world.answer.question,
        "result": deepcopy(world.answer.result),
        "evidence": deepcopy(world.answer.evidence),
    }
    world.session.flush()

    history = context.build_followup_context(world.session, world.user, world.parent, world.run)

    [copied] = _datasets(world)
    assert copied.title and len(copied.title) <= 255
    target_title = history["chart_targets"][0]["title"]
    assert len(json.dumps(target_title, ensure_ascii=False)) <= 180
    assert target_title.endswith("…") and copied.title.startswith(target_title.removesuffix("…"))
    world.session.expire(world.parent, ["question", "report"])
    world.session.expire(world.answer, ["question", "result", "evidence"])
    assert world.parent.question == title and world.parent.report["title"] == title
    assert {
        "question": world.answer.question,
        "result": world.answer.result,
        "evidence": world.answer.evidence,
    } == original_answer
    assert world.executions == []


def test_refresh_hides_lookup_chart_after_source_permission_is_revoked(lookup_world):
    world = lookup_world
    initial = context.build_followup_context(world.session, world.user, world.parent, world.run)
    assert initial["chart_targets"] and initial["copied_datasets"]
    [copied] = _datasets(world)
    saved_rows = deepcopy(copied.rows)
    world.session.execute(
        delete(DomainMember).where(DomainMember.domain_id == world.domain.id, DomainMember.user_id == world.user.id)
    )
    world.session.flush()

    refreshed = context.refresh_followup_context(world.session, world.user, world.run)

    assert refreshed["chart_targets"] == [] and refreshed["copied_datasets"] == []
    assert copied.rows == saved_rows  # 撤权只限制投影，不销毁已保存来源。
    assert len(_datasets(world)) == 1 and world.executions == []
