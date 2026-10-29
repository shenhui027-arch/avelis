"""派生指标：由已发布的组成指标按公式计算（例如 客单价 = paid_sales / orders_cnt）。

公式只允许：指标编码、数字、加减乘除、括号、取负。用 Python AST 白名单解析与求值，不执行任何代码。
提交时锁定每个组成指标的具体版本；问数时组成指标各自走完整治理管线，后端按维度键对齐后再计算，
除数为 0 或任一组成值缺失时结果为空（不当作 0）。
"""

from __future__ import annotations

import ast
from decimal import Decimal, DivisionByZero, InvalidOperation
from typing import Any

from avelis.governance.errors import Invalid

MAX_COMPONENTS = 4
_OPS = {ast.Add: "+", ast.Sub: "-", ast.Mult: "*", ast.Div: "/"}


def parse_formula(formula: str) -> list[str]:
    """校验公式并返回引用的指标编码（按首次出现顺序）。"""
    text = (formula or "").strip()
    if not text:
        raise Invalid("派生指标需要公式，例如 paid_sales / orders_cnt")
    try:
        tree = ast.parse(text, mode="eval")
    except SyntaxError as exc:
        raise Invalid(f"公式无法解析：{text}") from exc
    codes: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            if node.id not in codes:
                codes.append(node.id)
        elif isinstance(node, ast.BinOp):
            if type(node.op) not in _OPS:
                raise Invalid(f"公式只允许加减乘除：{text}")
        elif isinstance(node, ast.UnaryOp):
            if not isinstance(node.op, ast.USub):
                raise Invalid(f"公式只允许取负这一种一元运算：{text}")
        elif isinstance(node, ast.Constant):
            if not isinstance(node.value, (int, float)) or isinstance(node.value, bool):
                raise Invalid(f"公式中的常量只能是数字：{text}")
        elif not isinstance(node, (ast.Expression, ast.Load, *_OPS, ast.USub)):
            raise Invalid(f"公式包含不支持的语法 {type(node).__name__}：{text}")
    if not codes:
        raise Invalid("公式至少要引用一个指标")
    if len(codes) > MAX_COMPONENTS:
        raise Invalid(f"一个派生指标最多引用 {MAX_COMPONENTS} 个指标")
    return codes


def evaluate(formula: str, values: dict[str, Any]) -> Decimal | None:
    """按公式计算；任一引用值缺失、除数为 0 时返回 None。"""
    tree = ast.parse(formula.strip(), mode="eval")

    def ev(node: ast.AST) -> Decimal | None:
        if isinstance(node, ast.Expression):
            return ev(node.body)
        if isinstance(node, ast.Name):
            v = values.get(node.id)
            return None if v is None else Decimal(str(v))
        if isinstance(node, ast.Constant):
            return Decimal(str(node.value))
        if isinstance(node, ast.UnaryOp):
            inner = ev(node.operand)
            return None if inner is None else -inner
        if isinstance(node, ast.BinOp):
            left, right = ev(node.left), ev(node.right)
            if left is None or right is None:
                return None
            op = type(node.op)
            try:
                if op is ast.Add:
                    return left + right
                if op is ast.Sub:
                    return left - right
                if op is ast.Mult:
                    return left * right
                if right == 0:
                    return None
                return left / right
            except (DivisionByZero, InvalidOperation):
                return None
        raise Invalid(f"公式包含不支持的语法 {type(node).__name__}")

    result = ev(tree)
    return None if result is None else result.quantize(Decimal("0.0001"))
