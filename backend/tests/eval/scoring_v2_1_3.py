"""分析评测的判分 v2.1.3：沿用 v2.1.2，读取的路径有截断数据集就不绑定。"""

from __future__ import annotations

import re
from copy import deepcopy
from decimal import Decimal
from typing import Any

from tests.eval import scoring as v1
from tests.eval import scoring_v2 as v2
from tests.eval import scoring_v2_1 as v21
from tests.eval import scoring_v2_1_1 as v211
from tests.eval import scoring_v2_1_2 as v212
from tests.eval.scoring_v2_1_2 import SPEC as SPEC
from tests.eval.scoring_v2_1_2 import load_questions as load_questions

VERSION = "v2.1.3"


class _RowPaths:
    """统一检查选行及派生、贡献展开路径；仅追溯 SQL 或口径语境的输入不检查行。"""

    def __init__(self, run: dict):
        self.datasets = {d["ref"]: d for d in run.get("datasets") or []}
        self.producer = {s["dataset"]: s for s in run.get("steps") or [] if s.get("dataset")}

    def reason(self, ref: str, depth: int = 0) -> str | None:
        # 与冻结评分器相同，最多追溯六层；更深的路径继续由原规则拒绝。
        if depth > 6:
            return None
        ds = self.datasets.get(ref) or {}
        saved, total = len(ds.get("rows") or []), ds.get("row_count") or 0
        if saved < total:
            return f"数据集 {ref} 只保存了前 {saved} 行（共 {total} 行），不能确定"
        step = self.producer.get(ref) or {}
        args = step.get("args") or {}
        inputs = []
        if step.get("tool") == "derive":
            inputs = [value.get("dataset") for value in args.get("values") or []]
        elif step.get("tool") == "contribution":
            inputs = [args.get("dataset")]
        for source in inputs:
            why = self.reason(source, depth + 1)
            if why:
                return why
        return None


def _detail(result: dict, uncertain: dict[str, str] | None = None) -> dict:
    """按引用路径计数，同一路径的多个期间、绑定定义或机制备选只计一次。"""
    uncertain = uncertain or {}
    return {
        **result,
        "uncertain_truncated": len(uncertain),
        "uncertain_details": [f"{path}：{why}" for path, why in list(uncertain.items())[:5]],
    }


class RunIndex(v212.RunIndex):
    """在所有数值绑定分支之前使用同一个路径检查器。"""

    def __init__(self, run: dict):
        super().__init__(run)
        self.row_paths = _RowPaths(run)

    def bind(self, segment: dict, window: dict | None = None) -> tuple[dict | None, str | None]:
        path = v2.PATH.match(segment.get("path") or "")
        # 初始探索上下文只有 SQL；是否需要选行由具体绑定定义决定。
        if path and (self.datasets.get(path["ds"]) or {}).get("kind") == "explore":
            return super().bind(segment, window)
        why = self.row_paths.reason(path["ds"]) if path else None
        return (None, why) if why else super().bind(segment, window)


def explore_binding(
    q: dict, run: dict, segment: dict, spec: dict, periods: list[str]
) -> tuple[dict | None, str | None]:
    path = v2.PATH.match(segment.get("path") or "")
    why = _RowPaths(run).reason(path["ds"]) if path else None
    return (None, why) if why else v212.explore_binding(q, run, segment, spec, periods)


def match_mechanism(cause: dict, run: dict, alternatives: list[dict]) -> dict:
    """证据行及展开输入走统一检查；成员已给出时的 SQL 来源判断、因子编码保持原规则。"""
    entity = cause.get("entity") or {}
    evidence = re.fullmatch(r"(D\d+)#(\d+)", str(entity.get("evidence_id") or ""))
    why = _RowPaths(run).reason(evidence[1]) if evidence and "member" not in entity else None
    if not why or all("factor" in option for option in alternatives):
        return _detail(v211.match_mechanism(cause, run, alternatives))
    # 只阻断依赖行的备选；因子编码匹配不需要读取行，仍可独立命中。
    uncertain, misses = {}, []
    for i, option in enumerate(alternatives, 1):
        if "factor" in option:
            if entity.get("factor_metric_code") == option["factor"]:
                return _detail(
                    {"hit": True, "alternative": i, "option": option, "sources": [], "tables": [], "rows": []},
                    uncertain,
                )
            reason = "因子编码不匹配"
        else:
            uncertain[entity["evidence_id"]] = reason = why
        misses.append({"alternative": i, "reason": reason})
    return _detail(
        {"hit": False, "reason": "；".join(dict.fromkeys(m["reason"] for m in misses)), "misses": misses}, uncertain
    )


