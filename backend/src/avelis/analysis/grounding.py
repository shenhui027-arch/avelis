"""报告数字的引用片段（ADR 0009 第 1 点）：模型写片段列表，后端按数据集取值、格式化后生成正文。

结论、标题、摘要、“未解释”都写成 parts: list[str | NumberRef | WindowRef]（参照 DB-GPT 报告规格的
ReportPart = str | ReportReference）。不在正文里嵌入内联占位语法，因此不需要转义，结构由 pydantic 与工具的
JSON schema 校验；取值时的语义错误（数据集、列不存在，没有恰好选中一行等）在渲染时逐条记下，反馈给模型。

引用的定义、取值、格式化与渲染集中在本模块，细节调整只改这里。
"""

from __future__ import annotations

from datetime import date
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

REF_PATTERN = r"^D[1-9]\d{0,3}$"
Format = Literal["d0", "d1", "d2", "d3", "d4", "int", "pct0", "pct1", "pct2", "pp0", "pp1", "pp2",
                 "wan0", "wan1", "wan2", "yi0", "yi1", "yi2"]
_SCALE = {"pct": (Decimal(100), "%"), "pp": (Decimal(100), "个百分点"), "wan": (Decimal("0.0001"), "万"),
          "yi": (Decimal("0.00000001"), "亿")}


class NumberRef(BaseModel):
    """数据集里的一个值。match 与 row 二选一（都不写时数据集必须只有一行）；value 取汇总值、行数或不同值个数。"""
    model_config = ConfigDict(extra="forbid")

    dataset: str = Field(pattern=REF_PATTERN, description="数据集编号，如 D5")
    column: str | None = Field(None, description="列名；value 为 rows / distinct:<列> 时不写")
    match: dict[str, str | int | float] | None = Field(
        None, description="选行条件，如 {'区域': '华东'}，必须恰好选中一行；求和请先用 derive")
    row: int | None = Field(None, ge=1, description="第几行（从 1 开始，按数据集顺序），与 match 二选一")
    value: str | None = Field(None, description="可选：summary（计算类数据集的汇总值）、rows（行数）、"
                                                "distinct:<列>（不同值个数）；截断的数据集不能取行数或不同值个数")
    format: Format | None = Field(None, description="d0–d4 小数位、int 取整、pct0–pct2 百分比、pp0–pp2 百分点、"
                                                    "wan0–wan2 / yi0–yi2 万 / 亿；不写时原值去掉多余的零")
    abs: bool = Field(False, description="取绝对值（配合“下降 / 减少”等词）")
    role: Literal["fact", "proposal"] = Field(
        "fact", description="proposal：建议的目标值（非观测值），只能用在推断、建议类结论中")


class WindowRef(BaseModel):
    """本次分析的本期或对比期。"""
    model_config = ConfigDict(extra="forbid")

    window: Literal["current", "compare"]
    format: Literal["range", "month"] = Field("range", description="range 显示日期区间；month 显示“2026 年 9 月”")


Part = str | NumberRef | WindowRef


class RefError(ValueError):
    """引用无法解析：数据集、列不存在，没有恰好选中一行，格式不适用等。消息直接反馈给模型。"""


def as_part(part: Any) -> Part:
    """存储与工具调用传来的片段（dict 或模型实例）还原成模型；结构不合法时抛 RefError。"""
    if isinstance(part, (str, NumberRef, WindowRef)):
        return part
    if isinstance(part, dict):
        try:
            return WindowRef.model_validate(part) if "window" in part else NumberRef.model_validate(part)
        except ValidationError as e:
            raise RefError(f"引用 {part} 结构不合法：{e.errors()[0]['msg']}") from None
    raise RefError(f"片段应为文字或引用对象，收到 {type(part).__name__}")


