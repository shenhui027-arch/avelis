"""报告数字的引用片段（ADR 0009 第 1 点）：模型写片段列表，后端按数据集取值、格式化后生成正文。

报告的所有模型字段（标题、摘要、未解释、结论、premise、validation）都写成片段列表
parts: list[str | NumberRef | LabelRef | CountRef | WindowRef]（参照 DB-GPT 报告规格 ReportPart = str | ReportReference）。
不在正文里嵌入内联占位语法，因此不需要转义，结构由 pydantic 与工具的 JSON schema 校验；取值时的语义错误
（数据集、列不存在，没有恰好选中一行，量纲不允许所用的格式，方向与正负不一致等）在渲染时逐个片段记下，反馈给模型。

量纲（kind）与单位：数据集的数值列在 meta.units 里登记 {kind, unit, scale, of_ratio}；计算工具的汇总路径在
meta.summary_units 里登记。格式只能用在相应的量纲上，单位一律由格式器输出（文字片段里不能写单位）。

引用的定义、取值、格式化与渲染集中在本模块，细节调整只改这里。
"""

from __future__ import annotations

import re
from datetime import date
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

REF_PATTERN = r"^D[1-9]\d{0,3}$"
KINDS = ("level", "delta", "ratio", "rate_change", "share", "count", "unknown")


