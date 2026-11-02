"""按历史来源权限投影报告；只处理读取副本，不改保存的报告与核验结果。"""

from __future__ import annotations

from copy import deepcopy

from avelis.i18n import gettext

RESTRICTED_TEXT = "[无访问权限]"


def system_column_label(column: str, metric_name: str, period_label: str | None) -> str:
    """只翻译系统登记的期间与变化后缀，不改变业务指标名称或稳定列键。"""
    if period_label and column == metric_name:
        suffix = gettext("本期")
    elif period_label and column == f"{metric_name}·{period_label}":
        suffix = gettext(period_label)
    elif period_label and column == f"{metric_name}·变化":
        suffix = gettext("变化")
    elif period_label and column == f"{metric_name}·变化率":
        suffix = gettext("变化率")
    else:
        return column
    return gettext("{metric}·{period}", metric=metric_name, period=suffix)


def build_column_roles(columns: list[str], meta: dict) -> dict[str, dict[str, str]]:
    """只按登记的量纲、分组和时间列构造列契约，不读取行值或猜列名。"""
    units = meta.get("units") or {}
    grouped = set((meta.get("intent") or {}).get("group_by") or [])
    dimensions = {
        d["name"]: d.get("label") or d["name"]
        for d in (meta.get("dimensions") or (meta.get("export_labels") or {}).get("dimensions") or [])
        if d.get("name")
    }
    dimensions.update(meta.get("dimension_labels") or {})
    measures = meta.get("measure_labels") or {}
    labels = meta.get("column_labels") or {}
    roles = {}
    for column in columns:
        kind = (units.get(column) or {}).get("kind")
        if column == meta.get("time_column"):
            role = "time"
        elif column in grouped:
            role = "dimension"
        elif kind in {"level", "count", "ratio", "share"}:
            role = "measure"
        elif kind == "delta":
            role = "change"
        elif kind == "rate_change":
            role = "rate"
        elif column not in units and ("units" in meta or column in dimensions):
            role = "dimension"
        else:
            continue  # unknown 量纲没有足够信息，不能把数值列冒充维度。
        label = dimensions.get(column) if role == "dimension" else measures.get(column)
        roles[column] = {"role": role, "label": labels.get(column) or label or column}
    return roles


def dataset_meta(session, dataset) -> dict:
    """读取时补齐锁定口径的展示元数据，不改保存结果，也不改用最新版本。"""
    from avelis.analysis import measures
    from avelis.ask.service import PERIOD_LABEL
    from avelis.models import Metric, MetricVersion

    meta = dict(dataset.meta or {})
    if dataset.kind != "governed":
        return meta
    dimensions = dict(meta.get("dimension_labels") or {})
    labels = dict(meta.get("measure_labels") or {})
    column_labels = dict(meta.get("column_labels") or {})
    units = dict(meta.get("units") or {})
    polarity = dict(meta.get("polarity") or {})
    version_ids = list((meta.get("versions") or {}).values()) or meta.get("metric_version_ids") or []
    period = PERIOD_LABEL.get((meta.get("intent") or {}).get("compare"))
    for version_id in dict.fromkeys(version_ids):
        version = session.get(MetricVersion, version_id)
        metric = session.get(Metric, version.metric_id) if version else None
        if metric is None:
            continue
        for dimension in version.dimensions or []:
            name = dimension.get("name")
            if name:
                dimensions.setdefault(name, dimension.get("label") or name)
        registered = measures.governed_units(dataset.columns, {metric.name: version}, period)
        for column, unit in registered.items():
            if getattr(version, "value_kind", None) in {"level", "ratio", "count"}:
                units[column] = unit  # 旧运行可能仍登记了表达式推断值，显式声明优先。
            else:
                units.setdefault(column, unit)
            labels.setdefault(column, metric.name)
            saved_label = column_labels.get(column)
            generated_label = f"{metric.name}·本期" if period and column == metric.name else column
            if saved_label is None or saved_label == generated_label:
                column_labels[column] = system_column_label(column, metric.name, period)
        for column, direction in measures.governed_polarity(dataset.columns, {metric.name: version}, period).items():
            polarity.setdefault(column, direction)
    return {
        **meta,
        **({"units": units} if units or "units" in meta else {}),
        "polarity": polarity,
        "dimension_labels": dimensions,
        "measure_labels": labels,
        "column_labels": column_labels,
    }


