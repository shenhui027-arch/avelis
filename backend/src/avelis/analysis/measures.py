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
# 0 是中性常量；其余用于比例、数量级与时间单位换算，探索查询共用此白名单。
CONSTANTS = {Decimal(value) for value in (0, 1, 24, 60, 100, 1000, 3600, 10000, 1000000, 100000000, 1000000000)}


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
    """优先使用显式量纲；未声明时，派生指标 a / b 为比率，其余为水平值。"""
    declared = getattr(version, "value_kind", None)
    if declared in {"level", "ratio", "count"}:
        return declared
    return "ratio" if getattr(version, "kind", "base") == "derived" and "/" in (version.expression or "") else "level"


def metric_unit(version: Any) -> dict[str, Any]:
    return col(metric_kind(version), getattr(version, "unit", None), getattr(version, "unit_scale", None) or 1)


def governed_units(columns: list[str], metrics: dict[str, Any], period_label: str | None) -> dict[str, dict]:
    """受治理查询结果的数值列：指标名（本期）、指标名·对比期、指标名·变化、指标名·变化率。"""
    units: dict[str, dict] = {}
    for name, version in metrics.items():
        base = metric_unit(version)
        declared = [(name, base)]
        if period_label is not None:
            declared.extend(((f"{name}·{period_label}", base), (f"{name}·变化", like(base, "delta")),
                             (f"{name}·变化率", {"kind": "rate_change"})))
        for column, meta in declared:
            if column in columns:
                units[column] = meta
    return units


def governed_polarity(columns: list[str], metrics: dict[str, Any], period_label: str | None) -> dict[str, str]:
    """指标各期间与变化列沿用锁定版本的期望方向；未声明时不推测。"""
    polarity: dict[str, str] = {}
    for name, version in metrics.items():
        direction = getattr(version, "polarity", None)
        if direction not in {"up_good", "down_good"}:
            continue
        for column in governed_units(columns, {name: version}, period_label):
            polarity[column] = direction
    return polarity


# --- derive ----------------------------------------------------------------------------

def constants(formula: str) -> list[Decimal]:
    """公式里不在白名单（0、1、24、60、100、1000、3600、10000、1e6、1e8、1e9）里的常量。"""
    tree = ast.parse(formula.strip(), mode="eval")
    return [Decimal(str(n.value)) for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and Decimal(str(n.value)) not in CONSTANTS]


def formula_unit(formula: str, units: dict[str, dict]) -> dict[str, Any]:
    """递归推导 AST 的量纲；输入已由 derive 按 scale 换算为基本单位。

    同单位加减保留单位，相除得到无量纲比率；比率与白名单常量可缩放带单位的量。
    不同单位的水平值 / 计数沿用每单位均值规则；不能确认的运算返回 unknown。
    """
    def scalar(meta: dict) -> bool:
        return meta["kind"] == "constant" or (
            not meta.get("unit") and meta["kind"] in {"ratio", "rate_change", "share"})

    def kind_of(node: ast.AST) -> dict[str, Any]:
        if isinstance(node, ast.Expression):
            return kind_of(node.body)
        if isinstance(node, ast.Name):
            return {key: value for key, value in (units.get(node.id) or UNKNOWN).items() if key != "scale"}
        if isinstance(node, ast.Constant):
            value = Decimal(str(node.value))
            return {"kind": "constant", "value": value} if value in CONSTANTS else dict(UNKNOWN)
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
            inner = kind_of(node.operand)
            return {**inner, "value": -inner["value"]} if inner["kind"] == "constant" else inner
        if isinstance(node, ast.BinOp):
            left, right = kind_of(node.left), kind_of(node.right)
            if left["kind"] == "unknown" or right["kind"] == "unknown":
                return dict(UNKNOWN)
            if left["kind"] == right["kind"] == "constant":
                # 仅做量纲推导；组合常量的数值由现有求值器处理。
                from avelis.governance.derived import evaluate

                value = evaluate(ast.unparse(node), {})
                return {"kind": "constant", "value": value} if value is not None else dict(UNKNOWN)
            if isinstance(node.op, (ast.Add, ast.Sub)):
                if right["kind"] == "constant" and right["value"] == 0:
                    return left
                if left["kind"] == "constant" and left["value"] == 0:
                    return right  # 0 - x 与 -x 保持同一量纲
                if (isinstance(node.op, ast.Sub) and right["kind"] == "constant" and right["value"] == 1
                        and left["kind"] == "ratio" and not left.get("unit")):
                    return {"kind": "rate_change"}
                if _same(left, right):
                    if isinstance(node.op, ast.Sub) and left["kind"] in ("level", "count", "ratio", "rate_change", "share"):
                        return like(left, "delta")
                    return left
            if isinstance(node.op, ast.Mult):
                if left["kind"] == "constant":
                    return right
                if right["kind"] == "constant":
                    return left
                if scalar(left):
                    return right if not scalar(right) else {"kind": "ratio"}
                if scalar(right):
                    return left
            if isinstance(node.op, ast.Div):
                if right["kind"] == "constant":
                    return left
                if scalar(right):
                    return left if not scalar(left) else {"kind": "ratio"}
                if left["kind"] == "delta" and right["kind"] in ("level", "count") and _same(left, right):
                    return {"kind": "rate_change"}  # (a-b)/b
                if _same(left, right) and ({left["kind"], right["kind"]} != {"level", "count"} or left.get("unit")):
                    return {"kind": "ratio"}
                if {left["kind"], right["kind"]} == {"level", "count"}:  # 水平值 / 计数：每单位均值
                    unit = "/".join(x for x in (left.get("unit"), right.get("unit")) if x)
                    return col("level", unit)
                if left["kind"] in ("level", "count", "delta") and right["kind"] in ("level", "count", "delta"):
                    unit = "" if _same(left, right) else "/".join(x for x in (left.get("unit"), right.get("unit")) if x)
                    return col("ratio", unit)
                return dict(UNKNOWN)
        return dict(UNKNOWN)

    result = kind_of(ast.parse(formula.strip(), mode="eval"))
    return dict(UNKNOWN) if result["kind"] == "constant" else result


def _same(a: dict, b: dict) -> bool:
    return (
        bool(a.get("of_ratio")) == bool(b.get("of_ratio"))
        and (a.get("unit") or "") == (b.get("unit") or "")
        and str(a.get("scale") or 1) == str(b.get("scale") or 1)
        and (a["kind"] == b["kind"] or {a["kind"], b["kind"]} <= {"level", "delta", "count"})
    )


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
