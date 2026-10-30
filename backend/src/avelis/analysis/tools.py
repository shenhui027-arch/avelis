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
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Annotated, Any, Literal

from fastapi import HTTPException
from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from avelis.analysis import attribution, calc, measures
from avelis.analysis.grounding import Part
from avelis.analysis.measures import col, like
from avelis.ask import service
from avelis.ask.intent import DimensionFilter, Intent, comparison_range
from avelis.collect.service import freshness_boundary
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
    tool_calls: int = 30
    queries: int = 12
    seconds: int = 300
    used_tools: int = 0
    used_queries: int = 0
    started: float = field(default_factory=time.monotonic)

    def view(self) -> dict[str, Any]:
        return {"limits": {"tool_calls": self.tool_calls, "queries": self.queries, "seconds": self.seconds},
                "used": {"tool_calls": self.used_tools, "queries": self.used_queries,
                         "seconds": round(time.monotonic() - self.started, 1)}}

    def remaining(self) -> float:
        """距统一截止时间还剩的秒数（计划、执行、修订、宽限共用同一个截止时间）。"""
        return self.started + self.seconds - time.monotonic()

    def exhausted(self) -> str | None:
        if self.used_tools >= self.tool_calls:
            return f"工具调用已达上限 {self.tool_calls} 次"
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


def _json(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, default=lambda v: str(v) if isinstance(v, (Decimal, date, datetime))
                      else repr(v))


# --- 参数 ---------------------------------------------------------------------------

class Keyword(BaseModel):
    keyword: str = Field("", description="关键词（名称、编码、注释中的文字）；空表示全部")


class Code(BaseModel):
    code: str = Field(description="指标编码")


class Qualified(BaseModel):
    qualified: str = Field(description="资产全名 catalog.db.name，如 default_catalog.ec_dw.dws_store_daily")


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


class ExplainArgs(BaseModel):
    metric_code: str = Field(description="指标编码")
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
    current: str = Field(description="本期值列名，如 成交额")
    previous: str = Field(description="上期值列名，如 成交额·上一期")


class Pair(BaseModel):
    name: str = Field(description="因子名，如 订单数")
    current: str = Field(description="本期值列名")
    previous: str = Field(description="上期值列名")


class FactorArgs(BaseModel):
    dataset: str
    total: Pair = Field(description="总量的本期与上期列，如 成交额 / 成交额·上一期")
    factors: list[Pair] = Field(min_length=2, description="乘积等于总量的因子，如 订单数、客单价")
    match: dict[str, str] = Field(default_factory=dict, description="选行条件（维度列 → 值）；数据集只有一行时可省略")


class CalendarArgs(BaseModel):
    dataset: str = Field(description="带对比期的受治理查询数据集；两期日期取自该次查询，不能另行指定")
    current: str = Field(description="本期值列名，如 成交额")
    previous: str = Field(description="对比期值列名，如 成交额·对比期")
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
    exclude: dict[str, str] = Field(default_factory=dict, description="排除的行（如排除某门店后求其余合计）")


class DeriveArgs(BaseModel):
    label: str = Field(description="结果的名称，如 其余门店成交额合计")
    values: list[ValueRef] = Field(min_length=1, max_length=8)
    formula: str = Field(description="只用变量名、数字与 + - * / 括号，如 total - futian、a / b")


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


class PlanArgs(BaseModel):
    hypotheses: list[str] = Field(description="待验证的假设（按先排查口径与数据、再拆维度、因子、日历的顺序）")
    steps: list[str] = Field(description="准备做的查询与计算")


class Claim(BaseModel):
    type: Literal["fact", "calc", "inference", "recommendation"]
    parts: list[Part] = Field(description="结论正文的片段列表：文字片段写字符串，数字写成引用对象，如 "
                                          "['华东成交额下降 ', {'dataset': 'D19', 'column': '差额', 'abs': True, "
                                          "'format': 'wan1'}, '元']；文字片段里不写任何数字（ADR 0009）")
    refs: list[Annotated[str, Field(pattern=r"^D[1-9]\d{0,3}$")]] = Field(
        default_factory=list, description="额外引用的数据集编号，如 ['D2']；引用片段涉及的数据集自动计入")
    premise: str | list[Part] | None = Field(None, description="推断与建议的前提（文字或片段列表）")
    validation: str | list[Part] | None = Field(None, description="建议的验证方式（文字或片段列表）")


