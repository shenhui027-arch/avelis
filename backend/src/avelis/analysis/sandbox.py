"""受控只读 Python 计算沙箱（M3D）：零外部依赖的纯 Python AST 解释器。

不使用 exec / eval / compile，仅允许白名单内的 AST 节点、只读方法与纯计算内置函数；
强制步数上限、墙钟超时与输出规模上限；跟踪输入数据集行级来源（D{n}#row）与常量白名单假设标记。
"""

from __future__ import annotations

import ast
import copy
import json
import math
import re
import statistics
import time
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from avelis.analysis import calc, measures

MAX_CODE_CHARS = 8000
MAX_AST_NODES = 1500
MAX_AST_DEPTH = 30
MAX_STEPS = 100_000
MAX_RANGE = 10_000
MAX_POW_EXP = 100
MAX_INT_BITS = 16_384
_MAX_DECIMAL_INT_EXP = int(MAX_INT_BITS * 0.30103) + 1
MAX_SEQ_LEN = 10_000
MAX_STR_LEN = 64_000
MAX_FORMAT_WIDTH = 10_000
MAX_OUTPUT_ROWS = 500
MAX_OUTPUT_COLS = 30
MAX_OUTPUT_CHARS = 32_000

_STMT_TYPES = (
    ast.Module,
    ast.Assign,
    ast.AugAssign,
    ast.Expr,
    ast.If,
    ast.For,
    ast.Break,
    ast.Continue,
    ast.Pass,
)
_EXPR_TYPES = (
    ast.Constant,
    ast.Name,
    ast.List,
    ast.Tuple,
    ast.Dict,
    ast.Set,
    ast.BinOp,
    ast.UnaryOp,
    ast.BoolOp,
    ast.Compare,
    ast.IfExp,
    ast.Subscript,
    ast.Slice,
    ast.ListComp,
    ast.DictComp,
    ast.SetComp,
    ast.GeneratorExp,
    ast.Lambda,
    ast.Call,
    ast.Attribute,
    ast.JoinedStr,
    ast.FormattedValue,
    ast.comprehension,
    ast.Load,
    ast.Store,
)
_BINOPS = (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow)
_UNARYOPS = (ast.UAdd, ast.USub, ast.Not)
_BOOLOPS = (ast.And, ast.Or)
_CMPOPS = (ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.In, ast.NotIn, ast.Is, ast.IsNot)

_ALLOWED_METHODS: dict[type, frozenset[str]] = {
    dict: frozenset({"get", "keys", "values", "items", "copy", "setdefault", "update", "pop"}),
    list: frozenset({"append", "extend", "sort", "copy", "pop", "insert", "reverse", "index", "count"}),
    tuple: frozenset({"index", "count"}),
    str: frozenset(
        {
            "strip",
            "lstrip",
            "rstrip",
            "lower",
            "upper",
            "title",
            "startswith",
            "endswith",
            "split",
            "join",
            "replace",
            "find",
            "count",
        }
    ),
    set: frozenset({"add", "copy", "union", "intersection", "difference", "update", "discard", "remove"}),
    Decimal: frozenset({"quantize"}),
}

_SENTINEL = object()


class SandboxError(calc.CalcError):
    """沙箱校验、超时或执行越界错误。"""


class _BreakSignal(Exception):
    pass


class _ContinueSignal(Exception):
    pass


@dataclass(frozen=True)
class SandboxResult:
    columns: list[str]
    rows: list[list[Any]]
    summary: dict[str, Any]
    units: dict[str, dict[str, Any]]
    summary_units: dict[str, dict[str, Any]]
    assumption: bool
    source_evidence_ids: list[str]
    non_whitelist_constants: list[str]
    accessed_keys: dict[str, list[str]] = field(default_factory=dict)
    accessed_all_keys: list[str] = field(default_factory=list)


class _DatasetStr(str):
    """标记来自输入数据集单元格的字符串，以区分于脚本字面量或脚本拼接字符串。"""

    def strip(self, chars: str | None = None) -> _DatasetStr:
        return _DatasetStr(super().strip(chars))

    def lstrip(self, chars: str | None = None) -> _DatasetStr:
        return _DatasetStr(super().lstrip(chars))

    def rstrip(self, chars: str | None = None) -> _DatasetStr:
        return _DatasetStr(super().rstrip(chars))

    def lower(self) -> _DatasetStr:
        return _DatasetStr(super().lower())

    def upper(self) -> _DatasetStr:
        return _DatasetStr(super().upper())

    def title(self) -> _DatasetStr:
        return _DatasetStr(super().title())

    def replace(self, old: str, new: str, count: int = -1) -> _DatasetStr:
        return _DatasetStr(super().replace(old, new, count))


def _plain_str(value: str) -> str:
    return str.__str__(value) if isinstance(value, _DatasetStr) else value