class _Ref(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NumberRef(_Ref):
    """数据集里的一个数值单元格（column）或计算工具登记的汇总值（summary）。"""
    dataset: str = Field(pattern=REF_PATTERN, description="数据集编号，如 D5")
    column: str | None = Field(None, description="数值列名；与 summary 二选一")
    summary: str | None = Field(None, description="计算类数据集登记的汇总路径，如 value、total.change、day_count_effect")
    match: dict[str, str | int | float] | None = Field(
        None, description="选行条件，如 {'<维度>': '<成员>'}，必须恰好选中一行（不做隐式求和，求和先用 derive）")
    row: int | None = Field(None, ge=1, description="第几行（从 1 开始），与 match 二选一；只用来定位，不代表排名")
    precision: int | None = Field(None, ge=0, le=4, description="可选：小数位数（0–4）；显示格式与单位由系统按量纲决定")
    direction: Literal["up", "down"] | None = Field(
        None, description="变化的方向（水平值以外的任何值都可以写）：写了就核对正负并显示绝对值；不写时显示带符号的值")
    role: Literal["fact", "proposal"] = Field(
        "fact", description="proposal：建议的目标值（非观测值），只能用在推断、建议类结论中")


class LabelRef(_Ref):
    """维度列的文本值，例如名称里带数字的成员；只能取维度列。用 label 写列名（与 NumberRef 的 column 区分）。"""
    dataset: str = Field(pattern=REF_PATTERN)
    label: str = Field(description="维度列名")
    match: dict[str, str | int | float] | None = None
    row: int | None = Field(None, ge=1)


class CountRef(_Ref):
    """某个维度列的不同值个数（“N 个成员”）；不计 NULL 和空串；截断的数据集不可用。"""
    dataset: str = Field(pattern=REF_PATTERN)
    distinct: str = Field(description="维度列名")


class WindowRef(_Ref):
    """本次分析的本期、对比期，或某个受治理查询数据集的查询窗口。"""
    window: str = Field(pattern=r"^(run_current|run_compare|dataset:D[1-9]\d{0,3})$",
                        description="run_current、run_compare 或 dataset:D5")
    format: Literal["range", "month"] = Field("range", description="range 显示日期区间；month 显示“2026 年 9 月”（只用于完整的单月）")


Ref = NumberRef | LabelRef | CountRef | WindowRef
Part = str | NumberRef | LabelRef | CountRef | WindowRef
CONCLUSIONS = ("ranked_reasons", "no_material_change", "insufficient_evidence")
DATASET_REF = r"^D[1-9]\d{0,3}$"


class CauseEntity(_Ref):
    """原因的实体按编码关联（ADR 0009 第 7 点），不靠显示名对齐；界面另显示名称：
    成员 {metric_code, dimension, member}；因子 {factor_metric_code}；证据行 {evidence_id}（D3#2：D3 的第 2 行，
    治理事件必须用它，没有口径编码的成员、因子也用它）。"""
    metric_code: str | None = Field(None, description="成员类原因：被解释的指标编码")
    dimension: str | None = Field(None, description="成员类原因：维度名（口径里的维度名，与证据中的维度列或 dimension 列一致）")
    member: str | None = Field(None, description="成员类原因：成员取值")
    factor_metric_code: str | None = Field(None, description="因子类原因：因子的指标编码（因子拆解结果的 factor_metric_code 列）")
    evidence_id: str | None = Field(None, pattern=r"^D[1-9]\d{0,3}#[1-9]\d{0,4}$",
                                    description="证据行：数据集编号#行号，如 D3#2")


class Cause(_Ref):
    """结构化的原因项（ADR 0009 第 5 点）：按影响大小排序，第一项是首要原因。"""
    type: Literal["member", "factor", "calendar", "governance"]
    entity: CauseEntity
    direction: Literal["up", "down"] = Field(description="这个原因让指标上升还是下降")
    contribution: NumberRef | None = Field(None, description="贡献的数值引用（成员、因子、日历类必填），如 explain_change 结果的 contribution 列")
    evidence: list[str] = Field(min_length=1, description="证据数据集编号；事实类原因必须是受治理查询、计算或探索结果，"
                                                          "治理事件必须是 changes / data_health 的上下文证据")


class RefError(ValueError):
    """引用无法解析。消息直接反馈给模型。"""


def as_part(part: Any) -> Part:
    """存储与工具调用传来的片段（dict 或模型实例）还原成模型；结构不合法时抛 RefError。"""
    if isinstance(part, (str, NumberRef, LabelRef, CountRef, WindowRef)):
        return part
    if not isinstance(part, dict):
        raise RefError(f"片段应为文字或引用对象，收到 {type(part).__name__}")
    model = (WindowRef if "window" in part else CountRef if "distinct" in part else
             LabelRef if "label" in part else NumberRef)
    try:
        return model.model_validate(part)
    except ValidationError as e:
        err = e.errors()[0]
        where = ".".join(str(x) for x in err.get("loc", ())) or "片段"
        raise RefError(f"引用 {part} 结构不合法（{where}：{err['msg']}）") from None


def describe(ref: Ref) -> str:
    """引用的简短写法：D5.<指标>[<维度>=<成员>]、D3.<维度>[#1]、D12.summary.value、D5.distinct(<维度>)、window.run_current。"""
    if isinstance(ref, WindowRef):
        return f"window.{ref.window}"
    if isinstance(ref, CountRef):
        return f"{ref.dataset}.distinct({ref.distinct})"
    path = (ref.label if isinstance(ref, LabelRef) else
            f"summary.{ref.summary}" if ref.summary else (ref.column or ""))
    if ref.match:
        path += "[" + ",".join(f"{k}={v}" for k, v in ref.match.items()) + "]"
    elif ref.row:
        path += f"[#{ref.row}]"
    return f"{ref.dataset}.{path}"


def source_label(ref: Ref) -> str:
    """界面在数字旁显示的来源标签：D5 · <指标> · <维度>=<成员>。"""
    if isinstance(ref, WindowRef):
        return {"run_current": "本期窗口", "run_compare": "对比期窗口"}.get(ref.window, ref.window.replace(":", " · "))
    if isinstance(ref, CountRef):
        return f"{ref.dataset} · {ref.distinct} 不同值个数"
    bits = [ref.dataset, ref.label if isinstance(ref, LabelRef) else
            f"汇总 {ref.summary}" if ref.summary else ref.column]
    if ref.match:
        bits.append("，".join(f"{k}={v}" for k, v in ref.match.items()))
    elif ref.row:
        bits.append(f"第 {ref.row} 行")
    return " · ".join(str(b) for b in bits if b)


# --- 取值 ------------------------------------------------------------------------------

def number(value: Any) -> Decimal | None:
    """有限的数值；字符串数字可以，NaN、Infinity、文字都不是。"""
    if value is None or isinstance(value, bool):
        return None
    try:
        d = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None
    return d if d.is_finite() else None


def _key(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _columns(ds: dict) -> list:
    rows = ds.get("rows") or []
    return list(ds.get("columns") or (rows[0].keys() if rows else []))


def _dataset(ref: Ref, datasets: dict[str, dict]) -> dict:
    ds = datasets.get(ref.dataset)
    if ds is None:
        raise RefError(f"数据集 {ref.dataset} 不存在")
    return ds


def _select(ref: NumberRef | LabelRef, ds: dict) -> dict:
    rows, columns = ds.get("rows") or [], _columns(ds)
    if ref.match is not None and ref.row is not None:
        raise RefError("match 与 row 只能写一个")
    if ref.row is not None:
        if ref.row > len(rows):
            raise RefError(f"{ref.dataset} 只有 {len(rows)} 行")
        return rows[ref.row - 1]
    if not ref.match:
        if len(rows) != 1:
            raise RefError(f"{ref.dataset} 有 {len(rows)} 行，请用 match 或 row 选定一行")
        return rows[0]
    unknown = [c for c in ref.match if c not in columns]
    if unknown:
        raise RefError(f"{ref.dataset} 没有列 {'、'.join(unknown)}（可用：{'、'.join(map(str, columns))}）")
    hits = [r for r in rows if all(_equal(r.get(c), v) for c, v in ref.match.items())]
    if len(hits) != 1:
        raise RefError(f"条件选中了 {len(hits)} 行，必须恰好一行（求和请先用 derive）")
    return hits[0]


def _equal(cell: Any, value: Any) -> bool:
    if _key(cell) == _key(value):
        return True
    a, b = number(cell), number(value)
    return a is not None and b is not None and a == b


def _unit(meta: dict | None) -> dict:
    meta = dict(meta or {})
    kind = meta.get("kind") if meta.get("kind") in KINDS else "unknown"
    return {"kind": kind, "unit": meta.get("unit") or "", "scale": number(meta.get("scale")) or Decimal(1),
            "of_ratio": bool(meta.get("of_ratio"))}


def _summary_value(summary: Any, path: str) -> Any:
    node = summary
    for key in path.split("."):
        if not isinstance(node, dict) or key not in node:
            raise KeyError(path)
        node = node[key]
    return node


def resolve(ref: Ref, datasets: dict[str, dict], window: dict | None) -> tuple[Any, dict]:
    """引用的原值与量纲。datasets：编号 → {kind, columns, rows（list[dict]）, summary, truncated, units,
    summary_units, window, explore, assumption}。"""
    if isinstance(ref, WindowRef):
        return _window(ref, datasets, window or {}), {"kind": "window"}
    ds = _dataset(ref, datasets)
    if isinstance(ref, CountRef):
        if ds.get("truncated"):
            raise RefError(f"{ref.dataset} 已截断，不是全集，不能取不同值个数")
        if ref.distinct not in _columns(ds):
            raise RefError(f"{ref.dataset} 没有列 {ref.distinct}（可用：{'、'.join(map(str, _columns(ds)))}）")
        values = {_key(r.get(ref.distinct)) for r in ds.get("rows") or []} - {""}
        return Decimal(len(values)), _unit({"kind": "count"})
    if isinstance(ref, LabelRef):
        if ref.label not in _columns(ds):
            raise RefError(f"{ref.dataset} 没有列 {ref.label}（可用：{'、'.join(map(str, _columns(ds)))}）")
        if ref.label in (ds.get("units") or {}):
            raise RefError(f"{ref.label} 是数值列，不是维度列：数值请用 NumberRef")
        evidence = (ds.get("column_evidence") or {}).get(ref.label)
        if evidence is not None and not evidence.get("label_ok"):
            raise RefError(f"{ref.label} 不是维度列的直接投影（经过函数、转换或拼接），不能当作维度文本引用")
        value = _select(ref, ds)[ref.label]
        if number(value) is not None and not isinstance(value, str):
            raise RefError(f"{ref.label} 的值 {value} 是数值，不是维度文本：数值请用 NumberRef")
        return value, {"kind": "label"}
    if (ref.column is None) == (ref.summary is None):
        raise RefError("column 与 summary 必须且只能写一个")
    if ref.summary is not None:
        if ref.match is not None or ref.row is not None:
            raise RefError("汇总值不能带 match 或 row")
        registered = ds.get("summary_units") or {}
        if ref.summary not in registered:
            raise RefError(f"{ref.dataset} 没有登记汇总路径 {ref.summary}（可用：{'、'.join(registered) or '无'}）")
        try:
            raw = _summary_value(ds.get("summary") or {}, ref.summary)
        except KeyError:
            raise RefError(f"{ref.dataset} 的汇总里没有 {ref.summary}") from None
        meta = _unit(registered[ref.summary])
    else:
        if ref.column not in _columns(ds):
            raise RefError(f"{ref.dataset} 没有列 {ref.column}（可用：{'、'.join(map(str, _columns(ds)))}）")
        raw = _select(ref, ds)[ref.column]
        meta = _unit((ds.get("units") or {}).get(ref.column))
        evidence = (ds.get("column_evidence") or {}).get(ref.column)
        if evidence is not None and not evidence.get("eligible"):  # 探索结果中不能作事实来源的列：按假设计算处理
            meta["column_assumption"] = True
    if number(raw) is None:
        raise RefError(f"原值“{_key(raw)}”不是有限数值：文本请用 LabelRef，个数请用 CountRef")
    return raw, meta


def _window(ref: WindowRef, datasets: dict[str, dict], window: dict) -> tuple[date, date]:
    if ref.window.startswith("dataset:"):
        code = ref.window.split(":", 1)[1]
        ds = datasets.get(code)
        if ds is None:
            raise RefError(f"数据集 {code} 不存在")
        span = ds.get("window") or {}
        start, end = span.get("start"), span.get("end")
        if not start or not end:
            raise RefError(f"{code} 不是带查询窗口的受治理查询数据集")
    else:
        keys = ("start", "end") if ref.window == "run_current" else ("compare_start", "compare_end")
        start, end = (window.get(k) for k in keys)
        if not start or not end:
            raise RefError(f"本次分析没有{'本期' if ref.window == 'run_current' else '对比期'}窗口")
    return date.fromisoformat(str(start)), date.fromisoformat(str(end))


# --- 格式化 ----------------------------------------------------------------------------

LEVELS = ("level", "ratio")  # 水平值：不写方向（“下降至 X”里的 X 不做方向检查）
WAN, YI = Decimal(10000), Decimal(100000000)


def _plain(d: Decimal) -> str:
    text = format(d, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _rounded(v: Decimal, places: int, *, strip: bool = False, group: bool = False) -> str:
    """ROUND_HALF_UP 到 places 位；strip 去掉多余的零；group 加千分位。"""
    q = v.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)
    text = f"{q:,.{places}f}" if group else format(q, "f")
    return text.rstrip("0").rstrip(".") if strip and "." in text else text


def fmt(ref: Ref, raw: Any, meta: dict) -> str:
    """按量纲与格式把原值变成显示文字（四舍五入 ROUND_HALF_UP），单位由这里输出。"""
    if isinstance(ref, WindowRef):
        start, end = raw
        if ref.format == "month":
            last = date(end.year + end.month // 12, end.month % 12 + 1, 1).toordinal() - 1
            if start.day != 1 or (start.year, start.month) != (end.year, end.month) or end.toordinal() != last:
                raise RefError(f"{start} ~ {end} 不是一个完整的单月，不能用 month 格式")
            return f"{start.year} 年 {start.month} 月"
        return f"{start} ~ {end}"
    if isinstance(ref, LabelRef):
        return _key(raw)
    value = number(raw)
    if isinstance(ref, CountRef):
        return _plain(value)
    return display(value, meta, ref.precision, ref.direction, _key(raw))


def display(value: Decimal, meta: dict, precision: int | None = None, direction: str | None = None,
            raw: str = "") -> str:
    """系统按量纲决定显示格式（ADR 0009 第 1 点“量纲与单位”）：模型不选格式，只能指定小数位。
    - level、delta、count（有口径单位）：先按 unit_scale 换算回基本单位，绝对值 ≥ 1 亿用“亿”、≥ 1 万用“万”（2 位），
      否则原值；后接口径单位；没有单位时按 unknown 显示；
    - ratio：无单位时 ×100 带 %（如转化率），有单位时原值加单位（如 元/单）；
    - rate_change、share：×100 带 %；ratio 的差（of_ratio，且无单位）：×100 带“个百分点”；
    - unknown：原值、千分位、不带单位。
    direction：水平值（level、ratio）以外都可以写，核对正负后显示绝对值；不写时差额、变化率带符号。"""
    kind, unit, sign = meta["kind"], meta["unit"], ""
    if direction:
        if kind in LEVELS:
            raise RefError(f"量纲为 {kind} 的水平值不能标方向（变化、差额、变化率等才写 direction）")
        if value == 0 or (value > 0) != (direction == "up"):
            raise RefError(f"方向与正负不一致：direction={direction}，原值 {raw or _plain(value)}")
        value = abs(value)
    elif kind in ("delta", "rate_change") and value > 0:
        sign = "+"  # 没写方向：显示带符号的值
    fixed = precision is not None

    def places(default: int) -> int:
        return precision if fixed else default

    if kind in ("rate_change", "share") or (kind == "ratio" and not unit):
        return sign + _rounded(value * 100, places(1)) + "%"
    if kind == "delta" and meta["of_ratio"] and not unit:
        return sign + _rounded(value * 100, places(1)) + "个百分点"
    if kind in ("level", "delta", "count") and unit:
        base = value * meta["scale"]
        if abs(base) >= YI:
            return sign + _rounded(base / YI, places(2)) + "亿" + unit
        if abs(base) >= WAN:
            return sign + _rounded(base / WAN, places(2)) + "万" + unit
        return sign + _rounded(base, places(0 if kind == "count" else 2), strip=not fixed) + unit
    if kind == "ratio":
        return sign + _rounded(value, places(2), strip=not fixed) + unit
    return sign + _rounded(value * meta["scale"], places(0 if kind == "count" else 4), strip=not fixed, group=True)


# --- 渲染 ------------------------------------------------------------------------------

def render(parts: Any, datasets: dict[str, dict], window: dict | None) -> dict[str, Any]:
    """渲染片段列表：text（正文）、segments（文字段与引用段，每段带 part 序号）、errors（每个解析失败的片段一条，
    写明第几个片段）、refs（引用涉及的数据集）。解析失败的引用在正文里显示为“[?]”。"""
    if parts is None:
        parts = []
    elif isinstance(parts, str):
        parts = [parts]
    segments: list[dict[str, Any]] = []
    errors: list[str] = []
    refs: list[str] = []
    for index, item in enumerate(parts, start=1):
        try:
            part = as_part(item)
        except RefError as e:
            errors.append(f"第 {index} 个片段：{e}")
            segments.append({"kind": "error", "part": index, "text": "[?]", "error": str(e)})
            continue
        if isinstance(part, str):
            segments.append({"kind": "text", "part": index, "text": part})
            continue
        dataset = getattr(part, "dataset", None)
        if isinstance(part, WindowRef) and part.window.startswith("dataset:"):
            dataset = part.window.split(":", 1)[1]
        if dataset and dataset not in refs:
            refs.append(dataset)
        try:
            raw, meta = resolve(part, datasets, window)
            display = fmt(part, raw, meta)
        except RefError as e:
            errors.append(f"第 {index} 个片段（{describe(part)}）：{e}")
            segments.append({"kind": "error", "part": index, "text": "[?]", "error": str(e), "path": describe(part)})
            continue
        ds = datasets.get(dataset) or {}
        segment = {"kind": {NumberRef: "value", CountRef: "count", LabelRef: "label", WindowRef: "window"}[type(part)],
                   "part": index, "ref": dataset or "window", "path": describe(part), "label": source_label(part),
                   "raw": [str(x) for x in raw] if isinstance(raw, tuple) else _key(raw), "text": display}
        if isinstance(part, (NumberRef, CountRef)):
            segment.update(measure=meta["kind"], unit=meta["unit"],
                           source=ds.get("kind") or "", explore=bool(ds.get("explore")),
                           assumption=bool(ds.get("assumption") or meta.get("column_assumption")),
                           truncated=bool(ds.get("truncated")),
                           partial=bool(ds.get("partial")))
        if isinstance(part, NumberRef):
            segment.update(precision=part.precision, direction=part.direction or "", role=part.role)
        segments.append(segment)
    return {"text": "".join(s["text"] for s in segments), "segments": segments, "errors": errors, "refs": refs}
