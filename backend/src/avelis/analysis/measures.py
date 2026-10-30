"""数据集数值列的量纲与单位（ADR 0009 第 1、3 点）。

每个数值列在数据集 meta.units 里登记 {kind, unit, scale, of_ratio}，计算工具的汇总路径登记在 meta.summary_units：
- level 水平值（指标值列）；delta 差额；ratio 比率水平（派生指标 a / b）；rate_change 相对变化率；share 占比；
  count 计数；unknown 无法判断。
- unit、scale 来自口径版本的 unit、unit_scale（列本身已是“万元”时 scale = 10000）。
derive 的结果量纲由公式形式推断；公式常量只允许白名单，其余常量把结果标为“假设计算”；每个输入都必须影响结果。
"""

from __future__ import annotations

import ast
from decimal import Decimal
from typing import Any

UNKNOWN = {"kind": "unknown"}
CONSTANTS = {Decimal(1), Decimal(100), Decimal(1000), Decimal(10000), Decimal(100000000)}


def col(kind: str, unit: str | None = "", scale: Any = 1, of_ratio: bool = False) -> dict[str, Any]:
    out: dict[str, Any] = {"kind": kind}
    if unit:
        out["unit"] = unit
    if scale not in (None, 1, "1") and Decimal(str(scale)) != 1:
        out["scale"] = str(scale)
    if of_ratio:
        out["of_ratio"] = True
    return out


def like(base: dict | None, kind: str | None = None, **extra) -> dict[str, Any]:
    """沿用输入列的单位，换成另一种量纲（如水平值的差额）。"""
    base = dict(base or UNKNOWN)
    if base.get("kind") == "unknown":
        return dict(UNKNOWN)
    out = {k: v for k, v in base.items() if k in ("unit", "scale")}
    out["kind"] = kind or base["kind"]
    if kind == "delta" and base.get("kind") in ("ratio", "rate_change", "share") and not base.get("unit"):
        out["of_ratio"] = True  # 无单位比率（转化率、占比）之差：可以用百分点
    out.update(extra)
    return out


def metric_kind(version: Any) -> str:
    """派生指标 a / b 为比率，其余为水平值。"""
    return "ratio" if getattr(version, "kind", "base") == "derived" and "/" in (version.expression or "") else "level"


def metric_unit(version: Any) -> dict[str, Any]:
    return col(metric_kind(version), getattr(version, "unit", None), getattr(version, "unit_scale", None) or 1)


def governed_units(columns: list[str], metrics: dict[str, Any], period_label: str | None) -> dict[str, dict]:
    """受治理查询结果的数值列：指标名（本期）、指标名·对比期、指标名·变化、指标名·变化率。"""
    units: dict[str, dict] = {}
    for name, version in metrics.items():
        base = metric_unit(version)
        for column, meta in ((name, base), (f"{name}·{period_label}", base), (f"{name}·变化", like(base, "delta")),
                             (f"{name}·变化率", {"kind": "rate_change"})):
            if column in columns:
                units[column] = meta
    return units


# --- derive ----------------------------------------------------------------------------

def constants(formula: str) -> list[Decimal]:
    """公式里不在白名单（1、100、1000、10000、1e8）里的常量。"""
    tree = ast.parse(formula.strip(), mode="eval")
    return [Decimal(str(n.value)) for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and Decimal(str(n.value)) not in CONSTANTS]


def formula_unit(formula: str, units: dict[str, dict]) -> dict[str, Any]:
    """按公式形式推断结果量纲：a/b 为比率，a/b-1 为变化率，a-b 为差额，同量纲加减保持原量纲，其余 unknown。
    水平值除以计数（或计数除以水平值，如金额 / 订单数）是每单位的均值，按水平值处理（单位写成“分子/分母”），
    不是无量纲比率：两个均值相减是差额，不会显示成百分比或“个百分点”。"""
    def kind_of(node: ast.AST) -> dict[str, Any]:
        if isinstance(node, ast.Expression):
            return kind_of(node.body)
        if isinstance(node, ast.Name):
            return dict(units.get(node.id) or UNKNOWN)
        if isinstance(node, ast.BinOp):
            left, right = kind_of(node.left), kind_of(node.right)
            if isinstance(node.op, ast.Div) and not isinstance(node.right, ast.Constant):
                if left["kind"] == "delta" and right["kind"] in ("level", "count") and _same(left, right):
                    return {"kind": "rate_change"}  # (a-b)/b
                if {left["kind"], right["kind"]} == {"level", "count"}:  # 金额 / 订单数：每单均值
                    unit = "/".join(x for x in (left.get("unit"), right.get("unit")) if x)
                    return col("level", unit)
                if left["kind"] in ("level", "count", "delta") and right["kind"] in ("level", "count", "delta"):
                    unit = "" if _same(left, right) else "/".join(x for x in (left.get("unit"), right.get("unit")) if x)
                    return col("ratio", unit)
                return dict(UNKNOWN)
            if isinstance(node.op, ast.Sub) and isinstance(node.right, ast.Constant) and node.right.value == 1:
                return {"kind": "rate_change"} if left["kind"] == "ratio" and not left.get("unit") else dict(UNKNOWN)
            if isinstance(node.op, (ast.Add, ast.Sub)) and _same(left, right) and left["kind"] != "unknown":
                if isinstance(node.op, ast.Sub) and left["kind"] in ("level", "count"):
                    return like(left, "delta")
                if isinstance(node.op, ast.Sub) and left["kind"] in ("ratio", "rate_change", "share"):
                    return like(left, "delta")
                return left
        return dict(UNKNOWN)

    return kind_of(ast.parse(formula.strip(), mode="eval"))


def _same(a: dict, b: dict) -> bool:
    return (a.get("unit") or "") == (b.get("unit") or "") and str(a.get("scale") or 1) == str(b.get("scale") or 1) and (
        a["kind"] == b["kind"] or {a["kind"], b["kind"]} <= {"level", "delta", "count"})


def insensitive(formula: str, env: dict[str, Decimal], evaluate) -> list[str]:
    """把每个输入各扰动一次，结果不变的输入（如 a*0+99999 里的 a）。"""
    base = evaluate(formula, env)
    out = []
    for name, value in env.items():
        for bumped in ((value or Decimal(0)) * Decimal("1.37") + Decimal("0.91"), (value or Decimal(0)) - Decimal("3.1")):
            if evaluate(formula, {**env, name: bumped}) != base:
                break
        else:
            out.append(name)
    return out