def _safe_stringify(value: Any, *, max_len: int = MAX_STR_LEN, max_depth: int = 12) -> str:
    """有界字符串化：按最大深度与最大长度遍历容器，阻止深层 DAG 在 C 层一次性分配超限内存。"""
    if isinstance(value, str):
        if len(value) > max_len:
            raise SandboxError(f"字符串长度不能超过 {MAX_STR_LEN}")
        return _plain_str(value) if isinstance(value, _DatasetStr) else value
    if value is None or isinstance(value, (bool, int, float, Decimal)):
        if isinstance(value, int) and not isinstance(value, bool) and value.bit_length() > MAX_INT_BITS:
            raise SandboxError(f"整数运算结果位数不能超过 {MAX_INT_BITS}")
        s = str(value)
        if len(s) > max_len:
            raise SandboxError(f"字符串长度不能超过 {MAX_STR_LEN}")
        return s

    parts: list[str] = []
    total = 0

    def emit(chunk: str) -> None:
        nonlocal total
        total += len(chunk)
        if total > max_len:
            raise SandboxError(f"字符串长度不能超过 {MAX_STR_LEN}")
        parts.append(chunk)

    def walk(node: Any, depth: int) -> None:
        if depth > max_depth:
            raise SandboxError(f"数据结构嵌套深度不能超过 {max_depth}")
        if isinstance(node, _DatasetStr):
            emit(repr(_plain_str(node)))
            return
        if isinstance(node, str) or node is None or isinstance(node, (bool, int, float, Decimal)):
            if isinstance(node, int) and not isinstance(node, bool) and node.bit_length() > MAX_INT_BITS:
                raise SandboxError(f"整数运算结果位数不能超过 {MAX_INT_BITS}")
            emit(repr(node) if isinstance(node, str) else str(node))
            return
        if isinstance(node, list):
            emit("[")
            for idx, item in enumerate(node):
                if idx:
                    emit(", ")
                walk(item, depth + 1)
            emit("]")
            return
        if isinstance(node, tuple):
            emit("(")
            for idx, item in enumerate(node):
                if idx:
                    emit(", ")
                walk(item, depth + 1)
            if len(node) == 1:
                emit(",")
            emit(")")
            return
        if isinstance(node, dict):
            emit("{")
            for idx, (k, v) in enumerate(node.items()):
                if idx:
                    emit(", ")
                walk(k, depth + 1)
                emit(": ")
                walk(v, depth + 1)
            emit("}")
            return
        if isinstance(node, set):
            if not node:
                emit("set()")
                return
            emit("{")
            for idx, item in enumerate(node):
                if idx:
                    emit(", ")
                walk(item, depth + 1)
            emit("}")
            return
        emit(str(node))

    walk(value, 0)
    return "".join(parts)


def _safe_equal(left: Any, right: Any, *, max_nodes: int = 20_000, max_depth: int = 16) -> bool:
    """有界结构相等性比较，避免共享子节点的深层 DAG 触发指数级比较阻塞。"""
    stack: list[tuple[Any, Any, int]] = [(left, right, 0)]
    seen = 0
    while stack:
        a, b, depth = stack.pop()
        seen += 1
        if seen > max_nodes:
            raise SandboxError("容器比较规模超过上限")
        if depth > max_depth:
            raise SandboxError(f"数据结构嵌套深度不能超过 {max_depth}")
        if a is b:
            continue
        if isinstance(a, list) and isinstance(b, list):
            if len(a) != len(b):
                return False
            for x, y in zip(a, b):
                stack.append((x, y, depth + 1))
            continue
        if isinstance(a, tuple) and isinstance(b, tuple):
            if len(a) != len(b):
                return False
            for x, y in zip(a, b):
                stack.append((x, y, depth + 1))
            continue
        if isinstance(a, dict) and isinstance(b, dict):
            if len(a) != len(b) or a.keys() != b.keys():
                return False
            for k in a:
                stack.append((a[k], b[k], depth + 1))
            continue
        if a != b:
            return False
    return True


def _wrap_dataset_value(value: Any) -> Any:
    if isinstance(value, _DatasetStr):
        return value
    if isinstance(value, str):
        return _DatasetStr(value)
    if isinstance(value, list):
        return [_wrap_dataset_value(v) for v in value]
    if isinstance(value, tuple):
        return tuple(_wrap_dataset_value(v) for v in value)
    if isinstance(value, set):
        return {_wrap_dataset_value(v) for v in value}
    if isinstance(value, dict):
        return {(_plain_str(k) if isinstance(k, str) else k): _wrap_dataset_value(v) for k, v in value.items()}
    return value


def _validate_format_spec(fmt: str) -> None:
    if len(fmt) > 64:
        raise SandboxError("格式说明符过长")
    rest = fmt
    if len(rest) >= 2 and rest[1] in "<>=^":
        rest = rest[2:]
    elif rest and rest[0] in "<>=^":
        rest = rest[1:]
    for num in re.findall(r"\d+", rest):
        if int(num) > MAX_FORMAT_WIDTH:
            raise SandboxError(f"格式化宽度或精度不能超过 {MAX_FORMAT_WIDTH}")


def validate_ast(code: str) -> tuple[ast.Module, list[str]]:
    """校验 AST 白名单并收集不在 measures.CONSTANTS 中的数值字面量。"""
    if not code or not code.strip():
        raise SandboxError("沙箱计算代码不能为空")
    if len(code) > MAX_CODE_CHARS:
        raise SandboxError(f"沙箱计算代码超过 {MAX_CODE_CHARS} 个字符上限")
    try:
        tree = ast.parse(code, mode="exec")
    except SyntaxError as exc:
        raise SandboxError(f"沙箱代码语法错误：{exc.msg}（第 {exc.lineno} 行）") from None

    node_count = 0
    non_whitelist: list[str] = []

    def check(node: ast.AST, depth: int) -> None:
        nonlocal node_count
        node_count += 1
        if node_count > MAX_AST_NODES:
            raise SandboxError(f"沙箱代码过于复杂（超过 {MAX_AST_NODES} 个语法节点）")
        if depth > MAX_AST_DEPTH:
            raise SandboxError(f"沙箱代码嵌套超过 {MAX_AST_DEPTH} 层")

        if isinstance(
            node,
            (
                *_STMT_TYPES,
                *_EXPR_TYPES,
                *_BINOPS,
                *_UNARYOPS,
                *_BOOLOPS,
                *_CMPOPS,
                ast.arguments,
                ast.arg,
                ast.keyword,
            ),
        ):
            pass
        else:
            raise SandboxError(f"不允许的语法结构：{type(node).__name__}")

        if isinstance(node, ast.Name) and node.id.startswith("_"):
            raise SandboxError(f"不允许访问下划线开头标识符：{node.id}")
        if isinstance(node, ast.Attribute) and node.attr.startswith("_"):
            raise SandboxError(f"不允许访问私有或双下划线属性：{node.attr}")
        if isinstance(node, ast.arg) and node.arg.startswith("_"):
            raise SandboxError(f"不允许下划线开头的参数名：{node.arg}")
        if isinstance(node, ast.Lambda):
            args = node.args
            if args.vararg or args.kwarg or args.posonlyargs or args.kwonlyargs:
                raise SandboxError("lambda 仅支持简单位置参数")
        if isinstance(node, ast.Call):
            if any(isinstance(a, ast.Starred) for a in node.args) or any(k.arg is None for k in node.keywords):
                raise SandboxError("沙箱调用不支持 *args / **kwargs 解包")
        if isinstance(node, ast.Constant):
            val = node.value
            if val is not None and not isinstance(val, (bool, int, float, str)):
                raise SandboxError(f"不允许的常量类型：{type(val).__name__}")
            if isinstance(val, (int, float)) and not isinstance(val, bool):
                try:
                    dec = Decimal(str(val))
                except (InvalidOperation, ValueError):
                    raise SandboxError(f"非法数值常量：{val!r}") from None
                if not dec.is_finite():
                    raise SandboxError(f"不允许非有限数值：{val!r}")
                if dec not in measures.CONSTANTS:
                    non_whitelist.append(str(val))
        if isinstance(node, ast.JoinedStr) and len(node.values) > 32:
            raise SandboxError("格式化字符串片段过多")

        for child in ast.iter_child_nodes(node):
            check(child, depth + 1)

    check(tree, 0)
    return tree, non_whitelist


