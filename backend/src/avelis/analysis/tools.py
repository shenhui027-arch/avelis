"""分析智能体的工具（ADR 0007 第 3 点）。

三类工具，全部以发起人身份执行、按其可见范围过滤。每次调用前都重新核对发起人（账号存在且启用、仍有查询角色）；
涉及对象的工具只针对请求的口径与来源闭包按当前授权核对（与问数同一套规则与来源闭包），不重建全量口径目录。
- 上下文：口径、资产（含字段注释）、血缘、变更记录、数据健康——只读元数据；
- 受治理查询：query_metric 复用问数的完整管线（start_answer / fulfil / finish_answer），每次产生一个数据集；
- 计算：输入只能是数据集编号与列名，数值由后端从数据集中取，模型不能手填数字。
预算（工具次数、查询次数、时长）在统一分发层 dispatch 强制执行：参数校验之前就计数，超出时直接拒绝并说明。
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Annotated, Any, Literal

from fastapi import HTTPException
from langchain_core.tools import StructuredTool
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from avelis.analysis import attribution, calc, measures
from avelis.analysis.grounding import Cause, Part, describe_scope, scope_key
from avelis.analysis.measures import col, like
from avelis.ask import service
from avelis.ask.intent import DimensionFilter, Intent, comparison_range
from avelis.collect.service import freshness_boundary
from avelis.config import get_settings
from avelis.connectors.base import QueryError
from avelis.governance import access
from avelis.governance.errors import GovernanceError
from avelis.models import (
    AnalysisDataset,
    AnalysisRun,
    AnalysisStep,
    Asset,
    AssetVersion,
    ChangeSet,
    CollectionRun,
    ColumnVersion,
    Datasource,
    Domain,
    MetricVersion,
    Metric,
    MetricVersionTransition,
    User,
)

QUERY_ROLES = {"analyst", "steward", "reviewer", "admin"}
PREVIEW_ROWS = 40
MODEL_ROWS = 60  # 一次返回给模型的数据行上限；完整数据集保存在库中


@dataclass
class Budget:
    """预算按工具类别分开（ADR 0009 第 7 点第 7 条）：
    - tool_calls 是总调用次数，其中最后 report_reserve 次留给 submit_report：其他工具最多执行 tool_calls − report_reserve 次，
      submit_report 每轮最多尝试 report_reserve 次（解析失败后可以重交），这几次总是可用，被拒绝的调用不占用它们；
    - queries 是查询类工具（query_metric、query_sql、sample_values、explain_change 的子查询）共用的额度，
      用完只停用查询类工具，计算工具与 submit_report 照常可用；
    - explore 是其中探索查询的单独上限。"""
    tool_calls: int = 30
    queries: int = field(default_factory=lambda: get_settings().analysis_query_limit)
    seconds: int = 300
    explore: int = 8  # 探索查询（含 sample_values）单独计数，失败、重试、修订都算（ADR 0009 第 6 点）
    report_reserve: int = 2  # 总调用次数里保留最后几次给 submit_report（参数解析失败时可以重交一次）
    # 模型请求（含失败与重试）与累计 token 的上限（任务书第 13 节，manifest b0.budget）：到上限后不再请求模型，记为预算截止。
    # token 上限是 E1 前统一修订的值（manifest b0.token_cap_decision）：第 13 节的 16 万输入 / 2.4 万输出装不下 B0，
    # 取 max(默认值, ceil(历史 P95 × 1.2))；None 表示不限（只记录）
    model_requests: int = 16
    input_tokens: int | None = 552_321
    output_tokens: int | None = 30_716  # 含提供方计费的推理 token
    used_model_requests: int = 0
    used_input_tokens: int = 0
    used_output_tokens: int = 0
    used_calls: int = 0  # 全部调用（含被拒绝的），只用于记录
    used_tools: int = 0  # submit_report 以外执行了的调用
    used_reports: int = 0  # 本轮 submit_report 的尝试次数（修订时清零）
    reports_total: int = 0  # 累计的 submit_report 尝试次数（修订不清零，参与总预算）
    used_queries: int = 0
    used_explore: int = 0
    started: float = field(default_factory=time.monotonic)

    def view(self) -> dict[str, Any]:
        return {"limits": {"tool_calls": self.tool_calls, "queries": self.queries, "seconds": self.seconds,
                           "explore": self.explore, "model_requests": self.model_requests,
                           "input_tokens": self.input_tokens, "output_tokens": self.output_tokens},
                # tool_calls：执行了的调用（其他工具与累计的 submit_report 尝试），不超过上限；attempts 另含被拒绝的调用
                "used": {"tool_calls": self.used_tools + self.reports_total, "attempts": self.used_calls,
                         "queries": self.used_queries, "explore": self.used_explore, "reports": self.reports_total,
                         "model_requests": self.used_model_requests, "input_tokens": self.used_input_tokens,
                         "output_tokens": self.used_output_tokens,
                         "seconds": round(time.monotonic() - self.started, 1)}}

    def model_blocked(self) -> str | None:
        """不能再请求模型的原因：请求次数（含重试）或累计 token 到上限。"""
        if self.used_model_requests >= self.model_requests:
            return f"模型请求已达上限 {self.model_requests} 次（含重试）"
        if (self.input_tokens is not None and self.used_input_tokens >= self.input_tokens) or (
                self.output_tokens is not None and self.used_output_tokens >= self.output_tokens):
            return f"模型 token 已达累计上限（输入 {self.input_tokens}、输出 {self.output_tokens}）"
        return None

    @classmethod
    def restore(cls, view: dict, **limits) -> "Budget":
        """从保存的预算视图（view()）恢复：已用的次数接着计数，不清零（澄清后恢复运行时用）；已用的秒数从截止时间里扣除，
        等待用户的时间不计入。limits：本次的上限（默认取保存时的上限）。"""
        saved, used = view.get("limits") or {}, view.get("used") or {}
        keys = ("tool_calls", "queries", "seconds", "explore", "model_requests", "input_tokens", "output_tokens")
        budget = cls(**{k: limits.get(k, saved.get(k)) for k in keys if k in saved or k in limits})
        budget.used_tools = max(0, int(used.get("tool_calls") or 0) - int(used.get("reports") or 0))
        budget.reports_total = int(used.get("reports") or 0)
        budget.used_calls = int(used.get("attempts") or 0)
        budget.used_queries = int(used.get("queries") or 0)
        budget.used_explore = int(used.get("explore") or 0)
        budget.used_model_requests = int(used.get("model_requests") or 0)
        budget.used_input_tokens = int(used.get("input_tokens") or 0)
        budget.used_output_tokens = int(used.get("output_tokens") or 0)
        budget.started = time.monotonic() - float(used.get("seconds") or 0)
        return budget

    def queries_exhausted(self) -> str | None:
        if self.used_queries >= self.queries:
            return f"查询额度已用完（{self.queries} 次）"
        return None

    def spent(self) -> str | None:
        """执行循环结束的条件之一：总调用次数用完。其他工具的次数用完后只剩 submit_report 的预留次数，
        预留次数也用完（本轮再也交不了报告）才算用完；其余情况由统一截止时间兜底。"""
        if self.used_reports >= self.report_reserve:
            return f"submit_report 本轮已尝试 {self.used_reports} 次"
        return None

    def remaining(self) -> float:
        """距统一截止时间还剩的秒数（计划、执行、修订、宽限共用同一个截止时间）。"""
        return self.started + self.seconds - time.monotonic()

    def exhausted(self) -> str | None:
        """submit_report 以外的工具不能再调用的原因：只剩留给 submit_report 的次数，或统一截止时间已到。
        以前各轮的 submit_report 尝试照样占用总预算（修订只重置本轮的尝试次数）。"""
        if self.used_tools + (self.reports_total - self.used_reports) >= self.tool_calls - self.report_reserve:
            return f"工具调用已达上限 {self.tool_calls} 次（最后 {self.report_reserve} 次留给 submit_report）"
        if self.remaining() <= 0:
            return f"分析时长已超过 {self.seconds} 秒"
        return None


class PermissionLost(Exception):
    """发起人账号停用、被删除或失去查询角色：之后的取数与上下文一律拒绝，分析停止。"""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class PermissionDenied(Exception):
    """本次请求的口径或来源当前无权访问（明确的权限拒绝，记入 denials，定时任务据此暂停）。"""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def _grams(text: str) -> set[str]:
    """检索用的词元：中文取相邻两个字（二元组），英文、数字与编码按词（含下划线切开的部分，两个字符以上）。"""
    text = (text or "").lower()
    out = set(re.findall(r"[a-z0-9]{2,}", text.replace("_", " ")))
    out |= set(re.findall(r"[a-z0-9_]{2,}", text))
    for run in re.findall(r"[\u4e00-\u9fff]+", text):
        out |= {run[i:i + 2] for i in range(len(run) - 1)} if len(run) > 1 else {run}
    return out


def _scan_key(args) -> str:
    """维度扫描的去重键：指标（按请求顺序，决定结果的列顺序）、分组维度（按请求顺序）、本期、对比期（方式与日期）与
    规范化的筛选（筛选的先后与取值的重复不改变语义）。排序、行数不在键里：带 order 或 limit 的请求不用扫描结果复用
    （扫描结果是全体成员、按维度值排列，不满足“前 N 个”的语义）。"""
    compare = (str(args.compare_start), str(args.compare_end)) if args.compare == "custom" else args.compare
    filters = sorted((f.dimension, sorted(set(f.values))) for f in args.filters)
    return json.dumps([list(args.metric_codes), list(args.group_by), str(args.start_date), str(args.end_date),
                       compare, filters], ensure_ascii=False)


def repair_arguments(raw: str) -> dict | None:
    """工具参数的 JSON 语法修复（开源 json_repair，ADR 0009 第 7 点第 10 条）：只修语法问题（字符串没加引号、多了或少了
    逗号、括号没闭合等），修复结果必须是一个对象，否则算解析失败。字段与类型不在这里改，修复后照常严格校验。"""
    from json_repair import repair_json

    try:
        repaired = repair_json(raw, return_objects=True)
    except Exception:  # noqa: BLE001 - 修不了就是解析失败
        return None
    if not isinstance(repaired, dict) or not repaired:
        return None
    return repaired if all(_appears(v, raw) for v in _scalars(repaired)) else None


def _scalars(value: Any):
    """修复结果里的全部键与标量值。"""
    if isinstance(value, dict):
        for k, v in value.items():
            yield k
            yield from _scalars(v)
    elif isinstance(value, list):
        for v in value:
            yield from _scalars(v)
    else:
        yield value


def _appears(value: Any, raw: str) -> bool:
    """标量值在原文里原样出现：修复不能补出原文没有的值（例如 {"region":} 被补成空字符串）。"""
    if value is None:
        return "null" in raw
    if isinstance(value, bool):
        return ("true" if value else "false") in raw
    if isinstance(value, (int, float)):
        return str(value) in raw or json.dumps(value) in raw
    text = str(value)
    if text == "":
        return '""' in raw or "''" in raw
    return text in raw or json.dumps(text, ensure_ascii=False)[1:-1] in raw or json.dumps(text)[1:-1] in raw


def json_error_path(raw: str) -> tuple[str, str]:
    """参数 JSON 解析失败时，出错位置所在的字段路径（如 claims[3].parts[2]）与原因，回传给模型改正这一处。"""
    try:
        json.loads(raw)
        return "", "结构不对"
    except json.JSONDecodeError as e:
        pos, reason = e.pos, e.msg
    stack: list[list] = []  # [类型, 当前键或下标]
    key, in_string, escape, token, expect_key = None, False, False, [], False
    for ch in raw[:pos]:
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
                if expect_key:
                    key = "".join(token)
            else:
                token.append(ch)
            continue
        if ch == '"':
            in_string, token = True, []
        elif ch == "{":
            stack.append(["dict", None])
            expect_key = True
        elif ch == "[":
            stack.append(["list", 0])
            expect_key = False
        elif ch in "}]":
            if stack:
                stack.pop()
        elif ch == ":" and stack and stack[-1][0] == "dict":
            stack[-1][1], expect_key = key, False
        elif ch == "," and stack:
            if stack[-1][0] == "list":
                stack[-1][1] += 1
            else:
                expect_key = True
    path = ""
    for kind, at in stack:
        path += f"[{at}]" if kind == "list" else (f".{at}" if path and at is not None else (at or ""))
    return path, reason


def _json(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, default=lambda v: str(v) if isinstance(v, (Decimal, date, datetime))
                      else repr(v))


# --- 参数 ---------------------------------------------------------------------------

class Keyword(BaseModel):
    keyword: str = Field("", description="关键词（名称、编码、注释中的文字）；空表示全部")


class Code(BaseModel):
    code: str = Field(description="指标编码")


class Qualified(BaseModel):
    qualified: str = Field(description="资产全名 catalog.db.name，如 <catalog>.<库>.<表>")


class Window(BaseModel):
    start_date: date
    end_date: date
    metric_code: str | None = Field(None, description="只看与该指标相关的对象（其来源资产与指标版本）")


class Health(BaseModel):
    metric_code: str = Field(description="指标编码：检查其来源的数据源采集情况与资产版本变化")
    start_date: date
    end_date: date


class QueryArgs(BaseModel):
    metric_codes: list[str] = Field(min_length=1, max_length=4, description="指标编码；多个指标只能用共有维度")
    start_date: date
    end_date: date
    filters: list[DimensionFilter] = Field(default_factory=list)
    group_by: list[str] = Field(default_factory=list, description="维度名")
    time_grain: Literal["day", "month"] | None = Field(None, description="按日或按月分组（结果首列 period）")
    compare: Literal["previous_period", "previous_year", "custom"] | None = Field(
        None, description="对比：上一期（紧邻的同长度区间）/ 去年同期 / custom（用 compare_start、compare_end 指定，"
                          "如 9 月对比自然月 8 月）；不能与 time_grain 同用")
    compare_start: date | None = None
    compare_end: date | None = None
    order: Literal["desc", "asc"] | None = None
    limit: int | None = Field(None, ge=1, le=100)
    purpose: str = Field(description="这次查询要回答什么（写进步骤记录）")


class QuerySqlArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sql: str = Field(description="一条 SELECT，用 PostgreSQL 写法的受限子集：写出列名，表写 库.表 或 catalog.库.表；"
                                 "函数只用聚合、COALESCE、NULLIF、CASE、CAST、DATE_TRUNC，日期加减写 列 ± INTERVAL 'n' DAY；"
                                 "等值 JOIN；列名与 describe_asset 一致，有大写字母的加双引号。系统按表所在的数据源生成方言"
                                 "并加行数上限，不用管目标数据库")
    purpose: str = Field(description="查询目的；有口径能覆盖时应优先用 query_metric，这里写明为什么需要探索查询")


class SampleArgs(BaseModel):
    asset: str = Field(description="资产全名 <catalog>.<库>.<表>")
    column: str = Field(description="列名")
    keyword: str | None = Field(None, max_length=50, description="可选：只看包含该关键字的取值")


class ExplainArgs(BaseModel):
    metric_code: str = Field(description="指标编码")
    filters: list[DimensionFilter] = Field(default_factory=list, description=(
        "题目范围的筛选（与 query_metric 的 filters 相同，如某个区域、渠道）：总量、各维度与因子拆解的子查询都带上；"
        "被筛选的维度不再作为候选维度扫描"))
    start_date: date | None = Field(None, description="本期开始；不写时取本次分析的窗口")
    end_date: date | None = None
    compare_start: date | None = Field(None, description="对比期开始；不写时取本次分析的对比期")
    compare_end: date | None = None
    dimensions: list[str] = Field(default_factory=list, max_length=8,
                                  description="候选维度名；不写时取该指标允许的维度（最多 8 个）")
    depth: Literal[1, 2] = Field(1, description="2：对第一层排名前 3 的维度两两组合再扫描")
    top_k: int = Field(5, ge=1, le=20, description="每个维度最多返回的成员数")
    purpose: str = Field("自动归因", description="这次归因要回答什么（写进步骤记录）")


class ContributionArgs(BaseModel):
    dataset: str = Field(description="数据集编号，如 D2")
    keys: list[str] = Field(description="维度列")
    current: str = Field(description="本期值列名，如 <指标>")
    previous: str = Field(description="上期值列名，如 <指标>·上一期")


class Pair(BaseModel):
    name: str = Field(description="因子名，如 数量、单价")
    metric_code: str | None = Field(None, description="因子对应的指标编码（有口径时写）：原因按编码关联")
    current: str = Field(description="本期值列名")
    previous: str = Field(description="上期值列名")


class FactorArgs(BaseModel):
    dataset: str
    total: Pair = Field(description="总量的本期与上期列，如 <指标> / <指标>·上一期")
    factors: list[Pair] = Field(min_length=2, description="乘积等于总量的因子，如 数量、单价")
    match: dict[str, str] = Field(default_factory=dict, description="选行条件（维度列 → 值）；数据集只有一行时可省略")


class CalendarArgs(BaseModel):
    dataset: str = Field(description="带对比期的受治理查询数据集；两期日期取自该次查询，不能另行指定")
    current: str = Field(description="本期值列名，如 <指标>")
    previous: str = Field(description="对比期值列名，如 <指标>·对比期")
    match: dict[str, str] = Field(default_factory=dict)


class CorrelateArgs(BaseModel):
    dataset: str
    x: str
    y: str


class CoverageArgs(BaseModel):
    dataset: str
    keys: list[str] = Field(default_factory=list, description="按哪些列统计（如 store）；为空表示整个数据集")
    period: str | None = Field(None, description="日期列；为空时用数据集的时间段列（按日分组时），没有则只计行数")
    start_date: date | None = Field(None, description="应覆盖的起始日期（用于找缺失的日期）")
    end_date: date | None = None


class ValueRef(BaseModel):
    name: str = Field(description="公式中的变量名（字母、数字、下划线）")
    dataset: str = Field(description="数据集编号")
    column: str = Field(description="列名")
    match: dict[str, str] = Field(default_factory=dict, description="选行条件；选出多行时对该列求和")
    exclude: dict[str, str] = Field(default_factory=dict, description="排除的行（如排除某个成员后求其余合计）")


class DeriveArgs(BaseModel):
    label: str = Field(description="结果的名称，如 其余成员合计")
    values: list[ValueRef] = Field(min_length=1, max_length=8)
    formula: str = Field(description="只用变量名、数字与 + - * / 括号，如 total - part、a / b")


class DescribeArgs(BaseModel):
    dataset: str
    column: str
    match: dict[str, str] = Field(default_factory=dict, description="选行条件（如 period 前缀不支持，用精确值）")


class SegmentArgs(BaseModel):
    dataset: str
    segment: str = Field(description="分组列")
    measures: list[str]


class OutlierArgs(BaseModel):
    dataset: str
    value: str
    keys: list[str]


class Scope(BaseModel):
    """理解阶段的结构化输出（ADR 0012）：问题类型、指标、筛选、窗口；确定不了时写追问。"""
    question_type: Literal["change", "relation", "other"] = Field(
        description="change：为什么变化 / 是否变化；relation：什么关系、怎么分布；other：其他")
    metric_codes: list[str] = Field(default_factory=list, max_length=4, description="所问的指标编码（只能取自候选口径）")
    filters: list[DimensionFilter] = Field(default_factory=list, description="题目范围的筛选：维度名与取值（如某个区域）")
    dimensions: list[str] = Field(default_factory=list, max_length=3, description="关系类问题要对比的维度名")
    start_date: date | None = Field(None, description="本期开始；问题没有另说时不写（取本次分析的窗口）")
    end_date: date | None = None
    compare_start: date | None = Field(None, description="对比期开始；问题没有另说时不写")
    compare_end: date | None = None
    clarification: str | None = Field(None, description="确定不了指标或范围时，向用户追问的一句话")


class Claim(BaseModel):
    type: Literal["fact", "calc", "inference", "recommendation"]
    parts: list[Part] = Field(description="结论正文的片段列表：文字片段写字符串，数字写成引用对象，如 "
                                          "['<成员>较上期下降 ', {'dataset': 'D19', 'summary': 'total.change', 'direction': 'down'}]；"
                                          "显示格式与单位由系统按量纲决定；文字片段里不写数量（ADR 0009）")
    refs: list[Annotated[str, Field(pattern=r"^D[1-9]\d{0,3}$")]] = Field(
        default_factory=list, description="额外引用的数据集编号，如 ['D2']；引用片段涉及的数据集自动计入")
    premise: str | list[Part] | None = Field(None, description="推断与建议的前提（文字或片段列表）")
    validation: str | list[Part] | None = Field(None, description="建议的验证方式（文字或片段列表）")


# submit_report 参数的模式版本：片段、原因等结构改变时加一。回放轨迹记录录制时的版本，版本不同的轨迹要重新录制
# （模型当时按旧模式写的报告，在新模式下不成立）。2：显示格式由系统决定；3：原因按编码关联、报告写所问指标
# （ADR 0009 第 7 点）；4：结论类型增加 offsetting（相互抵消，第 8 条），所问指标改为 scope（指标、筛选与窗口，第 9 条）
REPORT_SCHEMA = 4


class ReportScope(BaseModel):
    """报告所回答的范围（ADR 0009 第 7 点第 9 条）：结论类型只和 scope 完全相同的 explain_change 判定比对。"""
    metric_code: str = Field(description="所问的指标编码")
    filters: list[DimensionFilter] = Field(default_factory=list, description="题目范围的筛选（与 explain_change 相同）")
    start_date: date | None = Field(None, description="本期开始；不写时取本次分析的窗口")
    end_date: date | None = None
    compare_start: date | None = Field(None, description="对比期开始；不写时取本次分析的对比期")
    compare_end: date | None = None


class ReportArgs(BaseModel):
    title: str | list[Part] = Field(description="标题：文字或片段列表；数字同样写成引用对象")
    summary: str | list[Part] = Field(description="一两句话的结论：片段列表，数字写成引用对象，后端渲染并做同样的结构检查")
    claims: list[Claim]
    unexplained: str | list[Part] | None = Field(None, description="仍未解释的部分：文字或片段列表")
    conclusion: Literal["ranked_reasons", "offsetting", "no_material_change", "insufficient_evidence"] = Field(
        description="结论类型：ranked_reasons 有排序的原因；offsetting 成员变化相互抵消；no_material_change 没有实质变化；"
                    "insufficient_evidence 证据不足。所问指标做过 explain_change 时必须与它的判定一致")
    scope: ReportScope | None = Field(None, description="所问的指标、筛选与窗口（问“为什么变化”时必填）：结论类型按"
                                                         "同一 scope 的 explain_change 判定核对")
    method: str | list[Part] | None = Field(None, description="分析方法（可选）：怎么排查、用了哪些数据集；片段列表，"
                                                             "数字写成引用对象")
    causes: list[Cause] = Field(default_factory=list, description="按影响大小排序的原因（结论为 ranked_reasons 时必填，"
                                                                   "第一项是首要原因；offsetting 时前两项是最大的减少项与"
                                                                   "增加项；其余两种结论不填）")


# --- 工具箱 ---------------------------------------------------------------------------

class AnalysisToolbox:
    def __init__(self, session: Session, user: User, run: AnalysisRun, catalog: service.Catalog, budget: Budget,
                 executor: str | None = None, adapter: str | None = None):
        self.session, self.user, self.run, self.catalog, self.budget = session, user, run, catalog, budget
        self.executor = executor  # 执行权标记（run.budget.executor）：失去执行权后不再写入、停止
        self.adapter = adapter or getattr(run, "model_adapter", None) or "analysis"
        self.aborted: str | None = None
        self.verification: dict | None = None  # 核验结果：终态时与运行一起写入
        self._allowed: set[str] = set()
        self.plan: dict | None = None  # 理解阶段的 scope（运行记录里的 plan）
        self.scope: Scope | None = None
        self.report: dict | None = None
        self.permission_lost: str | None = None  # 发起人失去权限的原因：之后一律拒绝，分析停止
        self.denials: list[dict] = []  # 明确的权限拒绝（口径或来源无权访问）
        self._seq = 0
        self._datasets: dict[str, AnalysisDataset] = {}
        self._governed: dict[str, AnalysisDataset] = {}  # 受治理查询参数（不含 purpose）→ 数据集：相同查询复用
        self._scanned: dict[str, AnalysisDataset] = {}  # explain_change 扫描过的“指标 × 维度 × 窗口 × 筛选” → 数据集
        self._repair: dict | None = None  # 当前调用的参数经过语法修复时的标记（写进这次调用的步骤记录）
        self._tools: dict[str, StructuredTool] | None = None
        self.explanations: dict[str, str] = {}  # explain_change 的判定，键是 scope_key（指标、筛选、窗口）；unsupported 表示不适用
        self.clarification: dict | None = None  # 需要用户澄清时的问题与可恢复上下文（request_clarification）

    # -- 记录 --
    def _step(self, kind: str, tool: str | None, args: dict, status: str, output: dict, reason: str | None = None,
              dataset: AnalysisDataset | None = None, started: float | None = None) -> None:
        if self.aborted:  # 已失去执行权：不再写入
            return
        if self._repair:  # 这次调用的参数经过语法修复：步骤记录标明，并保留原文摘要
            output = {**output, **self._repair}
        self._seq += 1
        self.session.add(AnalysisStep(
            run_id=self.run.id, seq=self._seq, kind=kind, tool=tool, args=json.loads(_json(args)), status=status,
            output=json.loads(_json(output)), reason=reason, dataset_id=dataset.id if dataset else None,
            duration_ms=int((time.monotonic() - started) * 1000) if started else None))
        self.session.flush()
        if self.executor:  # 每一步即时提交（前端能看到进度、中断时已完成的步骤不丢），提交前在行锁下核对执行权
            if not self._owns_run(lock=True):
                self.session.rollback()
                self.aborted = "分析已被终结或由其他执行者接管，本执行者停止（未写入这一步）"
                return
            self.session.commit()

    @property
    def stopped(self) -> str | None:
        """不能再继续（不再请求模型）的原因：发起人失去权限，或运行已被终结 / 接管。"""
        return self.permission_lost or self.aborted

    def _owns_run(self, *, lock: bool = False) -> bool:
        stmt = select(AnalysisRun.status, AnalysisRun.budget).where(AnalysisRun.id == self.run.id)
        row = self.session.execute(stmt.with_for_update() if lock else stmt).one_or_none()
        return row is not None and row.status == "running" and (row.budget or {}).get("executor") == self.executor

    def check_executor(self) -> None:
        if not self.executor or self.aborted:
            return
        if not self._owns_run():
            self.aborted = "分析已被终结或由其他执行者接管，本执行者停止"

    def _dataset(self, kind: str, title: str, columns: list[str], rows: list[list], *, answer_id: str | None = None,
                 inputs: list[str] | None = None, meta: dict | None = None, truncated: bool = False) -> AnalysisDataset:
        meta, inputs = dict(meta or {}), list(inputs or [])
        # 来源、假设、截断、部分结果沿服务端保存的依赖闭包传递（ADR 0009 第 6 点“结果与来源标记”）：
        # 输入只能是本次运行已有的数据集（缺失即拒绝；新数据集只依赖已有的，不会成环）
        missing = [i for i in inputs if i not in self._datasets]
        if missing:
            raise calc.CalcError(f"依赖的数据集 {'、'.join(missing)} 不在本次运行中")
        parents = [self._datasets[i] for i in inputs]
        closure = sorted({*inputs, *(c for p in parents for c in (p.meta or {}).get("closure") or [])},
                         key=lambda r: int(r[1:]))
        roots = {kind} if not inputs else {k for p in parents for k in (p.meta or {}).get("root_kinds") or [p.kind]}
        meta.update(closure=closure, root_kinds=sorted(roots), explore="explore" in roots,
                    assumption=bool(meta.get("assumption") or any((p.meta or {}).get("assumption") for p in parents)),
                    partial=bool(meta.get("partial") or any((p.meta or {}).get("partial") for p in parents)))
        truncated = bool(truncated or any(p.truncated for p in parents))
        if "metric_codes" not in meta:  # 原因按编码关联：计算结果继承输入的口径编码
            meta["metric_codes"] = sorted({c for p in parents for c in (p.meta or {}).get("metric_codes") or []})
        ds = AnalysisDataset(run_id=self.run.id, seq=len(self._datasets) + 1, kind=kind, title=title[:255],
                             answer_id=answer_id, inputs=inputs, columns=columns,
                             rows=json.loads(_json(rows)), truncated=truncated, meta=json.loads(_json(meta)))
        self.session.add(ds)
        self.session.flush()
        self._datasets[f"D{ds.seq}"] = ds
        return ds

    def datasets(self) -> dict[str, AnalysisDataset]:
        return dict(self._datasets)

    def _unit(self, ref: str, column: str) -> dict:
        """数据集某列登记的量纲与单位；没登记的按 unknown。"""
        ds = self._datasets.get(ref)
        return dict(((ds.meta or {}).get("units") or {}).get(column) or measures.UNKNOWN) if ds else dict(measures.UNKNOWN)

    def _rows(self, ref: str) -> tuple[AnalysisDataset, list[dict]]:
        ds = self._datasets.get(ref)
        if ds is None:
            raise calc.CalcError(f"数据集 {ref} 不存在（可用：{'、'.join(self._datasets) or '无'}）")
        return ds, [dict(zip(ds.columns, r)) for r in ds.rows]

    def _view(self, ref: str, ds: AnalysisDataset) -> dict:
        out = {"dataset": ref, "title": ds.title, "kind": ds.kind, "columns": ds.columns, "row_count": len(ds.rows),
               "rows": ds.rows[:MODEL_ROWS], "rows_shown": min(len(ds.rows), MODEL_ROWS),
               **({"summary": ds.meta.get("summary")} if ds.meta.get("summary") else {})}
        if ds.truncated:  # 结果截断（result_truncated）：数据集本身不是全集
            out["truncated"] = True
            out["note"] = ("数据集被截断：只含前若干行，不是全集。合计、覆盖度、相关性等需要全集的计算会被拒绝；"
                           "不要据此写总数、行数或全体结论，请缩小范围或减少分组后重新查询")
        if len(ds.rows) > MODEL_ROWS:  # 预览截断（context_truncated）：数据集完整，只是给模型看的行数有限
            out["context_truncated"] = True
            out["context_note"] = (f"这里只预览前 {MODEL_ROWS} 行（共 {len(ds.rows)} 行），数据集本身是完整的；"
                                   "需要全集统计时调用计算工具，不要从预览里数或加总")
        for flag, text in (("explore", "来源含探索查询（未经口径审核）"), ("assumption", "来源含假设计算"),
                           ("partial", "来源含部分结果")):
            if (ds.meta or {}).get(flag):
                out.setdefault("marks", []).append(text)
        return out

    # -- 授权 --
    def _expire_governed(self) -> None:
        """授权前让会话里的用户、口径、资产对象过期：expire_on_commit=False 时它们可能保留其他事务改动前的状态。"""
        for obj in list(self.session.identity_map.values()):
            if isinstance(obj, (User, Metric, MetricVersion, Asset)):
                self.session.expire(obj)

    def _authorize(self) -> None:
        """重新核对发起人：账号存在、启用、仍有查询角色；取当前可访问的业务域（两次查询，不建全量口径目录）。"""
        self._expire_governed()
        user = self.session.get(User, self.user.id, populate_existing=True)
        if user is None or not user.is_active:
            raise PermissionLost("发起人账号不存在或已停用")
        if not access.roles_of(self.session, user) & QUERY_ROLES:
            raise PermissionLost("发起人已没有查询角色")
        self.user = user
        self._allowed = access.domain_ids(self.session, user)

    def _deny(self, message: str, **detail) -> None:
        self.denials.append({"message": message, **detail})
        raise PermissionDenied(message)

    def _metric(self, code: str) -> tuple[Any, MetricVersion]:
        """按与口径目录相同的规则判断这一个口径（业务域可见、有进入过发布流程的版本）；
        存在但不可见时是明确的权限拒绝（模型编造的编码只是错误）。"""
        metric, version = service.visible_metric(self.session, code, self._allowed)
        if metric is None:
            raise ValueError(f"指标 {code} 不存在")
        if version is None:
            self._deny(f"指标 {code} 当前无权访问（不在你可访问的业务域，或已不在可查询状态）", metric_code=code)
        return metric, version

    def _metric_sources(self, code: str) -> tuple[Any, MetricVersion, list[MetricVersion], dict[str, Asset]]:
        """口径、组成版本与来源闭包，逐项核对当前授权：组成指标的业务域、来源资产（含关联的右表）及其上游都必须可见。
        来源闭包与问数同一套函数（ask.service.version_sources）。"""
        metric, version = self._metric(code)
        versions = [version]
        if version.kind == "derived":
            versions = []
            for c in version.components or []:
                cm, cv = self.session.get(Metric, c["metric_id"]), self.session.get(MetricVersion, c["version_id"])
                if cm is None or cv is None or cm.domain_id not in self._allowed:
                    self._deny(f"派生指标“{metric.name}”的组成指标 {c.get('metric_name')} 不在你可访问的业务域",
                               metric_code=code)
                versions.append(cv)
        closure: dict[str, Asset] = {}
        for v in versions:
            closure.update(service.version_sources(self.session, v))
        if any(not access.can_read_asset(self.session, self.user, a, self._allowed) for a in closure.values()):
            self._deny(f"没有权限读取指标 {code} 的来源（含关联表与视图背后的表）", metric_code=code)
        return metric, version, versions, closure

    def _guard(self, tool: str, args: dict, run) -> str:
        """统一的授权核对、异常与步骤记录（预算在 dispatch 中计数与检查）。"""
        started = time.monotonic()
        try:
            if self.permission_lost:
                raise PermissionLost(self.permission_lost)
            self._authorize()
        except PermissionLost as exc:
            self.permission_lost = exc.message
            self._step("tool", tool, args, "denied", {"error": exc.message}, started=started)
            return _json({"error": f"{exc.message}：之后的取数与上下文一律拒绝，分析停止"})
        try:
            output, dataset, status = run()
        except PermissionDenied as exc:
            self._step("tool", tool, args, "denied", {"error": exc.message}, started=started)
            return _json({"error": exc.message, "status": "denied"})
        except (calc.CalcError, HTTPException, ValueError, GovernanceError) as exc:
            message = exc.detail if isinstance(exc, HTTPException) else (
                exc.message if isinstance(exc, GovernanceError) else str(exc))
            self._step("tool", tool, args, "error", {"error": message}, started=started)
            return _json({"error": message})
        self._step("tool", tool, args, status, output if dataset is None else {"dataset": f"D{dataset.seq}"},
                   reason=args.get("purpose"), dataset=dataset, started=started)
        return _json(output)

    # -- 统一分发 --
    def dispatch(self, name: str, args: Any, *, only_report: bool = False) -> str:
        """模型的每一次工具调用都从这里进入：参数校验之前就计数（未知工具、参数无效、重复提交计划都算）。
        其他工具的次数用完后只能调用 submit_report（被拒绝的调用不占用它的预留次数）。only_report：时间将尽，只接受 submit_report。"""
        if self._tools is None:
            self._tools = {t.name: t for t in self.tools()}
        self.check_executor()
        if self.stopped:
            return _json({"error": f"{self.stopped}：不再执行工具"})
        self.budget.used_calls += 1
        if name == "submit_report":
            if self.budget.used_reports >= self.budget.report_reserve:
                self._step("tool", name, {}, "denied", {"error": "提交次数已用完"})
                return _json({"error": f"submit_report 本轮已尝试 {self.budget.used_reports} 次，不能再提交"})
            self.budget.used_reports += 1
            self.budget.reports_total += 1
        else:
            over = self.budget.exhausted() or ("分析时长即将用完" if only_report else None)
            if over:
                self._step("tool", name, args if isinstance(args, dict) else {"raw": str(args)[:500]}, "denied",
                           {"error": over})
                return _json({"error": f"{over}：只能调用 submit_report 提交报告，并在 unexplained 中说明未完成的部分"})
            self.budget.used_tools += 1
        tool = self._tools.get(name)
        if tool is None:
            self._step("tool", name, {}, "error", {"error": "未知工具"})
            return _json({"error": f"未知工具 {name}"})
        if not isinstance(args, dict):  # 参数不是合法 JSON：先只修语法（ADR 0009 第 7 点第 10 条），修好后照常做严格的结构校验
            repaired = repair_arguments(str(args))
            if repaired is not None:
                self._repair = {"repaired": True, "raw_excerpt": str(args)[:300]}
                try:
                    return self._invoke(tool, name, repaired)
                finally:
                    self._repair = None
            path, reason = json_error_path(str(args))
            message = f"参数无法解析：JSON 在 {path or '开头'} 处出错（{reason}）"
            self._step("tool", name, {"raw": str(args)[:500]}, "error", {"error": message})
            return _json({"error": f"工具 {name} 的{message}，请修正这一处后重交" +
                          ("（还可以重交一次）" if name == "submit_report" and self.budget.used_reports
                           < self.budget.report_reserve else "")})
        return self._invoke(tool, name, args)

    def _invoke(self, tool: StructuredTool, name: str, args: dict) -> str:
        try:
            return str(tool.invoke(args))
        except ValidationError as exc:  # 参数结构不对：逐条写出出错的字段路径
            problems = [f"{'.'.join(str(x) for x in e['loc']) or '参数'}：{e['msg']}" for e in exc.errors()[:6]]
            message = f"参数结构不对：{'；'.join(problems)}"
            self._step("tool", name, json.loads(_json(args)), "error", {"error": message})
            return _json({"error": message + ("（还可以重交一次）" if name == "submit_report" and
                                              self.budget.used_reports < self.budget.report_reserve else "")})
        except Exception as exc:  # noqa: BLE001 - 其他错误如实返回给模型
            message = f"{type(exc).__name__}: {str(exc)[:500]}"
            self._step("tool", name, json.loads(_json(args)), "error", {"error": message})
            return _json({"error": message})

    # -- 理解与系统预检（ADR 0012） --
    CANDIDATES = 10
    ALL_CANDIDATES = 30  # 可见口径不超过这个数时全部给出
    PACK_CHARS = 15000  # 上下文包的长度上限（约 7500 token）；完整数据保存在数据集中
    META_TABLES, META_COLUMNS = 4, 20  # 来源闭包的元数据：最多几张表、每张表几列
    PACK_ROWS = 8

    def candidate_metrics(self, question: str) -> list[dict[str, Any]]:
        """按问题文字从当前用户可见的口径中检索候选（ADR 0012 第 1 段）。可见口径不超过 ALL_CANDIDATES 个时全部给出；
        超过时按得分取前 CANDIDATES 个：中文按字的二元组、英文与编码按词，对名称、别名、编码、描述、维度名分别计分
        （名称与别名权重高）。每个候选带业务域，同名口径可以区分。"""
        self.catalog = service.visible_catalog(self.session, self.user, with_values=False)
        asked = _grams(question)
        domains: dict[str | None, str | None] = {}
        scored = []
        for c in self.catalog.cards:
            metric, version = self.catalog.versions[c.code]
            domain_id = getattr(metric, "domain_id", None)
            if domain_id not in domains:
                domain = self.session.get(Domain, domain_id) if domain_id else None
                domains[domain_id] = getattr(domain, "name", None)
            fields = [(3, c.name), *((3, a) for a in c.aliases or []), (1, c.code), (1, c.description or ""),
                      *((1, f"{d.get('name') or ''} {d.get('label') or ''}") for d in c.dimensions)]
            score = sum(w * len(_grams(text) & asked) for w, text in fields)
            scored.append((-score, c.code, {
                "code": c.code, "name": c.name, "domain": domains[domain_id], "aliases": list(c.aliases or []),
                "description": c.description or None,
                "dimensions": [{"name": d["name"], "label": d.get("label")} for d in c.dimensions],
                "unit": getattr(version, "unit", None), "kind": version.kind,
                "formula": version.expression if version.kind == "derived" else None}))
        ranked = [entry for *_, entry in sorted(scored, key=lambda x: (x[0], x[1]))]
        return ranked if len(ranked) <= self.ALL_CANDIDATES else ranked[:self.CANDIDATES]

    def record_scope(self, scope: Scope | None, raw: dict | None, problem: str | None) -> None:
        self.scope = scope
        self.plan = {"scope": scope.model_dump(mode="json") if scope else None, "problem": problem}
        self._step("plan", "understand", raw or {}, "ok" if scope else "error",
                   {"scope": self.plan["scope"]} if scope else {"error": problem})

    def request_clarification(self, question: str, scope: Scope | None, raw: dict | None) -> None:
        """需要用户澄清（任务书 P0-A）：记一个 clarify 步骤，保存可恢复的上下文——原问题、模型给出的范围（含原始参数）、
        追问与此刻已用的预算（恢复时用 Budget.restore 接着计数，额度不清零）。等待用户的时间不计入分析耗时。"""
        self.clarification = {"question": question[:300], "scope": scope.model_dump(mode="json") if scope else None,
                              "raw_scope": raw, "budget": self.budget.view(),
                              "resume": {"run_question": getattr(self.run, "question", None),
                                         "window": getattr(self.run, "window", None)}}
        self._step("clarify", "understand", raw or {}, "waiting", {"question": self.clarification["question"]})

    def precheck(self, scope: Scope) -> list[str]:
        """系统预检（确定性，不经模型）：变化类按顺序执行 describe_metric、changes、data_health、explain_change
        （带 scope 的筛选与自动因子拆解）、calendar_adjust；关系类按维度取多指标数据，做分组对比与相关。
        查询照常计入额度、检查权限并记录步骤；某一步失败只记下，不影响其余步骤。返回产出的数据集编号。"""
        refs: list[str] = []
        self.precheck_notes: list[str] = []
        self.precheck_groups: dict[str, list[str]] = {"attribution": [], "trend": []}
        self.precheck_meta: list[dict] = []
        self.precheck_problem: str | None = None  # 所选指标在预检里没有数据的原因：理解阶段据此重选一次
        window = self._window(scope.start_date, scope.end_date, scope.compare_start, scope.compare_end)

        def run(label: str, call, group: str = "attribution") -> dict:
            try:
                out = json.loads(call())
            except Exception as exc:  # noqa: BLE001 - 预检的单步失败如实记下
                out = {"error": f"{type(exc).__name__}: {str(exc)[:200]}"}
            if out.get("error"):
                self.precheck_notes.append(f"{label}：{str(out['error'])[:200]}")
            elif out.get("dataset"):
                refs.append(out["dataset"])
                self.precheck_groups[group].append(out["dataset"])
            return out
        if scope.question_type in ("change", "relation") and not scope.metric_codes:
            self.precheck_problem = "没有选出候选口径里的指标"
        if not scope.metric_codes or not window["start"]:
            return refs
        code, filters = scope.metric_codes[0], [f.model_dump() for f in scope.filters]
        if scope.question_type == "change":
            self.precheck_metric = run("口径详情", lambda: self.describe_metric(code=code))
            if self.precheck_metric.get("error"):
                self.precheck_problem = f"指标 {code} 不可用：{str(self.precheck_metric['error'])[:200]}"
                return refs
            if not window["compare_start"]:
                self.precheck_notes.append("没有对比期：跳过变更、归因与日历折算")
                return refs
            run("变更记录", lambda: self.changes(start_date=date.fromisoformat(window["compare_start"]),
                                                end_date=date.fromisoformat(window["end"]), metric_code=code))
            run("数据健康", lambda: self.data_health(metric_code=code, start_date=date.fromisoformat(window["compare_start"]),
                                                    end_date=date.fromisoformat(window["end"])))
            explained = run("自动归因", lambda: self.explain_change(
                metric_code=code, filters=filters, start_date=window["start"], end_date=window["end"],
                compare_start=window["compare_start"], compare_end=window["compare_end"], purpose="系统预检：自动归因"))
            if explained.get("error"):
                self.precheck_problem = f"指标 {code} 在这个范围里没有可归因的数据：{str(explained['error'])[:200]}"
            ds = self._datasets.get(explained.get("dataset") or "")
            summary = (ds.meta or {}).get("summary") or {} if ds else {}
            total = ds.inputs[0] if ds and ds.inputs else None
            name = self.catalog.versions.get(code, (None,))[0] if getattr(self, "catalog", None) else None
            if total and summary.get("algorithm") == "additive" and name is not None:
                run("日历折算", lambda: self.calendar_adjust(dataset=total, current=name.name,
                                                            previous=f"{name.name}·对比期"))
            if factor := (summary.get("factor_decomposition") or {}).get("dataset"):
                self.precheck_groups["attribution"].append(factor)
            daily = run("日趋势", lambda: self.query_metric(
                metric_codes=[code], start_date=window["compare_start"], end_date=window["end"], filters=filters,
                time_grain="day", purpose="系统预检：两期的日趋势"), "trend")
            if daily.get("dataset"):
                run("覆盖度", lambda: self.coverage(dataset=daily["dataset"], start_date=date.fromisoformat(
                    window["compare_start"]), end_date=date.fromisoformat(window["end"])), "trend")
            self.precheck_meta = self._source_metadata(code)
        elif scope.question_type == "relation":
            dims = scope.dimensions[:2]
            self.precheck_meta = self._source_metadata(code)
            for dim in dims:
                out = run(f"按{dim}取数", lambda dim=dim: self.query_metric(
                    metric_codes=scope.metric_codes, start_date=window["start"], end_date=window["end"],
                    filters=filters, group_by=[dim], purpose=f"系统预检：按{dim}取数"))
                ds = self._datasets.get(out.get("dataset") or "")
                if ds is None:
                    continue
                measures = [c for c in ds.columns if c != dim]
                run(f"按{dim}分组对比", lambda ref=out["dataset"], dim=dim, measures=measures: self.segment_compare(
                    dataset=ref, segment=dim, measures=measures))
                if len(measures) >= 2:
                    run(f"按{dim}的相关", lambda ref=out["dataset"], measures=measures: self.correlate(
                        dataset=ref, x=measures[0], y=measures[1]))
            if dims and not refs:
                self.precheck_problem = f"指标 {'、'.join(scope.metric_codes)} 按 {'、'.join(dims)} 取不到数据"
        return refs

    def _source_metadata(self, code: str) -> list[dict]:
        """所问口径来源闭包的元数据与血缘子图（ADR 0012 预检）：每张表的列（口径里用到的、有注释的排在前面）、字段注释、
        上游一层与视图定义摘要。读取照常经过权限核对并记录步骤；读不到的表跳过。"""
        try:
            _, version, versions, closure = self._metric_sources(code)
        except Exception:  # noqa: BLE001 - 不可见时不给元数据
            return []
        used: set[str] = set()
        for v in versions or [version]:
            used |= set(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", f"{v.expression or ''} {v.time_column or ''}"))
            used |= {d.get("column") for d in (v.dimensions or []) if d.get("column")}
        tables = []
        for asset in list(closure.values())[:self.META_TABLES]:
            qualified = f"{asset.catalog_name}.{asset.db_name}.{asset.name}"
            try:
                detail = json.loads(self.describe_asset(qualified=qualified))
                graph = json.loads(self.lineage(qualified=qualified))
            except Exception:  # noqa: BLE001 - 单表失败不影响其余
                continue
            if detail.get("error"):
                continue
            columns = sorted(detail.get("columns") or [], key=lambda c: (
                c["name"] not in used, not ((c.get("doc") or {}).get("comment"))))[:self.META_COLUMNS]
            tables.append({"table": qualified, "kind": detail.get("kind"), "comment": detail.get("comment"),
                           "columns": [{"name": c["name"], "type": c.get("type"),
                                        "comment": (c.get("doc") or {}).get("comment")} for c in columns],
                           "upstream": sorted({e["source"] for e in graph.get("edges") or []
                                               if e.get("target") == qualified})[:8],
                           "definition": (detail.get("definition") or "")[:400] or None})
        return tables

    def context_pack(self) -> str:
        """紧凑的上下文包（ADR 0012 第 2 段）。按优先级排列，超出上限时从后往前截断：判定与归因（explain_change 的判定、
        各维度前 3 个成员的两期明细、因子拆解、日历折算、变更与数据健康）→ 日趋势与覆盖 → 来源闭包的元数据与血缘 →
        其他数据集。总长度（含分隔符、预检错误与省略标记）不超过 PACK_CHARS；放不下的在末尾的省略标记里列出。
        完整数据保存在数据集中，引用 Dn 照常核验。"""
        sep, cap = "\n\n", self.PACK_CHARS
        lines: list[str] = []
        used = 0

        def add(text: str, limit: int) -> bool:
            nonlocal used
            cost = len(text) + (len(sep) if lines else 0)
            if used + cost > limit:
                return False
            lines.append(text)
            used += cost
            return True
        reserve = 400  # 留给预检错误与省略标记
        omitted: list[str] = []
        metric = getattr(self, "precheck_metric", None) or {}
        if metric and not metric.get("error"):
            keep = {k: metric.get(k) for k in ("code", "name", "kind", "unit", "expression", "time_column", "dimensions",
                                                "components") if metric.get(k) is not None}
            add("口径：" + _json(keep)[:1200], cap - reserve)
        groups = getattr(self, "precheck_groups", None) or {}
        first = [r for r in groups.get("attribution", []) if r in self._datasets]
        trend = [r for r in groups.get("trend", []) if r in self._datasets]
        rest = [r for r in self._datasets if r not in first and r not in trend]
        for ref in first + trend:
            if omitted or not add(self._pack_block(ref, self._datasets[ref]), cap - reserve):
                omitted.append(ref)
        for table in getattr(self, "precheck_meta", None) or []:
            block = "来源表 " + _json(table)[:2500]
            if omitted or not add(block, cap - reserve):
                omitted.append(f"来源表 {table['table']}")
        for ref in rest:
            if omitted or not add(self._pack_block(ref, self._datasets[ref]), cap - reserve):
                omitted.append(ref)
        notes = getattr(self, "precheck_notes", None) or []
        if notes:
            room = cap - used - len(sep) - (160 if omitted else 0)
            text = "预检中没有完成的步骤：" + "；".join(notes)
            add(text if len(text) <= room else text[:max(0, room - 1)] + "…", cap)
        if omitted:
            marker = f"其余 {len(omitted)} 项放不下，需要时用工具读取："
            room = cap - used - len(sep) - len(marker)
            listed = "、".join(omitted)
            add(marker + (listed if len(listed) <= room else listed[:max(0, room - 1)] + "…"), cap)
        return sep.join(lines) or "（没有预检数据）"

    def _pack_block(self, ref: str, ds: AnalysisDataset) -> str:
        """上下文包里的一个数据集：编号、标题、关键汇总与前若干行；自动归因按维度各列前 3 个成员的两期明细。"""
        head = f"{ref} {ds.title}（{ds.kind}，{len(ds.rows)} 行{'，截断' if ds.truncated else ''}）"
        body = []
        summary = (ds.meta or {}).get("summary")
        if summary:
            body.append("summary：" + _json(summary)[:1500])
        body.append("列：" + "、".join(ds.columns))
        if "dimension" in ds.columns and "role" in ds.columns:  # 自动归因：各维度前 3 个成员
            seen: dict[str, int] = {}
            picked = []
            for r in ds.rows:
                dim = r[ds.columns.index("dimension")]
                if seen.get(dim, 0) < 3:
                    seen[dim] = seen.get(dim, 0) + 1
                    picked.append(r)
            body += ["  " + _json(r)[:300] for r in picked]
            if len(picked) < len(ds.rows):
                body.append(f"  （各维度只列前 3 个成员，共 {len(ds.rows)} 行）")
        else:
            body += ["  " + _json(r)[:300] for r in ds.rows[:self.PACK_ROWS]]
            if len(ds.rows) > self.PACK_ROWS:
                body.append(f"  （只列前 {self.PACK_ROWS} 行，共 {len(ds.rows)} 行）")
        return "\n".join([head, *body])

    def submit_report(self, title: str | list, summary: str | list, claims: list[Claim],
                      unexplained: str | list | None = None, conclusion: str | None = None,
                      causes: list[Cause] | None = None, scope: ReportScope | None = None,
                      method: str | list | None = None) -> str:
        """保存模型提交的片段列表与原因项；渲染与结构检查在 verify 节点（ADR 0009）。scope 的窗口没写时取本次分析的窗口，
        登记键与 explain_change 用同一个函数生成。"""
        scope = _model(ReportScope, scope) if scope is not None else None
        scope_view, key = None, None
        if scope is not None:
            window = self._window(scope.start_date, scope.end_date, scope.compare_start, scope.compare_end)
            scope_view = {"metric_code": scope.metric_code, "filters": [f.model_dump() for f in scope.filters],
                          "start_date": window["start"], "end_date": window["end"],
                          "compare_start": window["compare_start"], "compare_end": window["compare_end"]}
            key = scope_key(scope.metric_code, scope.filters, window)
        self.report = {"title": _parts(title), "summary": _parts(summary),
                       "claims": [_model(Claim, c).model_dump() for c in claims], "unexplained": _parts(unexplained),
                       "method": _parts(method), "conclusion": conclusion,
                       "metric_code": scope.metric_code if scope else None,
                       "scope": scope_view, "scope_key": key,
                       "causes": [_model(Cause, c).model_dump(exclude_none=True) for c in causes or []]}
        self._step("note", "submit_report", {"title": self.report["title"]}, "ok", {"claims": len(claims)})
        return _json({"ok": True})

    # -- 上下文 --
    def search_metrics(self, keyword: str = "") -> str:
        def run():
            self.catalog = service.visible_catalog(self.session, self.user, with_values=False)  # 列表本身需要全量
            out = []
            for c in self.catalog.cards:
                if keyword and not any(keyword in s for s in (c.name, c.code, c.description or "", *(c.aliases or []))):
                    continue
                metric, version = self.catalog.versions[c.code]
                out.append({"code": c.code, "name": c.name, "kind": version.kind, "status": c.status,
                            "description": c.description, "formula": version.expression if version.kind == "derived"
                            else None, "dimensions": [{"name": d["name"], "label": d.get("label")}
                                                      for d in c.dimensions]})
            return {"metrics": out}, None, "ok"
        return self._guard("search_metrics", {"keyword": keyword}, run)

    def describe_metric(self, code: str) -> str:
        def run():
            from avelis.api.governance import version_view

            metric, version, _, _ = self._metric_sources(code)
            v = version_view(self.session, version)
            base = self.session.get(Asset, version.base_asset_id) if version.base_asset_id else None
            history = [{"version_no": mv.version_no, "status": mv.status, "status_reason": mv.status_reason,
                        "published_at": mv.published_at} for mv in self.session.scalars(
                select(MetricVersion).where(MetricVersion.metric_id == metric.id).order_by(MetricVersion.version_no))]
            return {"code": code, "name": metric.name, "version_no": version.version_no, "status": version.status,
                    "kind": version.kind, "description": version.description, "expression": version.expression,
                    "base_asset": f"{base.catalog_name}.{base.db_name}.{base.name}" if base else None,
                    "time_column": version.time_column, "filters": version.filters,
                    "dimensions": version.dimensions, "components": version.components,
                    "dependency": v.get("dependency"), "drift": v.get("drift"),
                    "transitions": v.get("transitions"), "versions": history}, None, "ok"
        return self._guard("describe_metric", {"code": code}, run)

    def _asset(self, qualified: str) -> Asset:
        parts = qualified.split(".")
        if len(parts) != 3:
            raise ValueError("资产全名应为 catalog.db.name")
        found = list(self.session.scalars(select(Asset).where(
            Asset.catalog_name == parts[0], Asset.db_name == parts[1], Asset.name == parts[2], Asset.status == "active")))
        for a in found:
            if access.can_read_asset(self.session, self.user, a, self._allowed):
                return a
        if found:  # 资产存在但不可见：明确的权限拒绝
            self._deny(f"资产 {qualified} 无权访问", asset=qualified)
        raise ValueError(f"资产 {qualified} 不存在")

    def search_assets(self, keyword: str = "") -> str:
        def run():
            allowed = access.domain_ids(self.session, self.user)
            out = []
            for a in self.session.scalars(select(Asset).where(Asset.status == "active")
                                          .order_by(Asset.catalog_name, Asset.db_name, Asset.name)):
                if not access.can_read_asset(self.session, self.user, a, allowed) or a.kind == "catalog_table":
                    continue
                name = f"{a.catalog_name}.{a.db_name}.{a.name}"
                cols = list(self.session.scalars(select(ColumnVersion).where(
                    ColumnVersion.asset_version_id == a.current_version_id))) if a.current_version_id else []
                hit = [c.name for c in cols if keyword and (keyword in c.name or keyword in (c.comment or ""))]
                comment = (self.session.get(AssetVersion, a.current_version_id).comment
                           if a.current_version_id else None)
                if keyword and keyword not in name and keyword not in (comment or "") and not hit:
                    continue
                out.append({"qualified": name, "kind": a.kind, "comment": comment, "matched_columns": hit[:10],
                            **self._source_info(a.datasource_id)})
                if len(out) >= 40:
                    break
            return {"assets": out, "note": "字段的数据库注释与状态见 describe_asset"}, None, "ok"
        return self._guard("search_assets", {"keyword": keyword}, run)

    def _source_info(self, datasource_id: str) -> dict:
        """资产所在的数据源与引擎（供说明与定位错误；探索查询照样写标准 SQL，方言由系统生成）。"""
        from avelis.analysis.explore import profile_for
        from avelis.engines import engine_of
        from avelis.engines.base import QUERY

        cache = self.__dict__.setdefault("_source_cache", {})
        if datasource_id not in cache:
            ds = self.session.get(Datasource, datasource_id)
            try:
                explorable = bool(ds and ds.allow_exploration and profile_for(ds.kind) and engine_of(ds).supports(QUERY))
            except Exception:  # noqa: BLE001 - 未知引擎
                explorable = False
            cache[datasource_id] = {"datasource": ds.name if ds else None, "engine": ds.kind if ds else None,
                                    "exploration": explorable}
        return cache[datasource_id]

    def describe_asset(self, qualified: str) -> str:
        def run():
            from avelis.api.catalog import asset_detail

            a = self._asset(qualified)
            d = asset_detail(a.id, self.user, self.session)
            return {"qualified": qualified, "kind": d["kind"], "comment": (d.get("version") or {}).get("comment"),
                    **self._source_info(a.datasource_id),
                    "columns": [{"name": c["name"], "type": c["data_type"], "doc": {
                        "status": c["doc"]["status"], "comment": c["doc"]["comment"], "source": c["doc"]["source"]}}
                        for c in d["columns"]],
                    "definition": (d.get("definition") or {}).get("raw_sql"),
                    "parse": (d.get("parse") or {}).get("status"),
                    "grains": [{"columns": g["columns"], "origin": g["origin"], "status": g["status"]}
                               for g in d.get("grains", [])]}, None, "ok"
        return self._guard("describe_asset", {"qualified": qualified}, run)

    def lineage(self, qualified: str) -> str:
        def run():
            from avelis.api.catalog import lineage_graph

            a = self._asset(qualified)
            g = lineage_graph(None, a.id, self.user, self.session)
            names = {n["id"]: n["qualified"] for n in g["nodes"]}
            edges = [{"source": names.get(e["source"], e["source"]), "target": names.get(e["target"], e["target"]),
                      "columns": [{"source": c["source_column"], "target": c["target_column"],
                                   "relation": c["relation"]} for c in e["columns"]][:40]}
                     for e in g["edges"] if a.id in (e["source"], e["target"])]
            return {"asset": qualified, "edges": edges}, None, "ok"
        return self._guard("lineage", {"qualified": qualified}, run)

    def changes(self, start_date: date, end_date: date, metric_code: str | None = None) -> str:
        def run():
            from avelis.api.governance import change_set_view

            lo = datetime(start_date.year, start_date.month, start_date.day)
            hi = datetime(end_date.year, end_date.month, end_date.day) + timedelta(days=1)
            # 给定指标时只看它的来源闭包（含组成指标的来源）上的资产变更，以及涉及它的版本与资产的影响
            scope_assets: set[str] | None = None
            scope_objects: set[str] = set()
            if metric_code:
                _, version, versions, closure = self._metric_sources(metric_code)
                scope_assets = set(closure)
                scope_objects = scope_assets | {version.id, *(v.id for v in versions)}
            sets = []
            for cs in self.session.scalars(select(ChangeSet).where(ChangeSet.created_at >= lo,
                                                                   ChangeSet.created_at < hi)
                                           .order_by(ChangeSet.created_at)):
                v = change_set_view(self.session, cs, full=True, user=self.user)
                events = [e for e in v["events"] if scope_assets is None or e["asset_id"] in scope_assets]
                impacts = [i for i in v["impacts"] if scope_assets is None or i["object_id"] in scope_objects]
                if events:
                    sets.append({"at": v["created_at"], "events": [
                        {"kind": e["kind"], "asset": e["asset"], "column": e["column"]} for e in events][:30],
                        "impacts": len(impacts)})
            transitions = []
            if metric_code:
                metrics = [self._metric(metric_code)[0]]
            else:
                self.catalog = service.visible_catalog(self.session, self.user, with_values=False)
                metrics = [m for m, _ in self.catalog.versions.values()]
            for metric in metrics:
                for t, mv in self.session.execute(
                        select(MetricVersionTransition, MetricVersion).join(
                            MetricVersion, MetricVersion.id == MetricVersionTransition.metric_version_id)
                        .where(MetricVersion.metric_id == metric.id, MetricVersionTransition.at >= lo,
                               MetricVersionTransition.at < hi)):
                    transitions.append({"metric": metric.name, "version_no": mv.version_no, "from": t.from_status,
                                        "to": t.to_status, "reason": t.reason, "at": t.at})
            note = ("元数据变更按采集时间记录（只能确定发生在两次采集之间）；口径版本流转为治理操作时间。"
                    "本窗口内没有变更不代表数据没有问题，还需查看数据健康与数据本身。")
            # 上下文证据数据集（ADR 0009 第 3 点）：固定结构的事件行、固定排序，注明截取范围，可在报告中引用
            rows = [["元数据变更", _iso(cs["at"]), e["asset"], e["column"], e["kind"], cs["impacts"], None]
                    for cs in sets for e in cs["events"]]
            rows += [["口径版本流转", _iso(t["at"]), t["metric"], f"V{t['version_no']}", f"{t['from'] or '—'} → {t['to']}",
                      None, t["reason"]] for t in transitions]
            rows.sort(key=lambda r: (r[1] or "", r[0], str(r[2]), str(r[3])))
            ds = self._dataset("context", f"变更记录 {start_date}~{end_date}" + (f"（{metric_code}）" if metric_code else ""),
                               ["type", "at", "object", "item", "change", "impacts", "note"], rows,
                               meta={"units": {"impacts": col("count")}, "window": {"start": str(start_date), "end": str(end_date)},
                                     "scope": "每个变更集最多 30 个事件；只含当前用户可见的资产与口径；不是完整历史"})
            return ({"dataset": f"D{ds.seq}", "change_sets": sets, "metric_transitions": transitions, "note": note},
                    ds, "ok")
        return self._guard("changes", {"start_date": start_date, "end_date": end_date, "metric_code": metric_code},
                           run)

    def data_health(self, metric_code: str, start_date: date, end_date: date) -> str:
        def run():
            # 组成指标的业务域与来源闭包逐项核对；采集只给状态与时间，不返回数据源的原始错误
            _, _, versions, _ = self._metric_sources(metric_code)
            out = []
            for v in versions:
                base = self.session.get(Asset, v.base_asset_id) if v.base_asset_id else None
                if base is None:
                    continue
                runs = list(self.session.scalars(select(CollectionRun).where(
                    CollectionRun.datasource_id == base.datasource_id).order_by(CollectionRun.started_at.desc())
                    .limit(5)))
                boundary = freshness_boundary(self.session, base.datasource_id)
                out.append({"metric_version": v.id, "base_asset": f"{base.catalog_name}.{base.db_name}.{base.name}",
                            "last_collected_at": boundary["last_collected_at"],
                            "recent_collections": [{"status": r.status, "started_at": r.started_at} for r in runs],
                            "metric_status": v.status, "drift": bool(v.status != "published")})
            rows = [[o["base_asset"], o["metric_status"], "是" if o["drift"] else "否", _iso(o["last_collected_at"]),
                     c["status"], _iso(c["started_at"])] for o in out for c in o["recent_collections"] or [{}]]
            rows.sort(key=lambda r: (r[0], "" if r[5] is None else r[5]), reverse=False)
            note = ("数据完整性需要看数据本身：用 query_metric 按 time_grain=day 并按成员维度分组，"
                    "找出某天某些成员缺失或骤降；再用 lineage 查看口径的主表（缺失可能来自上游）。")
            ds = self._dataset("context", f"数据健康 {metric_code}",
                               ["base_asset", "metric_status", "drift", "last_collected_at", "collection_status",
                                "collection_started_at"], rows,
                               meta={"window": {"start": str(start_date), "end": str(end_date)},
                                     "scope": "每个来源只列最近 5 次采集；采集只给状态与时间，不含数据源的原始错误"})
            return {"dataset": f"D{ds.seq}", "sources": out, "note": note}, ds, "ok"
        return self._guard("data_health", {"metric_code": metric_code, "start_date": start_date,
                                           "end_date": end_date}, run)

    # -- 受治理查询 --
    def query_metric(self, **kwargs) -> str:
        args = QueryArgs(**kwargs)
        return self._guard("query_metric", args.model_dump(mode="json"), lambda: self._run_query(args))

    def _run_query(self, args: QueryArgs) -> tuple[dict, AnalysisDataset | None, str]:
        """执行一次受治理查询并保存为数据集（query_metric 与 explain_change 的子查询共用）。
        与本次运行已有数据集完全相同的查询（口径、窗口、维度、过滤、排序与行数）在授权核对后直接返回那个数据集，不计额度；
        复用还要求所用的口径版本与当前可查询的版本相同（版本变了就重新查询），不满足请求语义的数据集不复用。"""
        # 按当前授权核对每个请求的口径，只把它们交给查询管线（管线再核对来源闭包、状态与执行前核对）
        scoped = service.Catalog([], {code: self._metric(code) for code in args.metric_codes})
        version_ids = {code: str(getattr(scoped.versions[code][1], "id", "")) for code in args.metric_codes}

        def current(ds: AnalysisDataset | None) -> AnalysisDataset | None:
            return ds if ds is not None and (ds.meta or {}).get("versions") == version_ids else None
        key = json.dumps(args.model_dump(mode="json", exclude={"purpose"}), sort_keys=True, ensure_ascii=False)
        reused = current(self._governed.get(key))
        if reused is not None:
            for code in args.metric_codes:  # 复用前同样核对来源闭包的权限与当前可查询状态（撤权后不再交付）
                self._metric_sources(code)
            ref = f"D{reused.seq}"
            return {**self._view(ref, reused), "reused": True,
                    "note": f"与 {ref} 是相同的受治理查询：直接复用 {ref}，不计查询额度"}, reused, "ok"
        scanned = current(self._scanned.get(_scan_key(args))) if (
            args.group_by and not args.time_grain and args.order is None and args.limit is None) else None
        if scanned is not None:  # explain_change 已扫描过同样的指标 × 维度 × 窗口 × 筛选（含两期）：直接复用
            for code in args.metric_codes:
                self._metric_sources(code)
            ref = f"D{scanned.seq}"
            return {**self._view(ref, scanned), "reused": True,
                    "note": f"explain_change 已按同样的指标、维度、窗口与筛选扫描过：直接复用 {ref}（含本期与对比期），"
                            "不计查询额度"}, scanned, "ok"
        exhausted = self.budget.queries_exhausted()
        if exhausted:
            raise ValueError(f"{exhausted}：查询类工具已停用，请基于已有数据集继续，计算工具与 submit_report 仍可用")
        self.budget.used_queries += 1
        intent = Intent(metric_codes=args.metric_codes, start_date=args.start_date, end_date=args.end_date,
                        filters=args.filters, group_by=args.group_by, time_grain=args.time_grain,
                        compare=args.compare, compare_start=args.compare_start, compare_end=args.compare_end,
                        order=args.order, limit=args.limit)
        started = time.monotonic()
        answer = service.start_answer(self.session, self.user, f"[分析 {self.run.id}] {args.purpose}",
                                      self.adapter, None)
        answer.intent = intent.model_dump(mode="json")
        service.fulfil(self.session, self.user, intent, answer, scoped)
        service.finish_answer(self.session, self.user, answer, started)
        if answer.status == "denied" and answer.error_code == "permission":
            self.denials.append({"message": answer.message, "metric_codes": args.metric_codes,
                                 "answer_id": answer.id})
        if answer.status != "answered":
            return ({"status": answer.status, "error_code": answer.error_code, "message": answer.message,
                     "answer_id": answer.id}, None, answer.status)
        result = answer.result
        names = [scoped.versions[c][0].name for c in args.metric_codes]
        columns = list(result["columns"])
        if columns and columns[-1] == "value" and len(names) == 1:  # 单指标下推时结果列名为 value：统一成指标名
            columns[-1] = names[0]
        if len(set(columns)) != len(columns):  # 列名重复会让按列取值互相覆盖：如实拒绝
            raise calc.CalcError(f"查询结果的列名重复（{'、'.join(columns)}），无法作为数据集使用")
        title = f"{'、'.join(names)} {args.start_date}~{args.end_date}" + (
            f" 按{'、'.join(args.group_by)}" if args.group_by else "") + (
            f" 按{'日' if args.time_grain == 'day' else '月'}" if args.time_grain else "") + (
            f" 对比{ {'previous_period': '上一期', 'previous_year': '去年同期'}.get(args.compare) or f'{args.compare_start}~{args.compare_end}'}"
            if args.compare else "")
        versions = {scoped.versions[c][0].name: scoped.versions[c][1] for c in args.metric_codes}
        units = measures.governed_units(columns, versions,
                                        service.PERIOD_LABEL[args.compare] if args.compare else None)
        ds = self._dataset("governed", title, columns, result["rows"], answer_id=answer.id,
                           truncated=bool(result.get("truncated")),
                           meta={"sql": answer.sql, "metric_version_ids": answer.metric_version_ids,
                                 "metric_codes": list(args.metric_codes), "versions": version_ids,
                                 "intent": answer.intent, "freshness": answer.freshness,
                                 "purpose": args.purpose,
                                 "time_column": columns[0] if args.time_grain else None, "units": units,
                                 "window": {"start": str(args.start_date), "end": str(args.end_date)}})
        self._governed[key] = ds
        return self._view(f"D{ds.seq}", ds), ds, "ok"

    # -- 探索查询 --
    EXPLORE_MAX_SECONDS = 60
    EXPLORE_MEM_LIMIT = 2 * 1024 ** 3

    def explore_available(self) -> bool:
        """有开启了探索查询、引擎有探索档案的数据源时才提供 query_sql / sample_values。"""
        from avelis.analysis.explore import profile_for
        from avelis.engines import engine_of
        from avelis.engines.base import QUERY

        try:
            sources = list(self.session.scalars(select(Datasource).where(Datasource.allow_exploration.is_(True))))
        except Exception:  # noqa: BLE001 - 测试替身没有数据源表
            return False
        return any(isinstance(ds, Datasource) and profile_for(ds.kind) and engine_of(ds).supports(QUERY)
                   for ds in sources)

    def query_sql(self, **kwargs) -> str:
        args = QuerySqlArgs(**kwargs)
        return self._guard("query_sql", args.model_dump(mode="json"), lambda: self._explore(
            args.sql, args.purpose, title=f"探索：{args.purpose}"[:255]))

    def sample_values(self, **kwargs) -> str:
        args = SampleArgs(**kwargs)

        def run():
            asset = self.session.scalar(select(Asset).where(
                Asset.catalog_name + "." + Asset.db_name + "." + Asset.name == args.asset, Asset.status == "active"))
            if asset is None:
                self.budget.used_explore += 1  # 失败的调用同样计数（探索上限与查询额度）
                self.budget.used_queries += 1
                raise ValueError(f"资产 {args.asset} 不存在或当前用户不可见")
            from sqlglot import exp

            from avelis.analysis.explore import STANDARD_DIALECT

            column = exp.column(args.column, quoted=True)
            query = (exp.select(exp.alias_(column, "value", quoted=True),
                                exp.alias_(exp.Count(this=exp.Star()), "n", quoted=True))
                     .from_(exp.table_(asset.name, db=asset.db_name, catalog=asset.catalog_name, quoted=True))
                     .group_by(column).order_by(exp.Ordered(this=exp.column("n", quoted=True), desc=True)))
            if args.keyword:  # 关键字按文本匹配：先转成 VARCHAR，数值、日期列也能用（不把 LIKE 用在非文本列上）
                text = exp.Cast(this=column.copy(), to=exp.DataType.build("VARCHAR"))
                query = query.where(exp.Like(this=text, expression=exp.Literal.string(f"%{args.keyword}%")))
            # 与 query_sql 同一路径：标准 SQL → 按资产路由数据源 → 校验 → 生成目标方言
            return self._explore(query.sql(dialect=STANDARD_DIALECT), f"列 {args.column} 的常见取值（有范围的预览）",
                                 title=f"取值预览：{args.asset}.{args.column}"[:255], max_rows=20, preview=True)
        return self._guard("sample_values", args.model_dump(mode="json"), run)

    def _explore(self, sql_text: str, purpose: str, *, title: str, max_rows: int | None = None,
                 preview: bool = False) -> tuple[dict, AnalysisDataset | None, str]:
        """探索查询：计数 → 按资产路由数据源与档案 → 允许清单校验与绑定（标准 SQL → 目标方言）→ 物理身份核对 →
        权限复核 → 执行 → 交付前权限复核 → 保存为 explore 数据集。拒绝时不发到数据库；每次都记审计。"""
        from avelis.analysis import exploration
        from avelis.engines import query_connector

        exhausted = self.budget.queries_exhausted()
        if exhausted:
            raise ValueError(f"{exhausted}：查询类工具已停用，请基于已有数据集继续，计算工具与 submit_report 仍可用")
        if self.budget.used_explore >= self.budget.explore:
            raise ValueError(f"探索查询已达上限 {self.budget.explore} 次：请基于已有数据集继续")
        self.budget.used_explore += 1
        self.budget.used_queries += 1
        step, source, validated, sent, result = self.budget.used_explore, None, None, False, None
        max_rows = max_rows or get_settings().query_max_rows
        limits = {"max_rows": max_rows, "mem_limit_bytes": self.EXPLORE_MEM_LIMIT}
        try:
            source = exploration.route(self.session, self.user, sql_text)
            if self.budget.remaining() <= self.SUBQUERY_RESERVE_SECONDS:
                raise exploration.ExploreRejected("binding", "deadline", "接近统一截止时间，不再执行探索查询")
            validated = exploration.validate(sql_text, source.profile,
                                             exploration.resolver(self.session, self.user, source), max_rows=max_rows)
            connector = query_connector(self.session, source.datasource)
            check_timeout = int(min(self.EXPLORE_MAX_SECONDS, self.budget.remaining() - 5))
            if check_timeout <= 0:
                raise exploration.ExploreRejected("binding", "deadline", "接近统一截止时间，不再执行探索查询")
            exploration.live_check(connector, validated, timeout_seconds=check_timeout)
            self._authorize()
            exploration.recheck_access(self.session, self.user, validated)
            # 派发前重新检查截止时间：执行前核对可能已耗时；超时值 ≤ 0 时 statement_timeout = 0 等于不限时，必须拒绝
            timeout = int(min(self.EXPLORE_MAX_SECONDS, self.budget.remaining() - 5))
            limits["timeout_seconds"] = timeout
            if timeout <= 0:
                raise exploration.ExploreRejected("binding", "deadline", "接近统一截止时间，不再执行探索查询")
            sent = True
            result = connector.execute_explore(validated.sql, timeout_seconds=timeout, max_rows=max_rows,
                                               mem_limit_bytes=self.EXPLORE_MEM_LIMIT)
            self._authorize()  # 交付前再核对：撤权后不再交付结果
            exploration.recheck_access(self.session, self.user, validated)
            if list(result.columns) != [c.name for c in validated.columns]:
                raise exploration.ExploreRejected("evidence", "columns_mismatch", "数据库返回的列与校验结果不一致，不交付")
        except exploration.ExploreRejected as e:
            exploration.record(self.session, self.user, self.run.id, step=step, source=source, purpose=purpose,
                               outcome="withheld" if sent else "rejected", validated=validated, error=e, limits=limits)
            return e.view() | ({"sent_to_database": True, "note": "结果未交付"} if sent else {}), None, "error"
        except QueryError as e:
            exploration.record(self.session, self.user, self.run.id, step=step, source=source, purpose=purpose,
                               outcome="failed", validated=validated, error=e, limits=limits)
            return {"error": f"探索查询执行失败：{e.message}", "code": e.code}, None, "error"
        except PermissionLost as e:
            self.permission_lost = e.message
            exploration.record(self.session, self.user, self.run.id, step=step, source=source, purpose=purpose,
                               outcome="withheld" if sent else "rejected", validated=validated,
                               error=exploration.ExploreRejected("binding", "permission_lost", e.message), limits=limits)
            return {"error": f"{e.message}：之后的取数与上下文一律拒绝，分析停止", "status": "denied"}, None, "denied"
        exploration.record(self.session, self.user, self.run.id, step=step, source=source, purpose=purpose, outcome="ok",
                           validated=validated, result=result, limits=limits,
                           identity=exploration.execution_identity(self.session, source.datasource))
        ds = self._dataset("explore", title, list(result.columns), result.rows, truncated=result.truncated,
                           meta={"sql": validated.sql, "datasource_id": source.datasource.id,
                                 "datasource": source.datasource.name, "engine": source.datasource.kind,
                                 "profile": validated.profile, "sqlglot": validated.sqlglot, "purpose": purpose,
                                 "tables": [{"asset_id": t.asset_id, "asset_version_id": t.asset_version_id,
                                             "qualified": t.qualified} for t in validated.tables],
                                 "units": exploration.units(validated.columns),
                                 "column_evidence": exploration.evidence(validated.columns), "preview": preview})
        view = self._view(f"D{ds.seq}", ds)
        view["sql"] = validated.sql
        view["datasource"], view["engine"] = source.datasource.name, source.datasource.kind
        view["note"] = ("探索查询（未经口径审核）：引用这些数字的结论会带探索标记；" +
                        ("这是有范围的取值预览，不是全集。" if preview else "量纲由后端按表达式推断。"))
        return view, ds, "ok"

    # -- 自动归因 --
    SUBQUERY_RESERVE_SECONDS = 15  # 子查询前至少要剩的时间：不够就停止扫描，返回 partial

    def _subquery_gate(self, metric_code: str) -> str | None:
        """每个子查询前的核对：执行权、账号与角色、口径与来源闭包的权限（失败即抛出，按 ADR 0007 处理）；
        查询额度与统一截止时间不够时返回原因（停止扫描，结果标为 partial）。"""
        self.check_executor()
        if self.aborted:
            raise calc.CalcError(self.aborted)
        self._authorize()
        self._metric_sources(metric_code)
        exhausted = self.budget.queries_exhausted()
        if exhausted:
            return exhausted
        if self.budget.remaining() <= self.SUBQUERY_RESERVE_SECONDS:
            return "接近统一截止时间"
        return None

    def explain_change(self, **kwargs) -> str:
        """自动归因（ADR 0009 第 4 点）：按候选维度各发一次受治理查询，计算成员贡献与分布变化强度，给出排名。"""
        args = ExplainArgs(**kwargs)

        def run():
            try:
                return self._explain(args)
            except PermissionLost as exc:  # 中途撤权：之后一律拒绝，分析停止
                self.permission_lost = exc.message
                return {"error": f"{exc.message}：之后的取数与上下文一律拒绝，分析停止", "status": "denied"}, None, "denied"
        return self._guard("explain_change", args.model_dump(mode="json"), run)

    def _window(self, start: date | None, end: date | None, cstart: date | None, cend: date | None) -> dict[str, str | None]:
        """本期、对比期的起止日期：没写的取本次分析的窗口（explain_change 与报告 scope 共用，登记键才一致）。"""
        window = self.run.window or {}
        picked = (start or _opt_date(window.get("start")), end or _opt_date(window.get("end")),
                  cstart or _opt_date(window.get("compare_start")), cend or _opt_date(window.get("compare_end")))
        return {k: str(v) if v else None for k, v in zip(("start", "end", "compare_start", "compare_end"), picked)}

    def _explain(self, args: ExplainArgs) -> tuple[dict, AnalysisDataset | None, str]:
        window = self._window(args.start_date, args.end_date, args.compare_start, args.compare_end)
        if not all(window.values()):
            raise ValueError("自动归因需要本期与对比期：本次分析没有窗口时请传 start_date、end_date、compare_start、compare_end")
        start, end, cstart, cend = (date.fromisoformat(window[k]) for k in ("start", "end", "compare_start", "compare_end"))
        key = scope_key(args.metric_code, args.filters, window)
        metric, version, _, _ = self._metric_sources(args.metric_code)
        components = {}
        if version.kind == "derived":
            for c in version.components or []:
                components[c["metric_code"]] = self.session.get(MetricVersion, c["version_id"])
        rule = attribution.applicability(version, components)
        if rule["algorithm"] is None:
            self.explanations[key] = "unsupported"
            return {"supported": False, "metric_code": args.metric_code, "reason": rule["reason"],
                    "note": "不支持自动归因：可以用 query_metric 按维度查询后用 contribution 拆解，或说明无法归因"}, None, "ok"
        allowed = [d["name"] for d in version.dimensions or []]
        filtered = {f.dimension for f in args.filters}  # 被筛选的维度只有选中的成员：不再作为候选维度扫描
        dims = [d for d in dict.fromkeys(args.dimensions) if d not in filtered] or [
            d for d in allowed if d not in filtered][:8]
        unknown = [d for d in dims if d not in allowed]
        if unknown:
            raise ValueError(f"维度 {'、'.join(unknown)} 不是指标 {args.metric_code} 允许的维度（可用：{'、'.join(allowed) or '无'}）")
        ratio = rule["algorithm"] == "ratio"
        codes = [rule["numerator"], rule["denominator"]] if ratio else [args.metric_code]
        names = [self.session.get(Metric, c["metric_id"]).name for code in codes for c in version.components
                 if c["metric_code"] == code] if ratio else [metric.name]
        inputs: list[str] = []
        unscanned: list[dict] = []

        def query(group_by: list[str], purpose: str, compare: bool = True) -> tuple[AnalysisDataset | None, str | None]:
            blocked = self._subquery_gate(args.metric_code)
            if blocked:
                return None, blocked
            qa = QueryArgs(metric_codes=codes, start_date=start if compare else cstart, end_date=end if compare else cend,
                           filters=args.filters, group_by=group_by, compare="custom" if compare else None,
                           compare_start=cstart if compare else None, compare_end=cend if compare else None,
                           purpose=f"{args.purpose}：{purpose}")
            out, ds, status = self._run_query(qa)
            if ds is not None and compare and not ds.truncated:  # 模型再按同样的维度、两期分组取数时直接复用
                self._scanned.setdefault(_scan_key(qa), ds)
            if ds is None:
                if status == "denied" and out.get("error_code") == "permission":
                    raise PermissionDenied(out.get("message") or "查询被拒绝")
                return None, out.get("message") or status
            inputs.append(f"D{ds.seq}")
            if ds.truncated:
                return None, "结果被截断（成员过多），不是全集"
            return ds, None

        def cells(ds: AnalysisDataset, key_cols: list[str]) -> dict[tuple, list]:
            out = {}
            for r in (dict(zip(ds.columns, row)) for row in ds.rows):
                vals = []
                for n in names:
                    vals += [calc.number(r.get(f"{n}·对比期")), calc.number(r.get(n))]
                out[tuple(r.get(k) for k in key_cols)] = vals  # [prev, cur] 或 [a0, a1, b0, b1]
            return out

        total_ds, reason = query([], "总量")
        if total_ds is None:
            raise calc.CalcError(f"总量查询没有完成（{reason}），不能归因")
        totals = next(iter(cells(total_ds, []).values()), None)
        if totals is None or any(v is None for v in totals):
            raise calc.CalcError("总量查询没有返回两期的值，不能归因")
        if ratio:
            a0, a1, b0, b1 = totals
            if not b0 or not b1:
                raise calc.CalcError("比率的分母总量为 0，不能归因")
            total_prev, total_cur = a0 / b0, a1 / b1
        else:
            total_prev, total_cur = totals
        total_change = total_cur - total_prev
        configured = getattr(version, "material_change_ratio", None)
        threshold = calc.Decimal(str(configured if configured is not None else get_settings().material_change_ratio))

        def scan(group_by: list[str]) -> attribution.Dimension | None:
            label = "×".join(group_by)
            ds, why = query(group_by, f"按{label}")
            if ds is None:
                unscanned.append({"dimension": label, "reason": why})
                return None
            members = cells(ds, group_by)
            prev_parts = lambda: [v[0] for v in members.values() if v[0] is not None]  # noqa: E731
            needs_exits = not attribution.reconciles(prev_parts(), totals[0]) or (
                ratio and not attribution.reconciles([v[2] for v in members.values() if v[2] is not None], totals[2]))
            if needs_exits:  # 对比期查询只带本期出现的成员：补查对比期，找出退出的成员
                before, why = query(group_by, f"按{label}（对比期成员）", compare=False)
                if before is None:
                    unscanned.append({"dimension": label, "reason": f"需要补查对比期成员，但{why}"})
                    return None
                for r in (dict(zip(before.columns, row)) for row in before.rows):
                    key = tuple(r.get(k) for k in group_by)
                    if key not in members:
                        vals = [calc.number(r.get(n)) for n in names]
                        members[key] = [vals[0], None] if not ratio else [vals[0], None, vals[1], None]
            checks = [(0, totals[0]), (1, totals[1])] + ([(2, totals[2]), (3, totals[3])] if ratio else [])
            for i, total in checks:
                if not attribution.reconciles([v[i] for v in members.values() if v[i] is not None], total):
                    unscanned.append({"dimension": label, "reason": "成员合计与总量对不上（可能有重复计数或过滤差异），拒绝归因"})
                    return None
            dim = attribution.Dimension(label)
            if ratio:
                values = {k: (v[0], v[2], v[1], v[3]) for k, v in members.items()}
                dim.rows, _ = attribution.ratio_members(values, (a0, b0, a1, b1), total_change)
                dim.js = attribution.js_divergence([v[2] or calc.Decimal(0) for v in members.values()],
                                                   [v[3] or calc.Decimal(0) for v in members.values()])
            else:
                dim.rows = attribution.additive_members({k: (v[0], v[1]) for k, v in members.items()}, total_change)
                dim.js = attribution.js_divergence([v[0] or calc.Decimal(0) for v in members.values()],
                                                   [v[1] or calc.Decimal(0) for v in members.values()])
            return attribution.judge(dim, total_prev, total_change, args.top_k, threshold)

        scanned = [d for d in (scan([name]) for name in dims) if d is not None]
        if args.depth == 2:
            top = [d.name for d in attribution.rank(scanned)[:3]]
            for i, first in enumerate(top):
                for second in top[i + 1:]:
                    pair = scan(sorted([first, second]))
                    if pair is not None:
                        scanned.append(pair)
        ranked = attribution.rank(scanned)
        conclusion = attribution.verdict(ranked, bool(scanned), attribution.immaterial(total_prev, total_change, threshold),
                                         candidates=bool(dims))
        self.explanations[key] = conclusion
        factor = None if ratio else self._auto_factor(args, metric, totals, (start, end, cstart, cend), inputs)
        primary = next((d for d in ranked if d.verdict == "ranked"), None)
        cols = ["dimension", "member", "previous", "current", "member_change", "contribution", "ep",
                "structure_effect", "rate_effect", "change_type", "role"]
        rows = [[d.name, *[r[c] for c in cols[1:-1]], r["role"]] for d in ranked for r in d.selected]
        summary = {
            "metric_code": args.metric_code, "algorithm": rule["algorithm"],
            "total": {"previous": attribution.q(total_prev) if ratio else total_prev,
                      "current": attribution.q(total_cur) if ratio else total_cur,
                      "change": attribution.q(total_change) if ratio else total_change},
            "verdict": conclusion, "material_change_ratio": threshold,
            "scope": {"metric_code": args.metric_code, "filters": [f.model_dump() for f in args.filters],  # 字段同报告 scope
                      "start_date": window["start"], "end_date": window["end"],
                      "compare_start": window["compare_start"], "compare_end": window["compare_end"]},
            "conclusion": conclusion,
            "factor_decomposition": factor, "primary_dimension": primary.name if primary else None,
            "cumulative_ep": primary.cumulative_ep if primary else None,
            "threshold_met": primary.threshold_met if primary else None,
            "partial": bool(unscanned), "scanned": [d.name for d in scanned], "unscanned": unscanned,
            "dimensions": [{"dimension": d.name, "js": d.js, "verdict": d.verdict, "cumulative_ep": d.cumulative_ep,
                            "threshold_met": d.threshold_met} for d in ranked],
            "note": ("EP 是带符号的净贡献比例（可以超过 100%），各维度的 EP 不跨维度相加；排名只在已扫描的维度内成立；"
                     "相互抵消或变化不明显时不排名，分别列出增加项与减少项"
                     + ("；比率拆解：结构效应 + 比率效应，交互项归入比率效应，这种拆法不唯一" if ratio else "")),
        }
        if ratio:
            covered = sum((r["contribution"] for r in scanned[0].rows if r["contribution"] is not None),
                          calc.Decimal(0)) if scanned else calc.Decimal(0)
            summary["residual"] = attribution.q(total_change - covered)  # 不可计算成员与舍入：单列，不归给任何成员
        base = measures.metric_unit(version)
        delta = like(base, "delta")
        units = {"previous": base, "current": base, "member_change": delta, "contribution": delta,
                 "ep": col("share"), "structure_effect": delta, "rate_effect": delta}
        summary_units = {"total.previous": base, "total.current": base, "total.change": delta,
                         "cumulative_ep": col("share"), "material_change_ratio": col("share"),
                         **({"residual": delta} if ratio else {})}
        title = f"自动归因：{metric.name} {start}~{end} 对比 {cstart}~{cend}"
        ds = self._dataset("calc", title, cols, rows, inputs=inputs,
                           meta={"summary": summary, "args": args.model_dump(mode="json"), "units": units,
                                 "summary_units": summary_units, "partial": bool(unscanned),
                                 "metric_codes": sorted({args.metric_code, *codes})})
        view = self._view(f"D{ds.seq}", ds)
        view["conclusion_note"] = (f"对 {describe_scope(key)}的判定：{summary['conclusion']}。报告的 conclusion 必须与它一致，"
                                   "scope 照抄 summary.scope（同样的 metric_code、filters 与窗口）")
        return view, ds, "ok"

    def _auto_factor(self, args: ExplainArgs, metric: Metric, totals: list, period: tuple,
                     inputs: list[str]) -> dict | None:
        """所问指标是某个已发布比率口径 a / b 的分子时，同时给出 a = b × (a/b) 的 LMDI 因子拆解（ADR 0009 第 7 点）：
        按口径元数据判断，与行业无关。比率口径或分母对当前用户不可见、预算不够、取值不为正时跳过并写明原因。"""
        candidates = []
        try:
            published = list(self.session.scalars(select(MetricVersion).where(
                MetricVersion.status == "published", MetricVersion.kind == "derived")))
        except Exception:  # noqa: BLE001 - 测试替身没有口径表
            return None
        for version in published:
            if not isinstance(version, MetricVersion):
                continue
            m = attribution.RATIO_FORMULA.match(version.expression or "")
            if m and m.group(1) == args.metric_code:
                ratio_metric = self.session.get(Metric, version.metric_id)
                if ratio_metric is not None:
                    candidates.append((ratio_metric.code, m.group(2), ratio_metric.name))
        if not candidates:
            return None
        ratio_code, denominator, ratio_name = sorted(candidates)[0]
        try:
            self._metric(ratio_code)
            denom_metric = self._metric(denominator)[0]
        except Exception as exc:  # noqa: BLE001 - 不可见、未发布：不拆
            return {"skipped": f"比率口径 {ratio_code} 或分母 {denominator} 当前不可用（{getattr(exc, 'message', exc)}）"}
        blocked = self._subquery_gate(args.metric_code)
        if blocked:
            return {"skipped": blocked, "ratio_metric": ratio_code}
        start, end, cstart, cend = period
        out, b_ds, status = self._run_query(QueryArgs(
            metric_codes=[denominator], start_date=start, end_date=end, filters=args.filters, compare="custom",
            compare_start=cstart, compare_end=cend, purpose=f"{args.purpose}：因子拆解的分母"))
        if b_ds is None:
            return {"skipped": out.get("message") or status, "ratio_metric": ratio_code}
        inputs.append(f"D{b_ds.seq}")
        row = dict(zip(b_ds.columns, b_ds.rows[0])) if b_ds.rows else {}
        b0, b1 = calc.number(row.get(f"{denom_metric.name}·对比期")), calc.number(row.get(denom_metric.name))
        a0, a1 = totals
        if not b0 or not b1:
            return {"skipped": "分母为 0，不能拆解", "ratio_metric": ratio_code}
        try:
            out = calc.factor_decompose((a0, a1), {denominator: (b0, b1), ratio_code: (a0 / b0, a1 / b1)})
        except calc.CalcError as exc:
            return {"skipped": str(exc), "ratio_metric": ratio_code}
        names = {denominator: denom_metric.name, ratio_code: ratio_name}
        cols = ["factor_metric_code", "factor", "previous", "current", "change_rate", "contribution", "share"]
        rows = [[r["factor"] if r["factor"] in names else None, names.get(r["factor"], r["factor"]),
                 r["previous"], r["current"], r["change_rate"], r["contribution"], r["share"]] for r in out["factors"]]
        base = measures.metric_unit(self._metric_sources(args.metric_code)[1])
        units = {"previous": col("unknown"), "current": col("unknown"), "change_rate": col("rate_change"),
                 "contribution": like(base, "delta"), "share": col("share")}
        ds = self._dataset("calc", f"因子拆解：{metric.name} = {denom_metric.name} × {ratio_name}", cols, rows,
                           inputs=list(dict.fromkeys(inputs)),
                           meta={"summary": {"total": out["total"], "unexplained": out["unexplained"],
                                             "ratio_metric": ratio_code},
                                 "units": units, "metric_codes": sorted({args.metric_code, denominator, ratio_code}),
                                 "summary_units": {"total.previous": base, "total.current": base,
                                                   "total.change": like(base, "delta"),
                                                   "unexplained": like(base, "delta")}})
        return {"dataset": f"D{ds.seq}", "ratio_metric": ratio_code, "factors": [denominator, ratio_code]}

    # -- 计算 --
    def _calc(self, tool: str, args: dict, inputs: list[str], fn, *, full_set: bool = True) -> str:
        """full_set：计算需要全集（合计、覆盖度、相关性等），截断的输入直接拒绝。只取单行的计算可以用截断的输入，
        结果沿依赖继承截断标记。"""
        def run():
            truncated = [ref for ref in inputs if ref in self._datasets and self._datasets[ref].truncated]
            if truncated and full_set:
                raise calc.CalcError(f"数据集 {'、'.join(truncated)} 被截断（不是全集），不能用于 {tool}："
                                     "请缩小时间范围、减少分组或加筛选后重新查询")
            title, columns, rows, summary, *extra = fn()
            registered = dict(extra[0]) if extra else {}  # {units, summary_units, assumption}：量纲登记（ADR 0009）
            # 读取的输入列继承证据资格：用了假设列（常量、函数加工）的结果是假设计算；复制到结果里的列保留原列的证据
            evidence = self._referenced_evidence(inputs, args)
            if any(ev.get("assumption") for ev in evidence.values()):
                registered["assumption"] = True
            copied = {col: ev for col, ev in evidence.items() if col in columns}
            if copied:
                registered["column_evidence"] = {**registered.get("column_evidence", {}), **copied}
            ds = self._dataset("calc", title, columns, rows, inputs=inputs, truncated=bool(truncated),
                               meta={"summary": summary, "args": args, **registered})
            return self._view(f"D{ds.seq}", ds), ds, "ok"
        return self._guard(tool, args, run)

    def _referenced_evidence(self, inputs: list[str], args: Any) -> dict[str, dict]:
        """参数里引用到的输入列（任一输入数据集里同名的列）及其证据资格；没有证据记录的列（口径、计算结果）不列出。"""
        names: set[str] = set()

        def walk(value: Any) -> None:
            if isinstance(value, str):
                names.add(value)
            elif isinstance(value, dict):
                for v in value.values():
                    walk(v)
            elif isinstance(value, (list, tuple)):
                for v in value:
                    walk(v)
        walk(args)
        out: dict[str, dict] = {}
        for ref in inputs:
            ds = self._datasets.get(ref)
            for col, ev in ((ds.meta or {}).get("column_evidence") or {}).items() if ds else []:
                if col in names:
                    prior = out.get(col)  # 同名列出现在多个输入里：取更严格的一个
                    out[col] = ev if prior is None else {**prior, "eligible": prior.get("eligible") and ev.get("eligible"),
                                                         "label_ok": prior.get("label_ok") and ev.get("label_ok"),
                                                         "assumption": prior.get("assumption") or ev.get("assumption")}
        return out

    def contribution(self, dataset: str, keys: list[str], current: str, previous: str) -> str:
        def fn():
            _, rows = self._rows(dataset)
            out = calc.contribution(rows, keys, current, previous)
            cols = [*keys, "previous", "current", "change", "change_rate", "share_of_change"]
            base = self._unit(dataset, current)
            units = {"previous": base, "current": base, "change": like(base, "delta"),
                     "change_rate": col("rate_change"), "share_of_change": col("share")}
            return (f"{dataset} 按{'、'.join(keys)}的差额拆解", cols, [[r[c] for c in cols] for r in out["rows"]],
                    {"total": out["total"]}, {"units": units, "summary_units": {
                        "total.previous": base, "total.current": base, "total.change": like(base, "delta"),
                        "total.change_rate": col("rate_change")}})
        return self._calc("contribution", {"dataset": dataset, "keys": keys, "current": current,
                                           "previous": previous}, [dataset], fn)

    @staticmethod
    def _pick(rows: list[dict], match: dict[str, str]) -> dict:
        hits = [r for r in rows if all(str(r.get(k)) == str(v) for k, v in match.items())]
        if len(hits) != 1:
            raise calc.CalcError(f"按 {match or '（无条件）'} 选出 {len(hits)} 行，需要正好 1 行")
        return hits[0]

    def factor_decompose(self, dataset: str, total: Pair, factors: list[Pair], match: dict[str, str] | None = None) -> str:
        total, factors = _model(Pair, total), [_model(Pair, f) for f in factors]

        def fn():
            _, rows = self._rows(dataset)
            row = self._pick(rows, match or {})
            out = calc.factor_decompose((row[total.previous], row[total.current]),
                                        {f.name: (row[f.previous], row[f.current]) for f in factors})
            codes = {f.name: f.metric_code for f in factors}
            cols = ["factor_metric_code", "factor", "previous", "current", "change_rate", "contribution", "share"]
            for f in out["factors"]:
                f["factor_metric_code"] = codes.get(f["factor"])
            base = self._unit(dataset, total.current)
            units = {"previous": col("unknown"), "current": col("unknown"), "change_rate": col("rate_change"),
                     "contribution": like(base, "delta"), "share": col("share")}
            return (f"{dataset} {total.name}的因子拆解", cols, [[f[c] for c in cols] for f in out["factors"]],
                    {"total": out["total"], "unexplained": out["unexplained"]}, {"units": units, "summary_units": {
                        "total.previous": base, "total.current": base, "total.change": like(base, "delta"),
                        "unexplained": like(base, "delta")}})
        return self._calc("factor_decompose", {"dataset": dataset, "total": total.model_dump(),
                                               "factors": [f.model_dump() for f in factors], "match": match},
                          [dataset], fn, full_set=False)

    def calendar_adjust(self, dataset: str, current: str, previous: str, match: dict[str, str] | None = None) -> str:
        def fn():
            ds, rows = self._rows(dataset)
            current_period, previous_period = _periods(ds, current, previous)
            row = self._pick(rows, match or {})
            out = calc.calendar_adjust(row[current], row[previous], current_period, previous_period, HOLIDAYS)
            cols = ["period", "days", "weekend_days", "holidays", "value", "daily"]
            rows_out = [["本期", *[out["current"][c] for c in cols[1:]]], ["上期", *[out["previous"][c] for c in cols[1:]]]]
            base = self._unit(dataset, current)
            delta, days = like(base, "delta"), col("count", "天")
            units = {"days": days, "weekend_days": days, "holidays": days, "value": base, "daily": base}
            return (f"{dataset} {current}的日历折算", cols, rows_out,
                    {k: out[k] for k in ("change", "day_count_effect", "daily_level_effect", "daily_change_rate")},
                    {"units": units, "summary_units": {"change": delta, "day_count_effect": delta,
                                                       "daily_level_effect": delta,
                                                       "daily_change_rate": col("rate_change")}})
        return self._calc("calendar_adjust", {"dataset": dataset, "current": current, "previous": previous,
                                              "match": match}, [dataset], fn, full_set=False)

    def correlate(self, dataset: str, x: str, y: str) -> str:
        def fn():
            _, rows = self._rows(dataset)
            out = calc.correlate(rows, x, y)
            cols = list(out)
            units = {c: col("count") if c == "n" else col("unknown") for c in ("n", "pearson", "spearman", "p_value")
                     if c in out}
            return f"{dataset} {x} 与 {y} 的相关性", cols, [[out[c] for c in cols]], out, {
                "units": units, "summary_units": units}
        return self._calc("correlate", {"dataset": dataset, "x": x, "y": y}, [dataset], fn)

    def coverage(self, dataset: str, keys: list[str] | None = None, period: str | None = None,
                 start_date: date | None = None, end_date: date | None = None) -> str:
        def fn():
            ds, rows = self._rows(dataset)
            period_col = period or ds.meta.get("time_column")
            if period and period not in ds.columns:
                raise calc.CalcError(f"{dataset} 没有日期列 {period}")
            period_col = period_col if period_col in ds.columns else None
            out = calc.coverage(rows, keys or [], period_col, _date(start_date), _date(end_date))
            cols = [*(keys or []), "rows", "periods", "missing_count", "missing_periods"]
            counts = {c: col("count") for c in ("rows", "periods", "missing_count")}
            return (f"{dataset} 的覆盖度", cols, [[r.get(c) for c in cols] for r in out["rows"]], out["summary"],
                    {"units": counts, "summary_units": {k: col("count") for k in out["summary"]}})
        return self._calc("coverage", {"dataset": dataset, "keys": keys, "period": period, "start_date": start_date,
                                       "end_date": end_date}, [dataset], fn)

    def derive(self, label: str, values: list[ValueRef], formula: str) -> str:
        """派生数字（差、和、比）：数值从数据集取，公式按白名单求值，结果成为新的数据集，可在报告中引用。"""
        from avelis.governance.derived import evaluate, parse_formula

        values = [_model(ValueRef, v) for v in values]

        def fn():
            names = parse_formula(formula)
            given = {v.name for v in values}
            missing = [n for n in names if n not in given]
            if missing:
                raise calc.CalcError(f"公式中的 {'、'.join(missing)} 没有对应的取值")
            env, rows_out = {}, []
            for v in values:
                ds, rows = self._rows(v.dataset)
                picked = [r for r in rows if all(str(r.get(k)) == str(x) for k, x in v.match.items())
                          and not (v.exclude and all(str(r.get(k)) == str(x) for k, x in v.exclude.items()))]
                if not picked:
                    raise calc.CalcError(f"{v.dataset} 中按 {v.match or '（无条件）'} 没有选出行")
                if ds.truncated and (len(picked) != 1 or v.exclude):  # 截断的数据集只能取单行，不能求和或排除
                    raise calc.CalcError(f"数据集 {v.dataset} 被截断（不是全集）：只能按条件取单行，不能求和或排除")
                if v.column not in picked[0]:
                    raise calc.CalcError(f"{v.dataset} 没有列 {v.column}")
                total = sum((calc.number(r[v.column]) or 0 for r in picked), calc.Decimal(0))
                env[v.name] = total
                rows_out.append([v.name, v.dataset, v.column, len(picked), total])
            result = evaluate(formula, env)
            if result is None:
                raise calc.CalcError("结果无法计算（除数为 0 或缺值）")
            # 每个输入都必须真正影响结果（拒绝 a*0+99999 这类写法）；非白名单常量把结果标为假设计算（ADR 0009 第 3 点）
            dead = measures.insensitive(formula, env, evaluate)
            if dead:
                raise calc.CalcError(f"输入 {'、'.join(dead)} 不影响结果：每个取值都必须参与计算")
            assumption = bool(measures.constants(formula))
            unit = measures.formula_unit(formula, {v.name: self._unit(v.dataset, v.column) for v in values})
            rows_out.append([label, None, formula, None, result])
            summary = {"label": label, "formula": formula, "value": result,
                       **({"assumption": "公式含 1、100、1000、10000、1e8 以外的常量：假设计算，只能被推断、建议引用"}
                          if assumption else {})}
            # value 列里也有各输入的取值（量纲各不相同），只登记汇总值 summary.value 的量纲
            return (f"派生：{label}", ["name", "dataset", "column", "rows", "value"], rows_out, summary,
                    {"units": {"rows": col("count")}, "summary_units": {"value": unit}, "assumption": assumption})
        return self._calc("derive", {"label": label, "values": [v.model_dump() for v in values], "formula": formula},
                          sorted({v.dataset for v in values}), fn, full_set=False)

    def describe(self, dataset: str, column: str, match: dict[str, str] | None = None) -> str:
        def fn():
            _, rows = self._rows(dataset)
            picked = [r for r in rows if all(str(r.get(k)) == str(v) for k, v in (match or {}).items())]
            if picked and column not in picked[0]:
                raise calc.CalcError(f"{dataset} 没有列 {column}")
            out = calc.describe([r.get(column) for r in picked])
            cols = list(out)
            base = self._unit(dataset, column)
            units = {c: col("count") if c == "count" else dict(base) for c in cols}
            return f"{dataset} {column}的描述统计", cols, [[out[c] for c in cols]], out, {
                "units": units, "summary_units": units}
        return self._calc("describe", {"dataset": dataset, "column": column, "match": match}, [dataset], fn)

    def segment_compare(self, dataset: str, segment: str, measures: list[str]) -> str:
        def fn():
            _, rows = self._rows(dataset)
            out = calc.segment_compare(rows, segment, measures)
            cols = list(out[0]) if out else [segment]
            units = {"count": col("count")}
            for m in measures:
                units.update({f"{m}_{k}": self._unit(dataset, m) for k in ("sum", "mean", "median")})
            return f"{dataset} 按{segment}分组对比", cols, [[r[c] for c in cols] for r in out], None, {"units": units}
        return self._calc("segment_compare", {"dataset": dataset, "segment": segment, "measures": measures},
                          [dataset], fn)

    def outliers(self, dataset: str, value: str, keys: list[str]) -> str:
        def fn():
            _, rows = self._rows(dataset)
            out = calc.outliers(rows, value, keys)
            cols = [*keys, value, "side"]
            base = self._unit(dataset, value)
            summary = {k: v for k, v in out.items() if k != "rows"}
            return (f"{dataset} {value}的异常值", cols, [[r.get(c) for c in cols] for r in out["rows"]], summary,
                    {"units": {value: base}, "summary_units": {k: dict(base) for k in ("q1", "q3", "low", "high")
                                                               if k in summary}})
        return self._calc("outliers", {"dataset": dataset, "value": value, "keys": keys}, [dataset], fn)

    def tools(self) -> list[StructuredTool]:
        def t(fn, schema, doc):
            return StructuredTool.from_function(fn, name=fn.__name__, args_schema=schema, description=doc)

        return [
            t(self.search_metrics, Keyword, "检索可用的已治理指标（含派生指标公式与开放维度）"),
            t(self.describe_metric, Code, "指标详情：表达式、来源资产、时间字段、维度、组成、版本历史、依赖漂移"),
            t(self.search_assets, Keyword, "按名称或字段注释检索可见的数据资产"),
            t(self.describe_asset, Qualified, "资产详情：字段与数据库注释、视图定义、粒度"),
            t(self.lineage, Qualified, "资产的列级上下游血缘（取值、聚合、过滤、关联键）"),
            t(self.changes, Window, "时间窗口内的元数据变更与口径版本流转（排查“是不是口径或数据本身变了”）"),
            t(self.data_health, Health, "指标来源的数据源采集情况与新鲜度"),
            t(self.query_metric, QueryArgs, "受治理查询：按已发布口径取数，产生数据集 Dn（权限与执行前核对照常生效）"),
            *([t(self.query_sql, QuerySqlArgs, "探索查询：口径覆盖不到的数据，按血缘与字段注释写一条 SELECT；只能用当前用户可见的"
                 "已采集表，先用 describe_asset、lineage 读注释与关联键；系统校验后重新生成 SQL 执行，结果带“探索查询”标记"),
               t(self.sample_values, SampleArgs, "列的常见取值（最多 20 个，有范围的预览），用于写过滤条件；计入探索预算")]
              if self.explore_available() else []),
            t(self.explain_change, ExplainArgs, "自动归因：按候选维度扫描两期变化，给出成员贡献（EP）、分布变化强度与排名；"
              "可加指标与 a / b 比率指标适用，不可加指标会说明不支持"),
            t(self.contribution, ContributionArgs, "两期差额按维度拆解（贡献、变化率、占总变化比例）"),
            t(self.factor_decompose, FactorArgs, "乘法关系的因子拆解（LMDI，各因子贡献相加等于总差额）"),
            t(self.calendar_adjust, CalendarArgs, "按天数、周末、节假日折算两期差异（两期日期取自数据集的查询窗口）"),
            t(self.correlate, CorrelateArgs, "两列的相关系数、样本量与显著性"),
            t(self.coverage, CoverageArgs, "数据覆盖度：各成员的行数、覆盖日期数、缺失的日期（行数与缺口要用它取，不要自己数）"),
            t(self.derive, DeriveArgs, "派生数字：用数据集中的值按公式算差、和、比（如排除某个成员后的合计、两期差额），结果可引用"),
            t(self.describe, DescribeArgs, "一列的个数、合计、均值、中位数、最小值、最大值（可按条件选行）"),
            t(self.segment_compare, SegmentArgs, "按某列分组对比各度量"),
            t(self.outliers, OutlierArgs, "四分位距法找异常值"),
            t(self.submit_report, ReportArgs, "提交最终报告：结论分事实 / 计算 / 推断 / 建议，事实与计算必须引用数据集"),
        ]


# 演示数据期间的法定节假日（日历折算用）
HOLIDAYS = {date(2026, 6, 19), date(2026, 6, 20), date(2026, 6, 21), date(2026, 9, 25), date(2026, 9, 26),
            date(2026, 9, 27)}


def _periods(ds: AnalysisDataset, current: str, previous: str) -> tuple[tuple[date, date], tuple[date, date]]:
    """日历折算的两期日期从数据集的查询意图推导，并核对所选列确实是本期与对比期的值（不信任模型传入的日期）。"""
    intent = ds.meta.get("intent") if ds.kind == "governed" else None
    if not intent or not intent.get("compare") or intent.get("time_grain"):
        raise calc.CalcError("日历折算只能用于带对比期、未按时间分组的受治理查询数据集（期间要能追溯到查询）")
    start, end = _date(intent["start_date"]), _date(intent["end_date"])
    compare = intent["compare"]
    before = ((_date(intent["compare_start"]), _date(intent["compare_end"])) if compare == "custom"
              else comparison_range(start, end, compare))
    if current not in ds.columns or previous != f"{current}·{service.PERIOD_LABEL[compare]}":
        raise calc.CalcError(f"列 {current} / {previous} 不是该数据集的本期值与"
                             f"{service.PERIOD_LABEL[compare]}值（应为 指标名 与 指标名·{service.PERIOD_LABEL[compare]}）")
    return (start, end), before


def _iso(value) -> str | None:
    """上下文数据集里的时间统一写成 ISO 字符串（固定排序、可序列化）。"""
    return None if value is None else (value.isoformat() if hasattr(value, "isoformat") else str(value))


def _opt_date(value) -> date | None:
    return date.fromisoformat(str(value)) if value else None


def _date(value):
    """直接调用（测试）时可能传入字符串日期；经模型调用时参数已按模型校验为 date。"""
    return date.fromisoformat(value) if isinstance(value, str) else value


def _model(cls, value):
    """工具参数里的嵌套对象：LangChain 可能传入模型实例或 dict。"""
    return value if isinstance(value, cls) else cls.model_validate(value)


def _parts(value):
    """标题、摘要等字段：文字或片段列表；片段里的引用对象转成 dict 保存。"""
    if value is None or isinstance(value, str):
        return value
    return [p.model_dump() if isinstance(p, BaseModel) else p for p in value]