def describe(ref: NumberRef | WindowRef) -> str:
    """引用的简短写法，用于反馈与来源表：D5.成交额[区域=华东]、D3.门店[#1]、D12.summary、window.current。"""
    if isinstance(ref, WindowRef):
        return f"window.{ref.window}"
    if ref.value and not ref.column:
        path = ref.value
    else:
        path = (ref.column or "") + (f"（{ref.value}）" if ref.value else "")
    if ref.match:
        path += "[" + ",".join(f"{k}={v}" for k, v in ref.match.items()) + "]"
    elif ref.row:
        path += f"[#{ref.row}]"
    return f"{ref.dataset}.{path}"


# --- 取值 ------------------------------------------------------------------------------

def resolve(ref: NumberRef | WindowRef, datasets: dict[str, dict], window: dict | None) -> Any:
    """引用的原值。datasets：编号 → {kind, columns, rows（list[dict]）, summary, truncated}。"""
    if isinstance(ref, WindowRef):
        return _window(ref, window or {})
    name = describe(ref)
    ds = datasets.get(ref.dataset)
    if ds is None:
        raise RefError(f"{name}：数据集 {ref.dataset} 不存在")
    rows, summary = ds.get("rows") or [], ds.get("summary") or {}
    columns = list(ds.get("columns") or (rows[0].keys() if rows else []))
    if ref.match is not None and ref.row is not None:
        raise RefError(f"{name}：match 与 row 只能写一个")
    value = ref.value
    if value == "rows" or (value or "").startswith("distinct:"):
        if ref.match is not None or ref.row is not None:
            raise RefError(f"{name}：行数与不同值个数不能带选行条件")
        if ds.get("truncated"):
            raise RefError(f"{name}：{ref.dataset} 已截断，不是全集，不能取行数或不同值个数")
        if value == "rows":
            return Decimal(len(rows))
        col = value.split(":", 1)[1].strip()
        if col not in columns:
            raise RefError(f"{name}：{ref.dataset} 没有列 {col}（可用：{'、'.join(map(str, columns))}）")
        return Decimal(len({_key(r.get(col)) for r in rows}))
    if value == "summary":
        key = ref.column or "value"
        if not isinstance(summary, dict) or key not in summary:
            raise RefError(f"{name}：{ref.dataset} 没有汇总值 {key}（可用：{'、'.join(summary) if summary else '无'}）")
        return summary[key]
    if value is not None:
        raise RefError(f"{name}：value 只能是 summary、rows 或 distinct:<列>")
    if not ref.column:
        raise RefError(f"{name}：缺少列名 column")
    if ref.column not in columns:
        raise RefError(f"{name}：{ref.dataset} 没有列 {ref.column}（可用：{'、'.join(map(str, columns))}）")
    return _select(ref, name, rows, columns)[ref.column]


def _select(ref: NumberRef, name: str, rows: list[dict], columns: list) -> dict:
    if ref.row is not None:
        if ref.row > len(rows):
            raise RefError(f"{name}：{ref.dataset} 只有 {len(rows)} 行")
        return rows[ref.row - 1]
    if not ref.match:
        if len(rows) != 1:
            raise RefError(f"{name}：{ref.dataset} 有 {len(rows)} 行，请用 match 或 row 选定一行")
        return rows[0]
    unknown = [c for c in ref.match if c not in columns]
    if unknown:
        raise RefError(f"{name}：{ref.dataset} 没有列 {'、'.join(unknown)}（可用：{'、'.join(map(str, columns))}）")
    hits = [r for r in rows if all(_equal(r.get(c), v) for c, v in ref.match.items())]
    if len(hits) != 1:
        raise RefError(f"{name}：条件选中了 {len(hits)} 行，必须恰好一行（求和请先用 derive）")
    return hits[0]


def _key(value: Any) -> str:
    return "" if value is None else str(value)


def _equal(cell: Any, value: Any) -> bool:
    if _key(cell) == _key(value):
        return True
    a, b = number(cell), number(value)
    return a is not None and b is not None and a == b


def _window(ref: WindowRef, window: dict) -> tuple[date, date]:
    keys = {"current": ("start", "end"), "compare": ("compare_start", "compare_end")}[ref.window]
    start, end = (window.get(k) for k in keys)
    if not start or not end:
        raise RefError(f"window.{ref.window}：本次分析没有{'本期' if ref.window == 'current' else '对比期'}窗口")
    return date.fromisoformat(str(start)), date.fromisoformat(str(end))


