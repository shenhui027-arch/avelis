"""问数解释数值核验：确定性校验模型生成的 explanation 中的数字是否有查询结果或洞察依据。"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Any

from avelis.analysis.grounding import _unit, display, number
from avelis.analysis.verify import IDENTIFIER_TOKEN

_DATE_RE = re.compile(
    r"\d{4}[-/年]\d{1,2}(?:[-/月]\d{1,2}日?)?"
    r"|\d{1,2}月\d{1,2}日"
    r"|\d{1,2}月"
    r"|\d{4}年(?:第?[1-4]季度)?"
    r"|\d{4}-?Q[1-4]"
    r"|(?<![A-Za-z0-9])Q[1-4](?![A-Za-z0-9])"
    r"|第\d+(?:名|位|个|条|项|期|周|季度|月|年)"
)
_NUM_MENTION_RE = re.compile(
    r"(?<![A-Za-z0-9_])([+-]?\d{1,3}(?:,\d{3})+|[+-]?\d+)(?:\.(\d+))?\s*(亿|万|[KMBkmb](?![A-Za-z])|%|％|个百分点|百分点)?"
)
_SCALE_MAP = {
    "亿": Decimal(100_000_000),
    "万": Decimal(10_000),
    "k": Decimal(1_000),
    "m": Decimal(1_000_000),
    "b": Decimal(1_000_000_000),
}
_PCT_SUFFIXES = frozenset({"%", "％", "个百分点", "百分点"})


def _parse_mentions(text: str) -> list[tuple[str, Decimal, int, str | None, bool, bool]]:
    """返回 [(raw_span, value, precision, suffix, explicit_negative, explicit_positive)]。"""
    mentions: list[tuple[str, Decimal, int, str | None, bool, bool]] = []
    for m in _NUM_MENTION_RE.finditer(text):
        int_part, frac_part, suffix = m.group(1), m.group(2), m.group(3)
        explicit_neg = int_part.startswith("-")
        explicit_pos = int_part.startswith("+")
        num_str = int_part.replace(",", "") + (f".{frac_part}" if frac_part else "")
        try:
            val = Decimal(num_str)
        except InvalidOperation:
            continue
        mentions.append((m.group(0).strip(), val, len(frac_part or ""), suffix, explicit_neg, explicit_pos))
    return mentions


def _add_scaled_mentions(pool: set[Decimal], text: str) -> None:
    cleaned = IDENTIFIER_TOKEN.sub(" ", _DATE_RE.sub(" ", text or ""))
    for _, val, _, suffix, _, _ in _parse_mentions(cleaned):
        scale = _SCALE_MAP.get((suffix or "").lower(), Decimal(1))
        scaled = val * scale
        pool.add(scaled)
        if suffix in _PCT_SUFFIXES:
            pool.add(val / Decimal(100))


def _grounded_pool(result: dict[str, Any], intent: dict[str, Any] | None) -> tuple[set[Decimal], list[str]]:
    pool: set[Decimal] = set()
    exempt_tokens: list[str] = []
    columns = result.get("columns") or []
    rows = result.get("rows") or []
    roles = result.get("column_roles") or {}
    units = result.get("units") or {}

    if isinstance(rows, list):
        pool.add(Decimal(len(rows)))
        for row in rows:
            if not isinstance(row, (list, tuple)):
                continue
            for col, cell in zip(columns, row):
                role_info = roles.get(col, {})
                role = role_info.get("role")
                parsed = number(cell)
                if role in ("time", "dimension"):
                    if cell is not None:
                        text = str(cell).strip()
                        if len(text) >= 2:
                            exempt_tokens.append(text)
                    if parsed is not None:
                        pool.add(parsed)
                    continue
                if parsed is None:
                    if cell is not None:
                        text = str(cell).strip()
                        if len(text) >= 2:
                            exempt_tokens.append(text)
                    continue
                pool.add(parsed)
                unit_meta = _unit(units.get(col))
                if (
                    unit_meta["kind"] in ("rate_change", "share")
                    or (unit_meta["kind"] == "ratio" and not unit_meta["unit"])
                    or role == "rate"
                ):
                    pool.add(parsed * Decimal(100))
                formatted = display(parsed, unit_meta)
                _add_scaled_mentions(pool, formatted)

    if result.get("row_count") is not None:
        if (rc := number(result.get("row_count"))) is not None:
            pool.add(rc)
    if intent and intent.get("limit") is not None:
        if (lim := number(intent.get("limit"))) is not None:
            pool.add(lim)

    for insight in result.get("insights") or []:
        if not isinstance(insight, dict):
            continue
        if (iv := number(insight.get("value"))) is not None:
            pool.add(iv)
            pool.add(iv * Decimal(100))
        if insight.get("text"):
            _add_scaled_mentions(pool, str(insight["text"]))

    exempt_tokens = sorted(set(exempt_tokens), key=len, reverse=True)
    return pool, exempt_tokens


def _matches_pool(
    val: Decimal,
    precision: int,
    suffix: str | None,
    explicit_neg: bool,
    explicit_pos: bool,
    pool: set[Decimal],
) -> bool:
    scale = _SCALE_MAP.get((suffix or "").lower(), Decimal(1))
    half_ulp = Decimal("0.5") * (Decimal(10) ** -precision)
    candidates: list[tuple[Decimal, Decimal]] = [(val * scale, half_ulp * scale)]
    if suffix in _PCT_SUFFIXES:
        candidates.append((val / Decimal(100), half_ulp / Decimal(100)))
        candidates.append((val, half_ulp))

    for target, tol in candidates:
        eps = tol + Decimal("1e-9")
        for g in pool:
            if explicit_neg and g > 0:
                continue
            if explicit_pos and g < 0:
                continue
            if abs(g - target) <= eps:
                return True
            if not explicit_neg and not explicit_pos and abs(abs(g) - target) <= eps:
                return True
    return False


def verify_explanation(explanation: str, answer: Any, intent: dict[str, Any] | None = None) -> dict[str, Any]:
    """核验 explanation 中除日期、标识符与维度标签外的数字是否均可在结果或洞察中找到依据。"""
    text = (explanation or "").strip()
    if not text:
        return {"ok": True, "checked_count": 0, "ungrounded": []}

    result = getattr(answer, "result", None) or {}
    if not isinstance(result, dict):
        result = {}
    pool, exempt_tokens = _grounded_pool(result, intent)

    cleaned = _DATE_RE.sub(" ", text)
    cleaned = IDENTIFIER_TOKEN.sub(" ", cleaned)
    for tok in exempt_tokens:
        if tok in cleaned:
            cleaned = re.sub(
                rf"(?<![0-9A-Za-z_])(?<!\d\.)(?<!\d,){re.escape(tok)}(?![0-9A-Za-z_])(?!\.\d)(?!,\d)",
                " ",
                cleaned,
            )

    mentions = _parse_mentions(cleaned)
    ungrounded: list[str] = []
    for raw_span, val, precision, suffix, explicit_neg, explicit_pos in mentions:
        if not _matches_pool(val, precision, suffix, explicit_neg, explicit_pos, pool):
            ungrounded.append(raw_span)

    return {
        "ok": len(ungrounded) == 0,
        "checked_count": len(mentions),
        "ungrounded": ungrounded,
    }