def report_dimension_labels(session, report: dict, datasets: list) -> dict:
    """原因实体使用所问口径的锁定版本；版本间显示名冲突时保留维度名。"""
    from avelis.models import Metric, MetricVersion

    projected = deepcopy(report)
    known = {f"D{dataset.seq}": dataset for dataset in datasets}
    for cause in projected.get("causes") or []:
        if cause.get("restricted") or cause.get("type") != "member":
            continue
        entity = cause.get("entity") or {}
        dimension = entity.get("dimension")
        code = report.get("metric_code") or (report.get("scope") or {}).get("metric_code") or entity.get("metric_code")
        if not dimension:
            continue
        pending = [*(cause.get("evidence") or []), *record_refs(cause.get("contribution") or {})]
        if entity.get("evidence_id"):
            pending.append(entity["evidence_id"].split("#", 1)[0])
        sources, seen = [], set()
        while pending:
            ref = pending.pop()
            if ref in seen or ref not in known:
                continue
            seen.add(ref)
            dataset = known[ref]
            sources.append(dataset)
            pending.extend([*(dataset.inputs or []), *((dataset.meta or {}).get("closure") or [])])
        ids = {
            version_id
            for dataset in sources
            for metric_code, version_id in ((dataset.meta or {}).get("versions") or {}).items()
            if metric_code == code
        }
        for dataset in sources:
            meta = dataset.meta or {}
            if meta.get("versions"):
                continue
            for version_id in meta.get("metric_version_ids") or []:
                version = session.get(MetricVersion, version_id)
                metric = session.get(Metric, version.metric_id) if version else None
                if metric is not None and metric.code == code:
                    ids.add(version_id)
        labels = set()
        for version_id in ids:
            version = session.get(MetricVersion, version_id)
            if version is not None:
                labels.update(
                    d.get("label") or dimension for d in version.dimensions or [] if d.get("name") == dimension
                )
        entity["dimension_label"] = next(iter(labels)) if len(labels) == 1 else dimension
    return projected


def _ref(part: dict) -> str | None:
    if ref := part.get("ref") or part.get("dataset"):
        return ref
    if (part.get("window") or "").startswith("dataset:"):
        return part["window"].split(":", 1)[1]
    # 渲染失败的段没有 ref，但 path 仍指向来源，错误中可能带原值。
    path = part.get("path") or ""
    return path.removeprefix("window.dataset:").split(".", 1)[0] or None


def record_refs(record: dict) -> set[str]:
    """从保存的引用片段推导依赖，兼容没有 refs 的字段和只有 refs 的旧结论。"""
    refs = set(record.get("refs") or [])
    for part in [*(record.get("parts") or []), *(record.get("segments") or [])]:
        if isinstance(part, dict) and (ref := _ref(part)):
            refs.add(ref)
    return refs


def _hide(record: dict) -> None:
    kept = {k: record[k] for k in ("type", "refs", "explicit_refs", "status") if k in record}
    record.clear()
    record.update(
        kept,
        text=RESTRICTED_TEXT,
        parts=[RESTRICTED_TEXT],
        segments=[{"kind": "restricted", "text": RESTRICTED_TEXT}],
        errors=[],
        restricted=True,
    )


def _record(record: dict, restricted_refs: set[str]) -> bool:
    if not record_refs(record) & restricted_refs:
        return False
    parts, segments = record.get("parts") or [], record.get("segments") or []
    hidden_parts = {i for i, part in enumerate(parts, 1) if isinstance(part, dict) and _ref(part) in restricted_refs}
    hidden = [i for i, seg in enumerate(segments) if _ref(seg) in restricted_refs or seg.get("part") in hidden_parts]
    if not hidden:
        _hide(record)  # 旧报告缺少可拆分的片段，只能遮蔽整个字段。
        return True
    for i in hidden:
        segment = segments[i]
        ref = _ref(segment)
        hidden_parts.add(segment.get("part"))
        segments[i] = {"kind": "restricted", "part": segment.get("part"), "ref": ref, "text": RESTRICTED_TEXT}
    record.update(
        text="".join(seg.get("text") or "" for seg in segments), segments=segments, errors=[], restricted=True
    )
    if "parts" in record:
        record["parts"] = [RESTRICTED_TEXT if i in hidden_parts else part for i, part in enumerate(parts, 1)]
    return True


def restrict_report(report: dict, restricted_refs: set[str], *, lookup_restricted: bool = False) -> dict:
    """遮蔽受限来源的引用、原值、选行条件与已渲染正文，保留其他来源的片段。"""
    if lookup_restricted:
        return {k: report[k] for k in ("kind", "answer_id", "title") if k in report} | {
            "summary": RESTRICTED_TEXT,
            "restricted": True,
        }
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
        refs = {
            *evidence,
            (entity.get("evidence_id") or "").split("#", 1)[0],
            _ref(contribution),
            *(_ref(seg) for seg in contribution.get("segments") or []),
        }
        if refs & restricted_refs:
            projected["causes"][i] = {
                "type": cause.get("type") or raw.get("type"),
                "evidence": evidence,
                "entity": {},
                "label": RESTRICTED_TEXT,
                "contribution": None,
                "restricted": True,
            }
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
    records = [*fields.values(), *(projected_report.get("claims") or []), *(projected_report.get("causes") or [])]
    if (projected.get("conclusion") or {}).get("problems") and (
        projected_report.get("restricted")
        or any(
            record.get("restricted") or any(d.get("restricted") for d in (record.get("details") or {}).values())
            for record in records
        )
    ):
        projected["conclusion"]["problems"] = [RESTRICTED_TEXT] * len(projected["conclusion"]["problems"])
    return projected