class ReportArgs(BaseModel):
    title: str | list[Part] = Field(description="标题：文字或片段列表；数字同样写成引用对象")
    summary: str | list[Part] = Field(description="一两句话的结论：片段列表，数字写成引用对象，后端渲染并做同样的结构检查")
    claims: list[Claim]
    unexplained: str | list[Part] | None = Field(None, description="仍未解释的部分：文字或片段列表")


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
        self.plan: dict | None = None
        self.report: dict | None = None
        self.permission_lost: str | None = None  # 发起人失去权限的原因：之后一律拒绝，分析停止
        self.denials: list[dict] = []  # 明确的权限拒绝（口径或来源无权访问）
        self._seq = 0
        self._datasets: dict[str, AnalysisDataset] = {}
        self._tools: dict[str, StructuredTool] | None = None

    # -- 记录 --
    def _step(self, kind: str, tool: str | None, args: dict, status: str, output: dict, reason: str | None = None,
              dataset: AnalysisDataset | None = None, started: float | None = None) -> None:
        if self.aborted:  # 已失去执行权：不再写入
            return
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
        """模型的每一次工具调用都从这里进入：参数校验之前就计入工具次数（未知工具、参数无效、重复提交计划都算），
        只有最终的 submit_report 不计数，预算用完后也只能调用它。only_report：时间将尽，只接受 submit_report。"""
        if self._tools is None:
            self._tools = {t.name: t for t in self.tools()}
        self.check_executor()
        if self.stopped:
            return _json({"error": f"{self.stopped}：不再执行工具"})
        if name != "submit_report":
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
        if not isinstance(args, dict):
            self._step("tool", name, {"raw": str(args)[:500]}, "error", {"error": "参数无法解析"})
            return _json({"error": f"工具 {name} 的参数无法解析，请修正后重试"})
        try:
            return str(tool.invoke(args))
        except Exception as exc:  # noqa: BLE001 - 参数校验失败等如实返回给模型
            message = f"{type(exc).__name__}: {str(exc)[:500]}"
            self._step("tool", name, json.loads(_json(args)), "error", {"error": message})
            return _json({"error": message})

    # -- 计划与报告 --
    def submit_plan(self, hypotheses: list[str], steps: list[str]) -> str:
        self.plan = {"hypotheses": hypotheses, "steps": steps}
        self._step("plan", "submit_plan", self.plan, "ok", {})
        return _json({"ok": True, "note": "计划已记录；按计划调用工具，中间结果推翻假设时可再次提交计划"})

    def submit_report(self, title: str | list, summary: str | list, claims: list[Claim],
                      unexplained: str | list | None = None) -> str:
        """保存模型提交的片段列表；渲染与结构检查在 verify 节点（ADR 0009）。"""
        self.report = {"title": _parts(title), "summary": _parts(summary),
                       "claims": [_model(Claim, c).model_dump() for c in claims], "unexplained": _parts(unexplained)}
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
                out.append({"qualified": name, "kind": a.kind, "comment": comment, "matched_columns": hit[:10]})
                if len(out) >= 40:
                    break
            return {"assets": out, "note": "字段的数据库注释与状态见 describe_asset"}, None, "ok"
        return self._guard("search_assets", {"keyword": keyword}, run)

    def describe_asset(self, qualified: str) -> str:
        def run():
            from avelis.api.catalog import asset_detail

            a = self._asset(qualified)
            d = asset_detail(a.id, self.user, self.session)
            return {"qualified": qualified, "kind": d["kind"], "comment": (d.get("version") or {}).get("comment"),
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
            note = ("数据完整性需要看数据本身：用 query_metric 按 time_grain=day 并按门店等维度分组，"
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
        """执行一次受治理查询并保存为数据集（query_metric 与 explain_change 的子查询共用）。"""
        if self.budget.used_queries >= self.budget.queries:
            raise ValueError(f"受治理查询已达上限 {self.budget.queries} 次：请基于已有数据集继续")
        # 按当前授权核对每个请求的口径，只把它们交给查询管线（管线再核对来源闭包、状态与执行前核对）
        scoped = service.Catalog([], {code: self._metric(code) for code in args.metric_codes})
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
                                 "intent": answer.intent, "freshness": answer.freshness,
                                 "purpose": args.purpose,
                                 "time_column": columns[0] if args.time_grain else None, "units": units,
                                 "window": {"start": str(args.start_date), "end": str(args.end_date)}})
        return self._view(f"D{ds.seq}", ds), ds, "ok"

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
        if self.budget.used_queries >= self.budget.queries:
            return f"受治理查询额度（{self.budget.queries} 次）已用完"
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

    def _explain(self, args: ExplainArgs) -> tuple[dict, AnalysisDataset | None, str]:
        window = self.run.window or {}
        start, end = args.start_date or _opt_date(window.get("start")), args.end_date or _opt_date(window.get("end"))
        cstart = args.compare_start or _opt_date(window.get("compare_start"))
        cend = args.compare_end or _opt_date(window.get("compare_end"))
        if not all((start, end, cstart, cend)):
            raise ValueError("自动归因需要本期与对比期：本次分析没有窗口时请传 start_date、end_date、compare_start、compare_end")
        metric, version, _, _ = self._metric_sources(args.metric_code)
        components = {}
        if version.kind == "derived":
            for c in version.components or []:
                components[c["metric_code"]] = self.session.get(MetricVersion, c["version_id"])
        rule = attribution.applicability(version, components)
        if rule["algorithm"] is None:
            return {"supported": False, "metric_code": args.metric_code, "reason": rule["reason"],
                    "note": "不支持自动归因：可以用 query_metric 按维度查询后用 contribution 拆解，或说明无法归因"}, None, "ok"
        allowed = [d["name"] for d in version.dimensions or []]
        dims = list(dict.fromkeys(args.dimensions)) or allowed[:8]
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
                           group_by=group_by, compare="custom" if compare else None,
                           compare_start=cstart if compare else None, compare_end=cend if compare else None,
                           purpose=f"{args.purpose}：{purpose}")
            out, ds, status = self._run_query(qa)
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
            return attribution.judge(dim, total_prev, total_change, args.top_k)

        scanned = [d for d in (scan([name]) for name in dims) if d is not None]
        if args.depth == 2:
            top = [d.name for d in attribution.rank(scanned)[:3]]
            for i, first in enumerate(top):
                for second in top[i + 1:]:
                    pair = scan(sorted([first, second]))
                    if pair is not None:
                        scanned.append(pair)
        ranked = attribution.rank(scanned)
        conclusion = attribution.verdict(ranked, bool(scanned))
        primary = next((d for d in ranked if d.verdict == "ranked"), None)
        cols = ["dimension", "member", "previous", "current", "member_change", "contribution", "ep",
                "structure_effect", "rate_effect", "change_type", "role"]
        rows = [[d.name, *[r[c] for c in cols[1:-1]], r["role"]] for d in ranked for r in d.selected]
        summary = {
            "metric_code": args.metric_code, "algorithm": rule["algorithm"],
            "total": {"previous": attribution.q(total_prev) if ratio else total_prev,
                      "current": attribution.q(total_cur) if ratio else total_cur,
                      "change": attribution.q(total_change) if ratio else total_change},
            "verdict": conclusion, "primary_dimension": primary.name if primary else None,
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
                         "cumulative_ep": col("share"), **({"residual": delta} if ratio else {})}
        title = f"自动归因：{metric.name} {start}~{end} 对比 {cstart}~{cend}"
        ds = self._dataset("calc", title, cols, rows, inputs=inputs,
                           meta={"summary": summary, "args": args.model_dump(mode="json"), "units": units,
                                 "summary_units": summary_units, "partial": bool(unscanned)})
        return self._view(f"D{ds.seq}", ds), ds, "ok"

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
            registered = extra[0] if extra else {}  # {units, summary_units, assumption}：量纲登记（ADR 0009）
            ds = self._dataset("calc", title, columns, rows, inputs=inputs, truncated=bool(truncated),
                               meta={"summary": summary, "args": args, **registered})
            return self._view(f"D{ds.seq}", ds), ds, "ok"
        return self._guard(tool, args, run)

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
            cols = ["factor", "previous", "current", "change_rate", "contribution", "share"]
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
            t(self.submit_plan, PlanArgs, "提交分析计划（第一步必须调用）：假设清单与准备做的查询、计算"),
            t(self.search_metrics, Keyword, "检索可用的已治理指标（含派生指标公式与开放维度）"),
            t(self.describe_metric, Code, "指标详情：表达式、来源资产、时间字段、维度、组成、版本历史、依赖漂移"),
            t(self.search_assets, Keyword, "按名称或字段注释检索可见的数据资产"),
            t(self.describe_asset, Qualified, "资产详情：字段与数据库注释、视图定义、粒度"),
            t(self.lineage, Qualified, "资产的列级上下游血缘（取值、聚合、过滤、关联键）"),
            t(self.changes, Window, "时间窗口内的元数据变更与口径版本流转（排查“是不是口径或数据本身变了”）"),
            t(self.data_health, Health, "指标来源的数据源采集情况与新鲜度"),
            t(self.query_metric, QueryArgs, "受治理查询：按已发布口径取数，产生数据集 Dn（权限与执行前核对照常生效）"),
            t(self.explain_change, ExplainArgs, "自动归因：按候选维度扫描两期变化，给出成员贡献（EP）、分布变化强度与排名；"
              "可加指标与 a / b 比率指标适用，不可加指标会说明不支持"),
            t(self.contribution, ContributionArgs, "两期差额按维度拆解（贡献、变化率、占总变化比例）"),
            t(self.factor_decompose, FactorArgs, "乘法关系的因子拆解（LMDI，各因子贡献相加等于总差额）"),
            t(self.calendar_adjust, CalendarArgs, "按天数、周末、节假日折算两期差异（两期日期取自数据集的查询窗口）"),
            t(self.correlate, CorrelateArgs, "两列的相关系数、样本量与显著性"),
            t(self.coverage, CoverageArgs, "数据覆盖度：各成员的行数、覆盖日期数、缺失的日期（行数与缺口要用它取，不要自己数）"),
            t(self.derive, DeriveArgs, "派生数字：用数据集中的值按公式算差、和、比（如排除某门店后的合计、两期差额），结果可引用"),
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
