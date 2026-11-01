"""续问的结构化历史与本轮可引用的数据副本。"""

from __future__ import annotations

import json
from copy import deepcopy

from sqlalchemy import select
from sqlalchemy.orm import Session

from avelis.analysis.dataset_access import can_read_dataset
from avelis.analysis.dataset_projection import RESTRICTED_TEXT, record_refs, restrict_report
from avelis.models import AnalysisDataset, AnalysisRun, Answer, User

CONTEXT_CHARS = 24_000
MAX_TURNS = 12
MAX_DATASETS = 24
_REF_FIELDS = {"dataset", "ref", "evidence_id", "inputs", "closure", "refs", "evidence"}
_PROVENANCE = {"copied_from", "rerun_of"}
_LITERAL_FIELDS = {"match", "exclude", "filters", "intent", "original_intent", "units", "summary_units"}


def _text(value, limit: int) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    if len(json.dumps(text, ensure_ascii=False)) <= limit:
        return text
    start, end = 0, min(len(text), limit)
    while start < end:
        middle = (start + end + 1) // 2
        if len(json.dumps(text[:middle] + "…", ensure_ascii=False)) <= limit:
            start = middle
        else:
            end = middle - 1
    return text[:start] + "…"


def _length(value) -> int:
    return len(json.dumps(value, ensure_ascii=False))


def _fit_latest(result: dict) -> None:
    """先减少可重新查看的目录与早期细节，始终保留最近一轮的问答摘要。"""
    for field in ("copied_datasets", "earlier_turns"):
        while _length(result) > CONTEXT_CHARS and result[field]:
            result[field].pop()
            if field == "copied_datasets":
                result["omitted_datasets"] += 1
    for field in ("datasets", "claims", "causes"):
        while _length(result) > CONTEXT_CHARS and result["turns"][0][field]:
            result["turns"][0][field].pop()


def _remap(value, refs: dict[str, str], field: str | None = None):
    """只改结构化引用；SQL、列名与成员值中恰好出现 Dn 的文字保持原样。"""
    if isinstance(value, dict):
        return {
            key: deepcopy(item) if key in _PROVENANCE | _LITERAL_FIELDS else _remap(item, refs, key)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_remap(item, refs, field) for item in value]
    if isinstance(value, str) and field in _REF_FIELDS:
        ref, sep, suffix = value.partition("#")
        return refs.get(ref, ref) + (sep + suffix if sep else "")
    return deepcopy(value)


def _datasets(session: Session, run_id: str) -> dict[str, AnalysisDataset]:
    return {
        f"D{dataset.seq}": dataset
        for dataset in session.scalars(
            select(AnalysisDataset).where(AnalysisDataset.run_id == run_id).order_by(AnalysisDataset.seq)
        )
    }


def _copy_datasets(session: Session, user: User, parent: AnalysisRun, run: AnalysisRun) -> None:
    previous, existing = _datasets(session, parent.id), _datasets(session, run.id)
    readable = {ref: dataset for ref, dataset in previous.items() if can_read_dataset(session, user, dataset, previous)}
    copied = {
        (dataset.meta or {}).get("copied_from", {}).get("ref"): ref
        for ref, dataset in existing.items()
        if (dataset.meta or {}).get("copied_from", {}).get("run_id") == parent.id
    }
    seq = max((dataset.seq for dataset in existing.values()), default=0)
    refs = dict(copied)
    for ref in readable:
        if ref not in refs:
            seq += 1
            refs[ref] = f"D{seq}"
    for ref, dataset in readable.items():
        if ref in copied:
            continue
        meta = _remap(dataset.meta or {}, refs)
        meta["copied_from"] = {"run_id": parent.id, "ref": ref}
        rows = deepcopy(dataset.rows)
        args = (dataset.meta or {}).get("args") or {}
        if (
            dataset.kind == "calc"
            and "values" in args
            and "formula" in args
            and dataset.columns == ["name", "dataset", "column", "rows", "value"]
        ):
            index = dataset.columns.index("dataset")
            for row in rows:
                row[index] = refs.get(row[index], row[index])
        session.add(
            AnalysisDataset(
                run_id=run.id,
                seq=int(refs[ref][1:]),
                kind=dataset.kind,
                title=dataset.title,
                answer_id=dataset.answer_id,
                inputs=[refs[item] for item in dataset.inputs or []],
                columns=deepcopy(dataset.columns),
                rows=rows,
                truncated=dataset.truncated,
                meta=meta,
            )
        )
    session.flush()


def _project_report(
    session: Session, user: User, run: AnalysisRun, datasets: dict[str, AnalysisDataset]
) -> tuple[dict, set[str]]:
    restricted = {ref for ref, dataset in datasets.items() if not can_read_dataset(session, user, dataset, datasets)}
    raw = run.report or {}
    lookup_restricted = False
    if raw.get("kind") == "lookup":
        answer = session.get(Answer, raw.get("answer_id"))
        lookup_restricted = answer is None or not can_read_dataset(
            session,
            user,
            AnalysisDataset(
                kind="governed", answer_id=answer.id, meta={"metric_version_ids": answer.metric_version_ids}
            ),
        )
    report = deepcopy(restrict_report(raw, restricted, lookup_restricted=lookup_restricted))
    if restricted:
        # 旧报告没有片段依赖，无法证明正文来自可读数据时不沿用原值。
        for name in ("title", "summary", "unexplained"):
            if name in report and not (raw.get("fields") or {}).get(name):
                report[name] = RESTRICTED_TEXT
        for claim in report.get("claims") or []:
            if not record_refs(claim):
                claim.clear()
                claim.update(text=RESTRICTED_TEXT, restricted=True)
    return report, restricted


