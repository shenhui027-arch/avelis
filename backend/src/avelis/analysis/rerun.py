"""按当前权限与口径，为已保存的受治理数据集换窗口取数。"""

from __future__ import annotations

import time
from typing import Any

from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from avelis.analysis import dataset_access, measures
from avelis.ask import service as ask
from avelis.ask.intent import Intent, comparison_range
from avelis.governance.errors import Conflict, Forbidden, Invalid, NotFound
from avelis.i18n import Msg
from avelis.models import AnalysisDataset, AnalysisRun, AnalysisStep, User


def rerun_dataset(
    session: Session, user: User, run: AnalysisRun, dataset: AnalysisDataset, window: dict[str, Any]
) -> AnalysisDataset:
    """成功时由调用方一起提交数据集与步骤；取数失败先提交问数审计，不改报告或核验结果。"""
    run = session.scalar(
        select(AnalysisRun).where(AnalysisRun.id == run.id).with_for_update().execution_options(populate_existing=True)
    )
    if run is None or run.user_id != user.id or dataset.run_id != run.id:
        raise NotFound("分析运行不存在或不属于当前用户")
    if run.status in ("queued", "running", "waiting"):
        raise Conflict("分析正在运行或等待确认，暂不能重跑数据集")
    meta = dataset.meta or {}
    if dataset.kind != "governed" or not meta.get("intent"):
        raise Conflict("只有保存了查询意图的受治理数据集可以重跑")
    dataset_access.require_dataset_access(session, user, dataset)

    original_intent = dict(meta["intent"])
    changed = {
        **original_intent,
        "start_date": window.get("start"),
        "end_date": window.get("end"),
        "compare_start": None,
        "compare_end": None,
    }
    if changed.get("compare") == "custom":
        if not window.get("compare_start") or not window.get("compare_end"):
            raise Invalid("自定义对比需要 compare_start 与 compare_end")
        changed.update(compare_start=window["compare_start"], compare_end=window["compare_end"])
    try:
        intent = Intent.model_validate(changed)
    except ValidationError as exc:
        raise Invalid(Msg("时间窗口无效：{error}", error=exc)) from None
    if not intent.start_date or not intent.end_date or intent.start_date > intent.end_date:
        raise Invalid("请指定有效的时间窗口")
    if intent.compare in ("previous_period", "previous_year"):
        intent.compare_start, intent.compare_end = comparison_range(intent.start_date, intent.end_date, intent.compare)

    catalog = ask.visible_catalog(session, user, with_values=False)
    if any(code not in catalog.versions for code in intent.metric_codes):
        raise Forbidden("你已没有该数据的访问权限")
    started = time.monotonic()
    answer = ask.start_answer(
        session, user, f"[分析 {run.id}] 重跑 D{dataset.seq}", run.model_adapter or "analysis", None
    )
    answer.intent = intent.model_dump(mode="json")
    ask.fulfil(session, user, intent, answer, catalog)
    ask.finish_answer(session, user, answer, started)
    if answer.status != "answered":
        session.commit()
        if answer.status == "denied" and answer.error_code == "permission":
            raise Forbidden("你已没有该数据的访问权限")
        error = Invalid if answer.status == "clarify" or answer.error_code == "invalid_intent" else Conflict
        raise error(answer.message or "数据集重跑失败")

    result = answer.result
    versions = {code: catalog.versions[code] for code in intent.metric_codes}
    names = [metric.name for metric, _ in versions.values()]
    columns = list(result["columns"])
    if columns and columns[-1] == "value" and len(names) == 1:
        columns[-1] = names[0]
    if len(set(columns)) != len(columns):
        raise Conflict("查询结果的列名重复，无法作为数据集使用")
    old_range = f"{original_intent.get('start_date')}~{original_intent.get('end_date')}"
    new_range = f"{intent.start_date}~{intent.end_date}"
    before, found, after = dataset.title.partition(old_range)
    if intent.compare == "custom":
        after = after.replace(
            f"{original_intent.get('compare_start')}~{original_intent.get('compare_end')}",
            f"{intent.compare_start}~{intent.compare_end}",
            1,
        )
    title = f"{before}{new_range}{after}" if found else f"{dataset.title} {new_range}"
    seq = (session.scalar(select(func.max(AnalysisDataset.seq)).where(AnalysisDataset.run_id == run.id)) or 0) + 1
    new = AnalysisDataset(
        run_id=run.id,
        seq=seq,
        kind="governed",
        title=title[:255],
        answer_id=answer.id,
        inputs=[],
        columns=columns,
        rows=result["rows"],
        truncated=bool(result.get("truncated")),
        meta={
            "sql": answer.sql,
            "metric_version_ids": answer.metric_version_ids,
            "metric_codes": list(intent.metric_codes),
            "versions": {code: version.id for code, (_, version) in versions.items()},
            "intent": answer.intent,
            "original_intent": original_intent,
            "freshness": answer.freshness,
            "purpose": meta.get("purpose"),
            "window": {"start": str(intent.start_date), "end": str(intent.end_date)},
            "time_column": columns[0] if intent.time_grain else None,
            "units": measures.governed_units(
                columns,
                {metric.name: version for metric, version in versions.values()},
                ask.PERIOD_LABEL[intent.compare] if intent.compare else None,
            ),
            "rerun_of": {"run_id": run.id, "ref": f"D{dataset.seq}", "dataset_id": dataset.id},
            "executed_at": answer.executed_at.isoformat() if answer.executed_at else None,
            "cached": bool(
                answer.cache_hit or any(s.get("cache_hit") for s in (answer.plan or {}).get("subqueries", []))
            ),
            "closure": [],
            "root_kinds": ["governed"],
            "explore": False,
            "assumption": False,
            "partial": False,
        },
    )
    session.add(new)
    session.flush()
    step_seq = (session.scalar(select(func.max(AnalysisStep.seq)).where(AnalysisStep.run_id == run.id)) or 0) + 1
    session.add(
        AnalysisStep(
            run_id=run.id,
            seq=step_seq,
            kind="tool",
            tool="rerun",
            status="ok",
            args={"ref": f"D{dataset.seq}", "window": window},
            output={"dataset": f"D{new.seq}", "title": new.title, "row_count": len(new.rows)},
            dataset_id=new.id,
            duration_ms=int((time.monotonic() - started) * 1000),
        )
    )
    session.flush()
    return new
