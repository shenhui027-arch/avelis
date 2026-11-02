"""自动归因的确定性计算（ADR 0009 第 4 点，explain_change）：Adtributor 的简化变体，加比率指标的结构 / 比率拆解。

- 适用范围从口径表达式解析：可加指标（SUM、COUNT(*)）用贡献算法；派生比率 a / b（a、b 都可加）用比率算法；
  COUNT(DISTINCT)、AVG、MIN、MAX 等不可加指标返回“不支持自动归因”和原因，不硬算。
- 成员取两期并集，新增成员的上期为 0、退出成员的本期为 0，单独标注；成员合计必须与单独查询的总量对账，
  对不上就拒绝（不当作舍入差）。
- 可加：c_i = 成员差额，EP_i = c_i / ΔT（带符号的净贡献比例，可以超过 100%）。
- 比率：w = b / Σb，r = a / b；c_i = (w_i1 − w_i0)·r_i0 + w_i1·(r_i1 − r_i0)，第一项是结构效应，第二项是比率效应，
  交互项归入比率效应（拆法不唯一）；新增、退出成员只有结构效应（w_1·r_1 或 −w_0·r_0），分母为 0 记为不可计算。
- |ΔT| 低于对比期的显著性阈值（产品参数，默认 1%，可按指标配置），或 |ΔT| / Σ|c_i| < 0.2（成员变化相互抵消）时
  不生成排名，分别列出增加项与减少项。这个判定就是报告的结论类型（ADR 0009 第 7 点），不交给模型选择。
- 分布变化强度：两期成员份额分布的 JS 散度，只对非负、总量为正的分布计算。维度按它排序（并列按维度名），
  维度内成员按 |EP| 排序，取到累计 EP ≥ 0.67 或达到 top_k 为止；各维度的 EP 不跨维度相加。
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

EP_THRESHOLD = Decimal("0.67")
MIN_CHANGE_RATIO = Decimal("0.01")  # 默认的显著性阈值：|ΔT| 低于对比期的 1% 为总体变化不明显
OFFSET_RATIO = Decimal("0.2")  # |ΔT| / Σ|c_i| 低于它：成员变化相互抵消
RECONCILE_TOLERANCE = Decimal("1e-9")  # 只容忍浮点存储的表示误差（相对），不当作舍入差
NULL_MEMBER = "（空）"
_SUM = re.compile(r"^SUM\s*\((?!\s*DISTINCT\b).+\)$", re.I | re.S)
_COUNT_ALL = re.compile(r"^COUNT\s*\(\s*\*\s*\)$", re.I)
_NON_ADDITIVE = re.compile(r"^(COUNT\s*\(\s*DISTINCT|AVG|MIN|MAX|MEDIAN|PERCENTILE|APPROX_)", re.I)
_RATIO = re.compile(r"^\s*([A-Za-z_]\w*)\s*/\s*([A-Za-z_]\w*)\s*$")
RATIO_FORMULA = _RATIO  # 派生比率口径的公式 a / b（自动因子拆解按它找分子为所问指标的比率口径）


class AttributionError(ValueError):
    """不能归因（对账失败等）：消息直接反馈给模型。"""


def q(value: Decimal | None, places: int = 4) -> Decimal | None:
    return None if value is None else value.quantize(Decimal(1).scaleb(-places))


# --- 适用范围 ---------------------------------------------------------------------------


def _single_call(text: str) -> bool:
    """整个表达式是一次函数调用：第一个左括号与最后一个字符的右括号配对（SUM(a) - SUM(b) 不是）。"""
    depth = 0
    for i, ch in enumerate(text):
        depth += (ch == "(") - (ch == ")")
        if depth == 0 and ch == ")":
            return i == len(text) - 1
    return False


def base_additive(expression: str) -> tuple[bool, str]:
    text = (expression or "").strip()
    if _COUNT_ALL.match(text) or (_SUM.match(text) and _single_call(text)):
        return True, ""
    if _NON_ADDITIVE.match(text):
        kind = re.match(r"[A-Za-z_]+(\s*\(\s*DISTINCT)?", text).group(0).upper().replace(" ", "")
        return False, f"表达式 {text} 是 {kind.replace('(', '(')} 类聚合，不可加（成员之和不等于总量）"
    return False, f"表达式 {text} 不是单一的 SUM 或 COUNT(*)，无法确定可加性"


def applicability(version: Any, components: dict[str, Any]) -> dict[str, Any]:
    """口径的归因方式：{algorithm: additive | ratio | None, reason, numerator, denominator}。
    components：派生指标的组成指标编码 → 组成版本。"""
    if getattr(version, "kind", "base") != "derived":
        ok, reason = base_additive(version.expression)
        return {"algorithm": "additive" if ok else None, "reason": reason}
    m = _RATIO.match(version.expression or "")
    if not m:
        return {"algorithm": None, "reason": f"派生公式 {version.expression} 不是 a / b 形式的比率，不支持自动归因"}
    numerator, denominator = m.group(1), m.group(2)
    for code in (numerator, denominator):
        comp = components.get(code)
        if comp is None:
            return {"algorithm": None, "reason": f"组成指标 {code} 没有锁定的版本"}
        ok, reason = (
            base_additive(comp.expression)
            if getattr(comp, "kind", "base") != "derived"
            else (False, f"组成指标 {code} 本身是派生指标")
        )
        if not ok:
            return {"algorithm": None, "reason": f"比率的组成指标 {code} 不可加：{reason}"}
    return {"algorithm": "ratio", "reason": "", "numerator": numerator, "denominator": denominator}


# --- 对账与成员 -------------------------------------------------------------------------


def reconciles(parts: list[Decimal], total: Decimal | None) -> bool:
    total = total or Decimal(0)
    return abs(sum(parts, Decimal(0)) - total) <= RECONCILE_TOLERANCE * max(Decimal(1), abs(total))


def member_name(key: tuple) -> str:
    return " / ".join(NULL_MEMBER if v in (None, "") else str(v) for v in key)


# --- 分布变化强度 -----------------------------------------------------------------------


def js_divergence(before: list[Decimal], after: list[Decimal]) -> float | None:
    """两期份额分布的 JS 散度（以 2 为底，0–1）；只对非负、总量为正的分布计算，否则返回 None（不偷偷取绝对值）。"""
    if any(v < 0 for v in before + after):
        return None
    s0, s1 = sum(before, Decimal(0)), sum(after, Decimal(0))
    if s0 <= 0 or s1 <= 0:
        return None
    p = [float(v / s0) for v in before]
    r = [float(v / s1) for v in after]

    def kl(a: list[float], b: list[float]) -> float:
        return sum(x * math.log2(x / y) for x, y in zip(a, b) if x > 0)

    m = [(x + y) / 2 for x, y in zip(p, r)]
    return round(0.5 * kl(p, m) + 0.5 * kl(r, m), 6)


# --- 单个维度 ---------------------------------------------------------------------------


@dataclass
class Dimension:
    name: str
    rows: list[dict[str, Any]] = field(default_factory=list)  # 全部成员（未截取）
    js: float | None = None
    verdict: str = "ranked"  # ranked | offsetting | no_material_change
    selected: list[dict[str, Any]] = field(default_factory=list)
    cumulative_ep: Decimal | None = None
    threshold_met: bool | None = None


def additive_members(values: dict[tuple, tuple[Decimal | None, Decimal | None]], total_change: Decimal) -> list[dict]:
    """可加指标：成员差额与 EP。values：成员 → (对比期值 | None, 本期值 | None)，None 表示该期没有这个成员。"""
    rows = []
    for key, (prev, cur) in values.items():
        status = "新增" if prev is None and cur is not None else "退出" if cur is None and prev is not None else ""
        p, c = prev or Decimal(0), cur or Decimal(0)
        change = c - p
        rows.append(
            {
                "member": member_name(key),
                "previous": p,
                "current": c,
                "member_change": change,
                "contribution": change,
                "ep": q(change / total_change) if total_change else None,
                "structure_effect": None,
                "rate_effect": None,
                "change_type": status,
            }
        )
    return rows


def ratio_members(
    values: dict[tuple, tuple], totals: tuple[Decimal, Decimal, Decimal, Decimal], total_change: Decimal
) -> tuple[list[dict], Decimal]:
    """比率指标：values：成员 → (a0, b0, a1, b1)（None 表示该期没有这个成员）；totals：(A0, B0, A1, B1)。
    返回（成员行，Σ贡献）。成员比率变化与对总体比率的贡献分两列。"""
    _, b0_total, _, b1_total = totals
    rows, total = [], Decimal(0)
    for key, (a0, b0, a1, b1) in values.items():
        gone, new = a1 is None and b1 is None, a0 is None and b0 is None
        a0, b0, a1, b1 = (v or Decimal(0) for v in (a0, b0, a1, b1))
        w0 = b0 / b0_total if b0_total else None
        w1 = b1 / b1_total if b1_total else None
        r0 = a0 / b0 if b0 else None
        r1 = a1 / b1 if b1 else None
        row = {
            "member": member_name(key),
            "previous": q(r0),
            "current": q(r1),
            "member_change": q(r1 - r0) if r0 is not None and r1 is not None else None,
            "structure_effect": None,
            "rate_effect": None,
            "contribution": None,
            "ep": None,
            "change_type": "新增" if new else "退出" if gone else "",
        }
        if new and w1 is not None and r1 is not None:  # 新增成员：只有结构效应，不补零、不估算比率
            row["structure_effect"] = row["contribution"] = w1 * r1
        elif gone and w0 is not None and r0 is not None:
            row["structure_effect"] = row["contribution"] = -(w0 * r0)
        elif None not in (w0, w1, r0, r1):
            structure, rate = (w1 - w0) * r0, w1 * (r1 - r0)
            row.update(structure_effect=structure, rate_effect=rate, contribution=structure + rate)
        else:
            row["change_type"] = (row["change_type"] + " " if row["change_type"] else "") + "不可计算（分母为 0）"
        if row["contribution"] is not None:
            total += row["contribution"]
            row["ep"] = q(row["contribution"] / total_change) if total_change else None
        for k in ("structure_effect", "rate_effect", "contribution"):
            row[k] = q(row[k])
        rows.append(row)
    return rows, total


def immaterial(total_previous: Decimal, total_change: Decimal, threshold: Decimal = MIN_CHANGE_RATIO) -> bool:
    return total_change == 0 or abs(total_change) < threshold * abs(total_previous)


def judge(
    dim: Dimension, total_previous: Decimal, total_change: Decimal, top_k: int, threshold: Decimal = MIN_CHANGE_RATIO
) -> Dimension:
    """维度内的判定与截取（ADR 0009 第 4 点的判定顺序）：先看成员变动 Σ|c_i|，未达阈值为变化不明显；达到阈值而
    |ΔT| 低于阈值或 |ΔT| / Σ|c_i| < 0.2 为相互抵消。这两种不排名，分别列出增加项与减少项（最大的减少项与增加项是首要
    原因）；否则按 |EP| 取到累计 ≥ 0.67 或 top_k。"""
    computable = [r for r in dim.rows if r["contribution"] is not None]
    moved = sum((abs(r["contribution"]) for r in computable), Decimal(0))
    if immaterial(total_previous, moved, threshold):
        dim.verdict = "no_material_change"
    elif immaterial(total_previous, total_change, threshold) or abs(total_change) / moved < OFFSET_RATIO:
        dim.verdict = "offsetting"
    if dim.verdict != "ranked":
        ups = sorted((r for r in computable if r["contribution"] > 0), key=lambda r: (-r["contribution"], r["member"]))
        downs = sorted((r for r in computable if r["contribution"] < 0), key=lambda r: (r["contribution"], r["member"]))
        dim.selected = [{**r, "role": "增加项"} for r in ups[:top_k]] + [{**r, "role": "减少项"} for r in downs[:top_k]]
        return dim
    ordered = sorted((r for r in computable if r["ep"] is not None), key=lambda r: (-abs(r["ep"]), r["member"]))
    cumulative = Decimal(0)
    for r in ordered[:top_k]:
        dim.selected.append({**r, "role": "原因"})
        cumulative += r["ep"]
        if cumulative >= EP_THRESHOLD:
            break
    dim.cumulative_ep, dim.threshold_met = q(cumulative), cumulative >= EP_THRESHOLD
    return dim


def rank(dims: list[Dimension]) -> list[Dimension]:
    """维度按分布变化强度排序（不可计算的排最后），并列按维度名。"""
    return sorted(dims, key=lambda d: (d.js is None, -(d.js or 0.0), d.name))


def verdict(dims: list[Dimension], scanned_any: bool, total_immaterial: bool = False, candidates: bool = True) -> str:
    """总体判定。candidates：有没有候选维度。没有候选维度（指标没有维度，或维度都被筛选掉了）时成员只有范围总量自己，
    Σ|c_i| = |ΔT|：按总变化判定，达到阈值为 ranked_reasons（原因来自因子、日历等），否则没有实质变化。
    有候选维度却一个都没扫成：证据不足。"""
    if not candidates:
        return "no_material_change" if total_immaterial else "ranked_reasons"
    if not scanned_any:
        return "insufficient_evidence"
    if all(d.verdict == "no_material_change" for d in dims):
        return "no_material_change"
    if all(d.verdict != "ranked" for d in dims):
        return "offsetting"
    return "ranked_reasons"