def key_value(q: dict, report: dict, run: dict) -> dict[str, Any]:
    """沿用 v2.1.2 的候选顺序与门槛，只在进入绑定前排除截断路径并单独计数。"""
    key = (q.get("expect") or {}).get("key_value")
    bindings = (q.get("v2") or {}).get("key_bindings") or []
    if not key:
        return _detail({"hit": None, "reason": "题目没有关键数值"})
    if any(b.get("manual") for b in bindings):
        return _detail({"hit": None, "reason": "关键数值需人工审定（key_bindings.manual）"})
    if not bindings:
        return _detail({"hit": None, "reason": "缺少 v2 绑定定义"})
    tolerance = Decimal(str(key.get("tolerance", 0)))
    index, misses, explore_candidates, uncertain = RunIndex(run), [], [], {}
    for segment in v2._value_segments(report):
        raw = v1._number(segment.get("raw"))
        if raw is None:
            continue
        path = v2.PATH.match(segment.get("path") or "")
        truncated = index.row_paths.reason(path["ds"]) if path else None
        got, why = index.bind(segment, q["window"])
        for spec in bindings:
            candidates = spec["truth"] if spec.get("truth") else {"value": Decimal(str(spec["value"]))}
            matched = [
                p
                for p, value in candidates.items()
                if v1._within(
                    abs(raw) if key.get("abs") else raw,
                    abs(Decimal(str(value))) if key.get("abs") else Decimal(str(value)),
                    tolerance,
                )
            ]
            if not matched:
                continue
            sql_only = "explore" in spec and got is not None and got.get("source") == "explore"
            if truncated and not sql_only:
                uncertain[segment["path"]] = truncated
                misses.append(f"{segment['path']}：数值相同但{truncated}")
                continue
            if got is None:
                misses.append(f"{segment.get('path')}：数值相同但{why}")
            elif spec.get("truth") and got.get("source") != "explore" and got.get("period") not in matched:
                misses.append(
                    f"{segment.get('path')}：数值等于真值的{'、'.join(matched)}，但所在列是 {got.get('period')}"
                )
            else:
                problem = v2._binding_ok(spec, got, q)
                if problem is None:
                    return _detail({"hit": True, "path": segment.get("path"), "binding": v2._plain(got)}, uncertain)
                misses.append(f"{segment.get('path')}：数值相同但{problem}")
            if (
                spec.get("metric")
                and spec.get("truth")
                and path is not None
                and (segment.get("explore") or v21._is_explore(index, path["ds"]))
            ):
                explore_candidates.append((segment, spec, matched))
    explore_misses = []
    for segment, spec, periods in explore_candidates:
        binding, why = explore_binding(q, run, segment, spec, periods)
        if binding is not None:
            return _detail({"hit": True, "path": segment.get("path"), "binding": v2._plain(binding)}, uncertain)
        explore_misses.append(f"{segment.get('path')}：数值相同但{why}")
    return _detail(
        {
            "hit": False,
            "reason": "；".join(dict.fromkeys(explore_misses or misses))[:1200] or "报告里没有等于真值的数值引用",
        },
        uncertain,
    )


def score(question: dict, run: dict | None) -> dict[str, Any]:
    """先接入受检查的机制，再复用冻结的其余门槛，最后接入受检查的关键数值。"""
    adapted = deepcopy(question)
    adapted.setdefault("expect", {}).pop("key_value", None)
    primary = (adapted.get("expect") or {}).get("primary")
    options = (question.get("v2_1") or question.get("v2") or {}).get("mechanism") or question.get("mechanism")
    report = (run or {}).get("report") or {}
    detail = None
    if primary and primary.get("entity") and options:
        causes = report.get("causes") or []
        heads = (
            [cause for cause in causes[:2] if cause.get("direction") == primary["direction"]]
            if report.get("conclusion") == "offsetting"
            else causes[:1]
        )
        detail = (
            match_mechanism(heads[0], run or {}, options)
            if heads
            else _detail({"hit": False, "reason": "没有方向相同的首要原因"})
        )
        primary["entity"] = []
        if not detail["hit"]:
            primary["types"] = []
    result = v212.score(adapted, run)
    result["scorer"] = VERSION
    if detail is not None:
        if detail["hit"] and not result["primary_hit"]:
            detail = {**detail, "hit": False, "reason": "机制匹配，但运行完成、类型、方向或核验条件不满足"}
    else:
        detail = _detail(result["primary_detail"])
    result["primary_detail"] = result["mechanism"] = detail
    key = (question.get("expect") or {}).get("key_value")
    kv = (
        key_value(question, report, run or {})
        if result["completed"]
        else _detail({"hit": False if key else None, "reason": "运行没有完成"})
    )
    result["key_value_hit"], result["key_value_detail"] = kv["hit"], kv
    if result["completed"] and (
        question["category"] in v1.POSITIVE or (question["category"] == "adversarial" and primary)
    ):
        result["passed"] = bool(result["passed"] and kv["hit"] is not False)
    return result