class _Interpreter:
    def __init__(
        self,
        datasets: dict[str, list[dict[str, Any]]],
        *,
        timeout_seconds: float,
    ):
        if timeout_seconds <= 0:
            raise SandboxError("沙箱超时值必须为正数")
        self.deadline = time.monotonic() + timeout_seconds
        self.steps = 0
        self.row_provenance: dict[int, str] = {}
        self.row_dataset: dict[int, str] = {}
        self.accessed_evidence: dict[str, None] = {}
        self.accessed_keys: dict[str, set[str]] = {ref: set() for ref in datasets}
        self.accessed_all_keys: set[str] = set()
        self.runtime_non_whitelist: list[str] = []

        copied_datasets: dict[str, list[dict[str, Any]]] = {}
        for ref, rows in datasets.items():
            copied_rows: list[dict[str, Any]] = []
            for idx, row in enumerate(rows, 1):
                rc = {
                    (_plain_str(k) if isinstance(k, str) else k): _wrap_dataset_value(copy.deepcopy(v))
                    for k, v in dict(row).items()
                }
                eid = f"{ref}#{idx}"
                self.row_provenance[id(rc)] = eid
                self.row_dataset[id(rc)] = ref
                copied_rows.append(rc)
            copied_datasets[ref] = copied_rows

        def evidence_id(row: Any) -> str | None:
            if not isinstance(row, dict):
                return None
            eid = self.row_provenance.get(id(row))
            if eid is not None:
                self.accessed_evidence.setdefault(eid, None)
            return eid

        def safe_range(*args: Any) -> range:
            r = range(*args)
            if len(r) > MAX_RANGE:
                raise SandboxError(f"range 长度不能超过 {MAX_RANGE}")
            return r

        def _check_script_numeric_str(raw_val: Any, parsed: Decimal) -> None:
            if isinstance(raw_val, str) and not isinstance(raw_val, _DatasetStr):
                if parsed not in measures.CONSTANTS:
                    self.runtime_non_whitelist.append(_plain_str(raw_val))

        def safe_decimal(value: Any = "0") -> Decimal:
            if isinstance(value, Decimal):
                return value
            if isinstance(value, bool):
                raise SandboxError("不能将布尔值直接转为 Decimal")
            try:
                d = Decimal(str(value))
            except (InvalidOperation, ValueError):
                raise SandboxError(f"无法转为 Decimal：{value!r}") from None
            if not d.is_finite():
                raise SandboxError(f"非有限数值：{value!r}")
            _check_script_numeric_str(value, d)
            return d

        def safe_float(value: Any = 0.0) -> float:
            if isinstance(value, bool):
                return float(value)
            res = float(value)
            if not math.isfinite(res):
                raise SandboxError(f"非有限数值：{value!r}")
            if isinstance(value, str) and not isinstance(value, _DatasetStr):
                try:
                    dec = Decimal(str(res))
                except (InvalidOperation, ValueError):
                    raise SandboxError(f"非有限数值：{value!r}") from None
                _check_script_numeric_str(value, dec)
            return res

        def safe_int(*args: Any, **kwargs: Any) -> int:
            if (
                args
                and isinstance(args[0], Decimal)
                and (not args[0].is_finite() or args[0].adjusted() > _MAX_DECIMAL_INT_EXP)
            ):
                raise SandboxError(f"整数运算结果位数不能超过 {MAX_INT_BITS}")
            if args and isinstance(args[0], str) and len(args[0]) > MAX_INT_BITS + 4:
                raise SandboxError(f"整数运算结果位数不能超过 {MAX_INT_BITS}")
            res = int(*args, **kwargs)
            if res.bit_length() > MAX_INT_BITS:
                raise SandboxError(f"整数运算结果位数不能超过 {MAX_INT_BITS}")
            if args and isinstance(args[0], str) and not isinstance(args[0], _DatasetStr):
                _check_script_numeric_str(args[0], Decimal(res))
            return res

        def safe_number(value: Any) -> Decimal | None:
            res = calc.number(value)
            if res is not None:
                if not res.is_finite():
                    raise SandboxError(f"非有限数值：{value!r}")
                _check_script_numeric_str(value, res)
            return res

        def safe_str(value: Any = "") -> str:
            if isinstance(value, _DatasetStr):
                return value
            return _safe_stringify(value)

        def safe_sum(iterable: Any, start: Any = 0) -> Any:
            if isinstance(start, (str, bytes, list, tuple, dict, set)) or not _is_numeric_val(start):
                raise SandboxError("sum() 仅支持数值累加")
            acc = start
            for item in iterable:
                self._tick()
                if isinstance(item, (str, bytes, list, tuple, dict, set)) or not _is_numeric_val(item):
                    raise SandboxError("sum() 仅支持数值累加")
                acc = acc + item
                if isinstance(acc, int) and not isinstance(acc, bool) and acc.bit_length() > MAX_INT_BITS:
                    raise SandboxError(f"整数运算结果位数不能超过 {MAX_INT_BITS}")
                if isinstance(acc, float) and not math.isfinite(acc):
                    raise SandboxError("sum() 结果超出有限数值范围")
            return acc

        def safe_dict(*args: Any, **kwargs: Any) -> dict[Any, Any]:
            if args and isinstance(args[0], dict) and id(args[0]) in self.row_dataset:
                self.accessed_all_keys.add(self.row_dataset[id(args[0])])
            return dict(*args, **kwargs)

        def safe_sorted(iterable: Any, *, key: Any = None, reverse: bool = False) -> list[Any]:
            return sorted(iterable, key=key, reverse=reverse)

        def safe_round(number_val: Any, ndigits: int | None = None) -> Any:
            if (
                ndigits is None
                and isinstance(number_val, Decimal)
                and (not number_val.is_finite() or number_val.adjusted() > _MAX_DECIMAL_INT_EXP)
            ):
                raise SandboxError(f"整数运算结果位数不能超过 {MAX_INT_BITS}")
            res = round(number_val) if ndigits is None else round(number_val, ndigits)
            if isinstance(res, int) and not isinstance(res, bool) and res.bit_length() > MAX_INT_BITS:
                raise SandboxError(f"整数运算结果位数不能超过 {MAX_INT_BITS}")
            return res

        def safe_sqrt(x: Any) -> float:
            return math.sqrt(safe_float(x))

        def safe_log(x: Any, base: Any = None) -> float:
            return math.log(safe_float(x)) if base is None else math.log(safe_float(x), safe_float(base))

        def safe_log10(x: Any) -> float:
            return math.log10(safe_float(x))

        def safe_exp(x: Any) -> float:
            return math.exp(safe_float(x))

        def safe_floor(x: Any) -> int:
            return math.floor(safe_float(x))

        def safe_ceil(x: Any) -> int:
            return math.ceil(safe_float(x))

        def safe_mean(vals: Any) -> float:
            return statistics.mean(safe_float(v) for v in vals)

        def safe_median(vals: Any) -> float:
            return statistics.median(safe_float(v) for v in vals)

        def safe_stdev(vals: Any) -> float:
            return statistics.stdev(safe_float(v) for v in vals)

        self.builtins: dict[str, Any] = {
            "len": len,
            "sum": safe_sum,
            "min": min,
            "max": max,
            "abs": abs,
            "round": safe_round,
            "sorted": safe_sorted,
            "enumerate": enumerate,
            "zip": zip,
            "reversed": reversed,
            "range": safe_range,
            "list": list,
            "dict": safe_dict,
            "set": set,
            "tuple": tuple,
            "int": safe_int,
            "float": safe_float,
            "str": safe_str,
            "bool": bool,
            "any": any,
            "all": all,
            "Decimal": safe_decimal,
            "number": safe_number,
            "sqrt": safe_sqrt,
            "log": safe_log,
            "log10": safe_log10,
            "exp": safe_exp,
            "floor": safe_floor,
            "ceil": safe_ceil,
            "mean": safe_mean,
            "median": safe_median,
            "stdev": safe_stdev,
            "evidence_id": evidence_id,
            "True": True,
            "False": False,
            "None": None,
        }
        self.env: dict[str, Any] = {
            "datasets": copied_datasets,
            **copied_datasets,
        }
        if len(copied_datasets) == 1:
            self.env["rows"] = next(iter(copied_datasets.values()))
        self.last_expr: Any = _SENTINEL

    def _tick(self) -> None:
        self.steps += 1
        if self.steps > MAX_STEPS:
            raise SandboxError(f"沙箱执行步数超过上限 {MAX_STEPS}")
        if (self.steps & 15) == 0 and time.monotonic() > self.deadline:
            raise SandboxError("沙箱计算超时")

    def _touch_row(self, obj: Any) -> None:
        if isinstance(obj, dict):
            eid = self.row_provenance.get(id(obj))
            if eid is not None:
                self.accessed_evidence.setdefault(eid, None)

    def exec_module(self, tree: ast.Module) -> Any:
        for stmt in tree.body:
            self.exec_stmt(stmt, self.env)
        if "result" in self.env:
            return self.env["result"]
        if self.last_expr is not _SENTINEL:
            return self.last_expr
        raise SandboxError("沙箱代码必须赋值给 result 或包含可求值的表达式")

    def exec_stmt(self, stmt: ast.AST, env: dict[str, Any]) -> None:
        self._tick()
        if isinstance(stmt, ast.Pass):
            return
        if isinstance(stmt, ast.Break):
            raise _BreakSignal()
        if isinstance(stmt, ast.Continue):
            raise _ContinueSignal()
        if isinstance(stmt, ast.Expr):
            self.last_expr = self.eval_expr(stmt.value, env)
            return
        if isinstance(stmt, ast.Assign):
            val = self.eval_expr(stmt.value, env)
            for target in stmt.targets:
                self._assign(target, val, env)
            return
        if isinstance(stmt, ast.AugAssign):
            cur = self.eval_expr(stmt.target, env)
            rhs = self.eval_expr(stmt.value, env)
            new_val = self._binop(stmt.op, cur, rhs)
            self._assign(stmt.target, new_val, env)
            return
        if isinstance(stmt, ast.If):
            branch = stmt.body if self.eval_expr(stmt.test, env) else stmt.orelse
            for child in branch:
                self.exec_stmt(child, env)
            return
        if isinstance(stmt, ast.For):
            iterable = self.eval_expr(stmt.iter, env)
            for item in iterable:
                self._tick()
                self._assign(stmt.target, item, env)
                try:
                    for child in stmt.body:
                        self.exec_stmt(child, env)
                except _ContinueSignal:
                    continue
                except _BreakSignal:
                    return
            for child in stmt.orelse:
                self.exec_stmt(child, env)
            return
        raise SandboxError(f"不支持的语句：{type(stmt).__name__}")

    def _assign(self, target: ast.AST, value: Any, env: dict[str, Any]) -> None:
        if isinstance(target, ast.Name):
            if target.id in self.builtins:
                raise SandboxError(f"不允许覆盖内置名称：{target.id}")
            env[target.id] = value
            return
        if isinstance(target, (ast.Tuple, ast.List)):
            items = list(value)
            if len(items) != len(target.elts):
                raise SandboxError("解包赋值元素数量不匹配")
            for sub, item in zip(target.elts, items):
                self._assign(sub, item, env)
            return
        if isinstance(target, ast.Subscript):
            container = self.eval_expr(target.value, env)
            idx = self._eval_slice_or_key(target.slice, env)
            if isinstance(container, list):
                if isinstance(idx, slice):
                    rhs: list[Any] = []
                    for item in value:
                        self._tick()
                        rhs.append(item)
                        if len(rhs) > MAX_SEQ_LEN:
                            raise SandboxError(f"序列长度不能超过 {MAX_SEQ_LEN}")
                    start, stop, step = idx.indices(len(container))
                    replaced = len(range(start, stop, step))
                    if len(container) - replaced + len(rhs) > MAX_SEQ_LEN:
                        raise SandboxError(f"序列长度不能超过 {MAX_SEQ_LEN}")
                    container[idx] = rhs
                    return
                container[idx] = value
                return
            if isinstance(container, dict):
                if idx not in container and len(container) + 1 > MAX_SEQ_LEN:
                    raise SandboxError(f"序列长度不能超过 {MAX_SEQ_LEN}")
                container[idx] = value
                return
            raise SandboxError("仅支持对 dict / list 下标赋值")
        raise SandboxError(f"不支持的赋值目标：{type(target).__name__}")

    def _eval_slice_or_key(self, node: ast.AST, env: dict[str, Any]) -> Any:
        if isinstance(node, ast.Slice):
            lower = self.eval_expr(node.lower, env) if node.lower else None
            upper = self.eval_expr(node.upper, env) if node.upper else None
            step = self.eval_expr(node.step, env) if node.step else None
            return slice(lower, upper, step)
        return self.eval_expr(node, env)

    def eval_expr(self, expr: ast.AST, env: dict[str, Any]) -> Any:
        self._tick()
        if isinstance(expr, ast.Constant):
            return expr.value
        if isinstance(expr, ast.Name):
            if expr.id in env:
                return env[expr.id]
            if expr.id in self.builtins:
                return self.builtins[expr.id]
            raise SandboxError(f"未定义的变量：{expr.id}")
        if isinstance(expr, ast.List):
            return [self.eval_expr(e, env) for e in expr.elts]
        if isinstance(expr, ast.Tuple):
            return tuple(self.eval_expr(e, env) for e in expr.elts)
        if isinstance(expr, ast.Set):
            return {self.eval_expr(e, env) for e in expr.elts}
        if isinstance(expr, ast.Dict):
            if any(k is None for k in expr.keys):
                raise SandboxError("沙箱字典字面量不支持 ** 解包")
            return {self.eval_expr(k, env): self.eval_expr(v, env) for k, v in zip(expr.keys, expr.values)}
        if isinstance(expr, ast.UnaryOp):
            operand = self.eval_expr(expr.operand, env)
            if isinstance(expr.op, ast.UAdd):
                return +operand
            if isinstance(expr.op, ast.USub):
                return -operand
            if isinstance(expr.op, ast.Not):
                return not operand
        if isinstance(expr, ast.BinOp):
            left = self.eval_expr(expr.left, env)
            right = self.eval_expr(expr.right, env)
            return self._binop(expr.op, left, right)
        if isinstance(expr, ast.BoolOp):
            if isinstance(expr.op, ast.And):
                val: Any = True
                for item in expr.values:
                    val = self.eval_expr(item, env)
                    if not val:
                        return val
                return val
            val = False
            for item in expr.values:
                val = self.eval_expr(item, env)
                if val:
                    return val
            return val
        if isinstance(expr, ast.Compare):
            left = self.eval_expr(expr.left, env)
            for op, comp in zip(expr.ops, expr.comparators):
                right = self.eval_expr(comp, env)
                if not self._cmp(op, left, right):
                    return False
                left = right
            return True
        if isinstance(expr, ast.IfExp):
            return self.eval_expr(expr.body if self.eval_expr(expr.test, env) else expr.orelse, env)
        if isinstance(expr, ast.Subscript):
            container = self.eval_expr(expr.value, env)
            self._touch_row(container)
            key = self._eval_slice_or_key(expr.slice, env)
            val = container[key]
            if isinstance(container, dict) and id(container) in self.row_dataset and isinstance(key, str):
                ds_ref = self.row_dataset[id(container)]
                self.accessed_keys.setdefault(ds_ref, set()).add(_plain_str(key))
            return val
        if isinstance(expr, ast.ListComp):
            return list(self._iter_comp(expr.generators, 0, env, lambda e: self.eval_expr(expr.elt, e)))
        if isinstance(expr, ast.SetComp):
            return set(self._iter_comp(expr.generators, 0, env, lambda e: self.eval_expr(expr.elt, e)))
        if isinstance(expr, ast.GeneratorExp):
            return list(self._iter_comp(expr.generators, 0, env, lambda e: self.eval_expr(expr.elt, e)))
        if isinstance(expr, ast.DictComp):
            return dict(
                self._iter_comp(
                    expr.generators,
                    0,
                    env,
                    lambda e: (self.eval_expr(expr.key, e), self.eval_expr(expr.value, e)),
                )
            )
        if isinstance(expr, ast.Lambda):
            arg_names = [a.arg for a in expr.args.args]
            defaults = [self.eval_expr(d, env) for d in expr.args.defaults]
            captured = dict(env)

            def fn(*call_args: Any) -> Any:
                self._tick()
                if len(call_args) > len(arg_names):
                    raise SandboxError("lambda 参数过多")
                local = dict(captured)
                offset = len(arg_names) - len(defaults)
                for i, name in enumerate(arg_names):
                    if i < len(call_args):
                        local[name] = call_args[i]
                    elif i >= offset:
                        local[name] = defaults[i - offset]
                    else:
                        raise SandboxError(f"lambda 缺少参数：{name}")
                return self.eval_expr(expr.body, local)

            return fn
        if isinstance(expr, ast.Attribute):
            target = self.eval_expr(expr.value, env)
            self._touch_row(target)
            allowed = next((methods for cls, methods in _ALLOWED_METHODS.items() if isinstance(target, cls)), None)
            if allowed is None or expr.attr not in allowed:
                raise SandboxError(f"不允许在 {type(target).__name__} 上访问属性或方法 {expr.attr}")
            if isinstance(target, dict):
                ds_ref = self.row_dataset.get(id(target))
                if ds_ref is not None and expr.attr == "get":

                    def safe_row_get(key: Any, default: Any = None) -> Any:
                        if isinstance(key, str):
                            self.accessed_keys.setdefault(ds_ref, set()).add(_plain_str(key))
                        return target.get(key, default)

                    return safe_row_get
                if ds_ref is not None and expr.attr in ("values", "items", "copy"):
                    self.accessed_all_keys.add(ds_ref)
                if expr.attr == "setdefault":

                    def safe_dict_setdefault(key: Any, default: Any = None) -> Any:
                        if ds_ref is not None and isinstance(key, str):
                            self.accessed_keys.setdefault(ds_ref, set()).add(_plain_str(key))
                        if key not in target and len(target) + 1 > MAX_SEQ_LEN:
                            raise SandboxError(f"序列长度不能超过 {MAX_SEQ_LEN}")
                        return target.setdefault(key, default)

                    return safe_dict_setdefault
                if expr.attr == "update":

                    def safe_dict_update(*args: Any, **kwargs: Any) -> None:
                        if len(args) > 1:
                            raise TypeError(f"update expected at most 1 arguments, got {len(args)}")
                        if ds_ref is not None:
                            self.accessed_all_keys.add(ds_ref)
                        pairs: list[tuple[Any, Any]] = []
                        if args:
                            src = args[0]
                            raw_items = src.items() if isinstance(src, dict) or hasattr(src, "items") else src
                            for pair in raw_items:
                                self._tick()
                                k, v = pair
                                pairs.append((k, v))
                        for k, v in kwargs.items():
                            self._tick()
                            pairs.append((k, v))
                        for k, v in pairs:
                            if k not in target and len(target) + 1 > MAX_SEQ_LEN:
                                raise SandboxError(f"序列长度不能超过 {MAX_SEQ_LEN}")
                            target[k] = v

                    return safe_dict_update
                if expr.attr == "pop":

                    def safe_dict_pop(key: Any, *default: Any) -> Any:
                        if len(default) > 1:
                            raise TypeError(f"pop expected at most 2 arguments, got {1 + len(default)}")
                        if ds_ref is not None and isinstance(key, str):
                            self.accessed_keys.setdefault(ds_ref, set()).add(_plain_str(key))
                        return target.pop(key, *default)

                    return safe_dict_pop
            if isinstance(target, list):
                if expr.attr == "append":

                    def safe_append(item: Any) -> None:
                        if len(target) + 1 > MAX_SEQ_LEN:
                            raise SandboxError(f"序列长度不能超过 {MAX_SEQ_LEN}")
                        target.append(item)

                    return safe_append
                if expr.attr == "insert":

                    def safe_insert(index: int, item: Any) -> None:
                        if len(target) + 1 > MAX_SEQ_LEN:
                            raise SandboxError(f"序列长度不能超过 {MAX_SEQ_LEN}")
                        target.insert(index, item)

                    return safe_insert
                if expr.attr == "extend":

                    def safe_extend(items: Any) -> None:
                        buf = []
                        for item in items:
                            if len(target) + len(buf) + 1 > MAX_SEQ_LEN:
                                raise SandboxError(f"序列长度不能超过 {MAX_SEQ_LEN}")
                            buf.append(item)
                        target.extend(buf)

                    return safe_extend
            if isinstance(target, set):
                if expr.attr == "add":

                    def safe_set_add(item: Any) -> None:
                        if item not in target and len(target) + 1 > MAX_SEQ_LEN:
                            raise SandboxError(f"序列长度不能超过 {MAX_SEQ_LEN}")
                        target.add(item)

                    return safe_set_add
                if expr.attr == "update":

                    def safe_set_update(*others: Any) -> None:
                        for other in others:
                            for item in other:
                                self._tick()
                                if item not in target and len(target) + 1 > MAX_SEQ_LEN:
                                    raise SandboxError(f"序列长度不能超过 {MAX_SEQ_LEN}")
                                target.add(item)

                    return safe_set_update
                if expr.attr in ("union", "intersection", "difference"):
                    orig_set_fn = getattr(target, expr.attr)

                    def safe_set_op(*others: Any) -> set[Any]:
                        res = orig_set_fn(*others)
                        if len(res) > MAX_SEQ_LEN:
                            raise SandboxError(f"序列长度不能超过 {MAX_SEQ_LEN}")
                        return res

                    return safe_set_op
            if isinstance(target, str):
                if expr.attr == "join":

                    def safe_join(items: Any) -> str:
                        sep_len = len(target)
                        parts: list[str] = []
                        total = 0
                        for i, item in enumerate(items):
                            if not isinstance(item, str):
                                raise TypeError(
                                    f"sequence item {i}: expected str instance, {type(item).__name__} found"
                                )
                            total += len(item) + (sep_len if i > 0 else 0)
                            if total > MAX_STR_LEN:
                                raise SandboxError(f"字符串长度不能超过 {MAX_STR_LEN}")
                            parts.append(item)
                        return target.join(parts)

                    return safe_join
                if expr.attr == "replace":

                    def safe_replace(old: str, new: str, count: int = -1) -> str:
                        if not isinstance(old, str) or not isinstance(new, str) or not isinstance(count, int):
                            return target.replace(old, new, count)
                        if len(new) > len(old) and count != 0:
                            occ = target.count(old) if old else len(target) + 1
                            if count > 0:
                                occ = min(occ, count)
                            predicted = len(target) + occ * (len(new) - len(old))
                            if predicted > MAX_STR_LEN:
                                raise SandboxError(f"字符串长度不能超过 {MAX_STR_LEN}")
                        res = target.replace(old, new, count)
                        if len(res) > MAX_STR_LEN:
                            raise SandboxError(f"字符串长度不能超过 {MAX_STR_LEN}")
                        return res

                    return safe_replace
            return getattr(target, expr.attr)
        if isinstance(expr, ast.Call):
            fn = self.eval_expr(expr.func, env)
            if not callable(fn):
                raise SandboxError("目标不可调用")
            pos_args = [self.eval_expr(a, env) for a in expr.args]
            kw_args = {k.arg: self.eval_expr(k.value, env) for k in expr.keywords if k.arg is not None}
            return fn(*pos_args, **kw_args)
        if isinstance(expr, ast.JoinedStr):
            parts = []
            total_len = 0
            for v in expr.values:
                if isinstance(v, ast.Constant):
                    part = str(v.value)
                elif isinstance(v, ast.FormattedValue):
                    val = self.eval_expr(v.value, env)
                    fmt = self.eval_expr(v.format_spec, env) if v.format_spec else ""
                    if fmt:
                        if not isinstance(fmt, str):
                            raise SandboxError("格式说明符必须是字符串")
                        _validate_format_spec(fmt)
                        if (
                            isinstance(val, Decimal)
                            and ("f" in fmt.lower() or "%" in fmt)
                            and val.is_finite()
                            and abs(val.adjusted()) > MAX_STR_LEN
                        ):
                            raise SandboxError(f"格式化字符串长度不能超过 {MAX_STR_LEN}")
                        part = format(val, fmt)
                    else:
                        part = _safe_stringify(val)
                else:
                    raise SandboxError("不支持的格式化字符串节点")
                total_len += len(part)
                if total_len > MAX_STR_LEN:
                    raise SandboxError(f"格式化字符串长度不能超过 {MAX_STR_LEN}")
                parts.append(part)
            return "".join(parts)
        raise SandboxError(f"不支持的表达式：{type(expr).__name__}")

    def _iter_comp(self, generators: list[ast.comprehension], idx: int, env: dict[str, Any], leaf: Any):
        if idx >= len(generators):
            yield leaf(env)
            return
        gen = generators[idx]
        if gen.is_async:
            raise SandboxError("沙箱不支持异步推导式")
        iterable = self.eval_expr(gen.iter, env)
        for item in iterable:
            self._tick()
            local = dict(env)
            self._assign(gen.target, item, local)
            if all(self.eval_expr(cond, local) for cond in gen.ifs):
                yield from self._iter_comp(generators, idx + 1, local, leaf)

    @staticmethod
    def _binop(op: ast.operator, left: Any, right: Any) -> Any:
        if isinstance(op, ast.Pow):
            if isinstance(right, (int, float, Decimal)) and abs(right) > MAX_POW_EXP:
                raise SandboxError(f"幂指数绝对值不能超过 {MAX_POW_EXP}")
            if (
                isinstance(left, int)
                and not isinstance(left, bool)
                and isinstance(right, int)
                and not isinstance(right, bool)
                and right >= 0
                and left not in (0, 1, -1)
                and left.bit_length() * right > MAX_INT_BITS
            ):
                raise SandboxError(f"整数运算结果位数不能超过 {MAX_INT_BITS}")
            return left**right
        if isinstance(op, ast.Add):
            if isinstance(left, str) and isinstance(right, str):
                if len(left) + len(right) > MAX_STR_LEN:
                    raise SandboxError(f"字符串长度不能超过 {MAX_STR_LEN}")
            elif (isinstance(left, list) and isinstance(right, list)) or (
                isinstance(left, tuple) and isinstance(right, tuple)
            ):
                if len(left) + len(right) > MAX_SEQ_LEN:
                    raise SandboxError(f"序列长度不能超过 {MAX_SEQ_LEN}")
            return left + right
        if isinstance(op, ast.Sub):
            return left - right
        if isinstance(op, ast.Mult):
            seq = None
            count = None
            if isinstance(left, (list, tuple, str)) and isinstance(right, int):
                seq, count = left, right
            elif isinstance(right, (list, tuple, str)) and isinstance(left, int):
                seq, count = right, left
            if seq is not None and count is not None:
                limit = MAX_STR_LEN if isinstance(seq, str) else MAX_SEQ_LEN
                if count < 0:
                    raise SandboxError("序列或字符串重复次数不能为负数")
                if count > limit or (len(seq) > 0 and len(seq) * count > limit):
                    kind_label = "字符串" if isinstance(seq, str) else "序列"
                    raise SandboxError(f"{kind_label}长度不能超过 {limit}")
            if (
                isinstance(left, int)
                and not isinstance(left, bool)
                and isinstance(right, int)
                and not isinstance(right, bool)
                and left != 0
                and right != 0
                and left.bit_length() + right.bit_length() > MAX_INT_BITS
            ):
                raise SandboxError(f"整数运算结果位数不能超过 {MAX_INT_BITS}")
            return left * right
        if isinstance(op, ast.Div):
            return left / right
        if isinstance(op, ast.FloorDiv):
            return left // right
        if isinstance(op, ast.Mod):
            return left % right
        raise SandboxError(f"不支持的二元运算：{type(op).__name__}")

    @staticmethod
    def _cmp(op: ast.cmpop, left: Any, right: Any) -> bool:
        if isinstance(op, ast.Eq):
            return _safe_equal(left, right)
        if isinstance(op, ast.NotEq):
            return not _safe_equal(left, right)
        if isinstance(op, ast.Lt):
            return left < right
        if isinstance(op, ast.LtE):
            return left <= right
        if isinstance(op, ast.Gt):
            return left > right
        if isinstance(op, ast.GtE):
            return left >= right
        if isinstance(op, ast.In):
            return left in right
        if isinstance(op, ast.NotIn):
            return left not in right
        if isinstance(op, ast.Is):
            return left is right
        if isinstance(op, ast.IsNot):
            return left is not right
        raise SandboxError(f"不支持的比较运算：{type(op).__name__}")


