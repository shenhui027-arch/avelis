"""按历史来源权限投影报告；只处理读取副本，不改保存的报告与核验结果。"""

from __future__ import annotations

from copy import deepcopy

RESTRICTED_TEXT = "[无访问权限]"


def _ref(part: dict) -> str | None:
    if ref := part.get("ref") or part.get("dataset"):
        return ref
    if (part.get("window") or "").startswith("dataset:"):
        return part["window"].split(":", 1)[1]
    # 渲染失败的段没有 ref，但 path 仍指向来源，错误中可能带原值。
    path = part.get("path") or ""
    return path.removeprefix("window.dataset:").split(".", 1)[0] or None


def _hide(record: dict) -> None:
    kept = {k: record[k] for k in ("type", "refs", "explicit_refs", "status") if k in record}
    record.clear()
    record.update(kept, text=RESTRICTED_TEXT, parts=[RESTRICTED_TEXT],
                  segments=[{"kind": "restricted", "text": RESTRICTED_TEXT}], errors=[], restricted=True)


def _record(record: dict, restricted_refs: set[str]) -> bool:
    parts, segments = record.get("parts") or [], record.get("segments") or []
    hidden_parts = {i for i, part in enumerate(parts, 1)
                    if isinstance(part, dict) and _ref(part) in restricted_refs}
    hidden = [i for i, seg in enumerate(segments)
              if _ref(seg) in restricted_refs or seg.get("part") in hidden_parts]
    if not hidden:
        if hidden_parts or set(record.get("refs") or []) & restricted_refs:
            _hide(record)  # 旧报告只有显式 refs，不能从正文里拆出各来源。
            return True
        return False
    for i in hidden:
        segment = segments[i]
        ref = _ref(segment)
        hidden_parts.add(segment.get("part"))
        segments[i] = {"kind": "restricted", "part": segment.get("part"),
                       "ref": ref, "text": RESTRICTED_TEXT}
    record.update(text="".join(seg.get("text") or "" for seg in segments), segments=segments,
                  errors=[], restricted=True)
    if "parts" in record:
        record["parts"] = [RESTRICTED_TEXT if i in hidden_parts else part for i, part in enumerate(parts, 1)]
    return True


def restrict_report(report: dict, restricted_refs: set[str], *, lookup_restricted: bool = False) -> dict:
    """遮蔽受限来源的引用、原值、选行条件与已渲染正文，保留其他来源的片段。"""
    if lookup_restricted:
        return {k: report[k] for k in ("kind", "answer_id", "title") if k in report} | {
            "summary": RESTRICTED_TEXT, "restricted": True}
    if not restricted_refs:
        return report
    projected = deepcopy(report)
    for name, record in (projected.get("fields") or {}).items():
        if _record(record, restricted_refs):
            projected[name] = record["text"]
    for claim in projected.get("claims") or []:
        details = claim.get("details") or {}
        premise, validation = claim.get("premise"), claim.get("validation")
        _record(claim, restricted_refs)
        if "details" not in claim and details:
            for record in details.values():
                _hide(record)
            claim["details"] = details
            claim["premise"] = RESTRICTED_TEXT if premise else None
            claim["validation"] = RESTRICTED_TEXT if validation else None
        else:
            for name, record in details.items():
                if _record(record, restricted_refs):
                    claim[name] = record["text"]
    for i, cause in enumerate(projected.get("causes") or []):
        raw = cause.get("raw") or {}
        evidence = cause.get("evidence") or raw.get("evidence") or []
        entity = cause.get("entity") or raw.get("entity") or {}
        contribution = cause.get("contribution") or raw.get("contribution") or {}
        refs = {*evidence, (entity.get("evidence_id") or "").split("#", 1)[0], _ref(contribution),
                *(_ref(seg) for seg in contribution.get("segments") or [])}
        if refs & restricted_refs:
            projected["causes"][i] = {"type": cause.get("type") or raw.get("type"), "evidence": evidence,
                                       "entity": {}, "label": RESTRICTED_TEXT, "contribution": None,
                                       "restricted": True}
    return projected


def restrict_verification(verification: dict, projected_report: dict) -> dict:
    """核验错误可能带原值；仅遮蔽受限报告记录的错误文案，保留原核验状态与计数。"""
    projected = deepcopy(verification)
    fields = projected_report.get("fields") or {}
    for item in projected.get("fields") or []:
        if (fields.get(item["field"]) or {}).get("restricted"):
            item["problems"] = []
    for key in ("claims", "causes"):
        records = projected_report.get(key) or []
        for item in projected.get(key) or []:
            if item["index"] >= len(records):
                continue
            record = records[item["index"]]
            if record.get("restricted") or any(d.get("restricted") for d in (record.get("details") or {}).values()):
                item["problems"] = []
    return projected