# --- 格式化 ----------------------------------------------------------------------------

def number(value: Any) -> Decimal | None:
    """有限的数值；字符串数字可以，NaN、Infinity、文字都不是。"""
    if value is None or isinstance(value, bool):
        return None
    try:
        d = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None
    return d if d.is_finite() else None


def _plain(d: Decimal) -> str:
    """不用科学计数法，去掉多余的零。"""
    text = format(d, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def fmt(ref: NumberRef | WindowRef, raw: Any) -> str:
    """按格式把原值变成显示文字（四舍五入 ROUND_HALF_UP）。"""
    if isinstance(ref, WindowRef):
        start, end = raw
        if ref.format == "month":
            last = date(end.year + end.month // 12, end.month % 12 + 1, 1).toordinal() - 1
            if start.day != 1 or (start.year, start.month) != (end.year, end.month) or end.toordinal() != last:
                raise RefError(f"window.{ref.window}：{start} ~ {end} 不是一个整月，不能用 month 格式")
            return f"{start.year} 年 {start.month} 月"
        return f"{start} ~ {end}"
    value = number(raw)
    if value is None or (isinstance(raw, str) and not _numeric_text(raw)):
        if ref.format or ref.abs:
            raise RefError(f"{describe(ref)}：原值“{_key(raw)}”不是数值，不能用 format / abs")
        return _key(raw)  # 名称、日期等文字原样
    if ref.abs:
        value = abs(value)
    if not ref.format:
        return _plain(value)
    suffix, places = "", 0
    if ref.format == "int":
        places = 0
    elif ref.format.startswith("d"):
        places = int(ref.format[1:])
    else:
        kind = next(k for k in _SCALE if ref.format.startswith(k))
        scale, suffix = _SCALE[kind]
        value, places = value * scale, int(ref.format[len(kind):])
    return format(value.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP), "f") + suffix


def _numeric_text(raw: str) -> bool:
    try:
        return Decimal(raw.strip()).is_finite() and raw.strip()[:1] in "+-0123456789"
    except (InvalidOperation, ValueError):
        return False


# --- 渲染 ------------------------------------------------------------------------------

def render(parts: Any, datasets: dict[str, dict], window: dict | None) -> dict[str, Any]:
    """渲染片段列表：text（正文）、segments（文字段与数值段）、errors（每个解析失败的引用一条）、refs（引用涉及的数据集）。
    解析失败的引用在正文里显示为“[?]”。parts 为 None 时视为空；单个字符串视为只有一个文字片段。"""
    if parts is None:
        parts = []
    elif isinstance(parts, str):
        parts = [parts]
    segments: list[dict[str, Any]] = []
    errors: list[str] = []
    refs: list[str] = []
    for item in parts:
        try:
            part = as_part(item)
        except RefError as e:
            errors.append(str(e))
            segments.append({"kind": "error", "text": "[?]", "error": str(e)})
            continue
        if isinstance(part, str):
            segments.append({"kind": "text", "text": part})
            continue
        if isinstance(part, NumberRef) and part.dataset not in refs:
            refs.append(part.dataset)
        try:
            raw = resolve(part, datasets, window)
            display = fmt(part, raw)
        except RefError as e:
            errors.append(str(e))
            segments.append({"kind": "error", "text": "[?]", "error": str(e), "path": describe(part)})
            continue
        segment = {"kind": "value", "ref": "window" if isinstance(part, WindowRef) else part.dataset,
                   "path": describe(part), "format": part.format or "",
                   "raw": [str(x) for x in raw] if isinstance(raw, tuple) else _key(raw), "text": display}
        if isinstance(part, NumberRef):
            segment.update(abs=part.abs, role=part.role)
        segments.append(segment)
    return {"text": "".join(s["text"] for s in segments), "segments": segments, "errors": errors, "refs": refs}