def _norm_cell(value: Any) -> Any:
    if isinstance(value, _DatasetStr):
        return _plain_str(value)
    if value is None or isinstance(value, (bool, int, str, Decimal)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise SandboxError("沙箱结果不能包含 NaN 或 Inf")
        return Decimal(str(value))
    return str(value)


def _is_numeric_val(value: Any) -> bool:
    return isinstance(value, (int, float, Decimal)) and not isinstance(value, bool)


def run(
    code: str,
    datasets: dict[str, list[dict[str, Any]]],
    *,
    timeout_seconds: float = 5.0,
) -> SandboxResult:
    """在只读 AST 沙箱中执行计算脚本，返回规范化的列、行、汇总、量纲与行级来源。"""
    tree, non_whitelist = validate_ast(code)
    interp = _Interpreter(datasets, timeout_seconds=timeout_seconds)
    columns: list[str]
    table_rows: list[list[Any]]
    summary: dict[str, Any]

    try:
        raw = interp.exec_module(tree)
        if isinstance(raw, dict):
            if not raw:
                raise SandboxError("沙箱返回的字典结果不能为空")
            columns = [_plain_str(str(k)) for k in raw.keys()]
            if len(columns) > MAX_OUTPUT_COLS:
                raise SandboxError(f"沙箱输出列数超过 {MAX_OUTPUT_COLS} 上限")
            table_rows = [[_norm_cell(raw[k]) for k in raw.keys()]]
            summary = (
                {"value": table_rows[0][columns.index("value")]}
                if "value" in columns
                else {k: v for k, v in zip(columns[:10], table_rows[0][:10])}
            )
        elif isinstance(raw, list):
            if len(raw) > MAX_OUTPUT_ROWS:
                raise SandboxError(f"沙箱输出行数超过 {MAX_OUTPUT_ROWS} 上限")
            if not raw:
                columns = ["value"]
                table_rows = []
                summary = {"row_count": 0}
            elif all(isinstance(item, dict) for item in raw):
                seen: dict[str, None] = {}
                for item in raw:
                    for k in item.keys():
                        seen.setdefault(_plain_str(str(k)), None)
                columns = list(seen)
                if not columns:
                    raise SandboxError("沙箱输出行字典不能为空")
                if len(columns) > MAX_OUTPUT_COLS:
                    raise SandboxError(f"沙箱输出列数超过 {MAX_OUTPUT_COLS} 上限")
                table_rows = [[_norm_cell(item.get(col_name)) for col_name in columns] for item in raw]
                summary = {"row_count": len(table_rows)}
                if len(table_rows) == 1:
                    for k, v in zip(columns[:10], table_rows[0][:10]):
                        summary.setdefault(k, v)
            else:
                raise SandboxError("沙箱 result 为列表时，每个元素必须是字典（行记录）")
        elif _is_numeric_val(raw) or isinstance(raw, (str, bool)) or raw is None:
            val = _norm_cell(raw)
            columns = ["value"]
            table_rows = [[val]]
            summary = {"value": val}
        else:
            raise SandboxError(f"不支持的沙箱返回类型：{type(raw).__name__}")

        serialized = json.dumps({"columns": columns, "rows": table_rows}, ensure_ascii=False, default=str)
        if len(serialized) > MAX_OUTPUT_CHARS:
            raise SandboxError(f"沙箱输出体积超过 {MAX_OUTPUT_CHARS} 字符上限")
    except SandboxError:
        raise
    except ZeroDivisionError:
        raise SandboxError("沙箱计算发生除以零错误") from None
    except (
        KeyError,
        IndexError,
        TypeError,
        ValueError,
        ArithmeticError,
        RecursionError,
        MemoryError,
        OverflowError,
    ) as exc:
        raise SandboxError(f"沙箱计算出错：{type(exc).__name__}: {exc}") from None

    units: dict[str, dict[str, Any]] = {}
    for col_idx, col_name in enumerate(columns):
        vals = [row[col_idx] for row in table_rows if row[col_idx] is not None]
        if vals and all(_is_numeric_val(v) for v in vals):
            units[col_name] = dict(measures.UNKNOWN)

    summary_units = {
        k: ({"kind": "count"} if k == "row_count" else dict(measures.UNKNOWN))
        for k, v in summary.items()
        if _is_numeric_val(v)
    }
    all_non_whitelist = list(dict.fromkeys([*non_whitelist, *interp.runtime_non_whitelist]))
    return SandboxResult(
        columns=columns,
        rows=table_rows,
        summary=summary,
        units=units,
        summary_units=summary_units,
        assumption=bool(all_non_whitelist),
        source_evidence_ids=list(interp.accessed_evidence),
        non_whitelist_constants=all_non_whitelist,
        accessed_keys={ref: sorted(keys) for ref, keys in interp.accessed_keys.items() if keys},
        accessed_all_keys=sorted(interp.accessed_all_keys),
    )