def _context(session: Session, user: User, parent: AnalysisRun, run: AnalysisRun) -> dict:
    conversation_id = parent.conversation_id or parent.id
    query = select(AnalysisRun).where(AnalysisRun.conversation_id == conversation_id, AnalysisRun.id != run.id)
    if run.created_at is not None:
        query = query.where(AnalysisRun.created_at <= run.created_at)
    turns = list(
        session.scalars(query.order_by(AnalysisRun.parent_run_id.is_not(None), AnalysisRun.created_at, AnalysisRun.id))
    )
    if not any(previous.id == parent.id for previous in turns):
        turns.append(parent)
    saved: dict[str, dict[str, AnalysisDataset]] = {}

    def datasets_for(run_id):
        if run_id not in saved:
            saved[run_id] = _datasets(session, run_id)
        return saved[run_id]

    current = _datasets(session, run.id)
    available: dict[tuple[str, str], str] = {}
    copied = []
    for ref, dataset in current.items():
        source = (dataset.meta or {}).get("copied_from")
        if not source or not can_read_dataset(session, user, dataset, current):
            continue
        copied.append(
            {"run_id": source["run_id"], "ref": source["ref"], "new_ref": ref, "title": _text(dataset.title, 120)}
        )
        seen = set()
        while source and (key := (source["run_id"], source["ref"])) not in seen:
            seen.add(key)
            available[key] = ref
            original = datasets_for(source["run_id"]).get(source["ref"])
            source = (original.meta or {}).get("copied_from") if original else None
    result = {
        "id": conversation_id,
        "previous_turns": len(turns),
        "turns": [],
        "earlier_turns": [],
        "copied_datasets": copied[:MAX_DATASETS],
        "omitted_datasets": max(0, len(copied) - MAX_DATASETS),
        "omitted_turns": max(0, len(turns) - MAX_TURNS),
    }
    for number, previous in list(enumerate(turns, 1))[:-MAX_TURNS][:6]:
        report, _ = _project_report(session, user, previous, datasets_for(previous.id))
        result["earlier_turns"].append(
            {
                "run_id": previous.id,
                "turn": number,
                "question": _text(
                    ((previous.checkpoint or {}).get("inherited") or {}).get("user_text") or previous.question, 120
                ),
                "title": _text(report.get("title") or "", 100),
                "summary": _text(report.get("summary") or "", 160),
            }
        )
    selected = list(enumerate(turns, 1))[-MAX_TURNS:]
    for number, previous in reversed(selected):
        known = datasets_for(previous.id)
        report, restricted = _project_report(session, user, previous, known)

        def references(refs):
            return [
                {
                    "run_id": previous.id,
                    "ref": ref,
                    **({"copied_ref": available[(previous.id, ref)]} if (previous.id, ref) in available else {}),
                    "title": _text(known[ref].title, 100),
                }
                for ref in sorted(refs)
                if ref in known and ref not in restricted
            ][:4]

        causes = [
            {
                "label": _text(cause.get("label") or "", 160),
                "contribution": _text(cause.get("contribution"), 240),
                "datasets": references(cause.get("evidence") or []),
            }
            for cause in (report.get("causes") or [])[:4]
            if not cause.get("restricted")
        ]
        claims = [
            {
                "type": claim.get("type"),
                "text": _text(claim.get("text") or "", 320),
                "datasets": references(record_refs(claim)),
            }
            for claim in (report.get("claims") or [])[:5]
            if not claim.get("restricted")
        ]
        turn = {
            "run_id": previous.id,
            "turn": number,
            "question": _text(
                ((previous.checkpoint or {}).get("inherited") or {}).get("user_text") or previous.question, 600
            ),
            "title": _text(report.get("title") or "", 180),
            "summary": _text(report.get("summary") or "", 700),
            "causes": causes,
            "unexplained": _text(report.get("unexplained") or "", 400),
            "claims": claims,
            "datasets": references(known)[:8],
        }
        result["turns"].insert(0, turn)
        if len(result["turns"]) == 1:
            _fit_latest(result)
        elif _length(result) > CONTEXT_CHARS:
            result["turns"].pop(0)
            result["omitted_turns"] += len(selected) - len(result["turns"])
            break
    return result


def build_followup_context(session: Session, user: User, parent: AnalysisRun, new_run: AnalysisRun) -> dict:
    """续问创建事务中复制上一轮可读数据，并生成有界的同对话历史。"""
    _copy_datasets(session, user, parent, new_run)
    return _context(session, user, parent, new_run)


def refresh_followup_context(session: Session, user: User, run: AnalysisRun) -> dict:
    """执行和确认恢复时按当前权限重建摘要，不再复制数据。"""
    return _context(session, user, session.get(AnalysisRun, run.parent_run_id), run)


def load_followup_datasets(toolbox) -> None:
    """新工具箱注册当前仍可读的副本；暂停恢复使用原有 snapshot。"""
    datasets = _datasets(toolbox.session, toolbox.run.id)
    toolbox._dataset_seq_floor = max((dataset.seq for dataset in datasets.values()), default=0)
    for ref, dataset in datasets.items():
        if (dataset.meta or {}).get("copied_from") and can_read_dataset(
            toolbox.session, toolbox.user, dataset, datasets
        ):
            toolbox._datasets[ref] = dataset
