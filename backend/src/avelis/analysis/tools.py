"""分析智能体的工具（ADR 0007 第 3 点）。

三类工具，全部以发起人身份执行、按其可见范围过滤：
- 上下文：口径、资产（含字段注释）、血缘、变更记录、数据健康——只读元数据；
- 受治理查询：query_metric 复用问数的完整管线（start_answer / fulfil / finish_answer），每次产生一个数据集；
- 计算：输入只能是数据集编号与列名，数值由后端从数据集中取，模型不能手填数字。
预算（工具次数、查询次数、时长）在这里强制执行，超出时工具直接拒绝并说明。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Literal

from fastapi import HTTPException
from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from avelis.analysis import calc
from avelis.ask import service
from avelis.ask.intent import DimensionFilter, Intent
from avelis.collect.service import freshness_boundary
from avelis.governance import access
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
    MetricVersionTransition,
    User,
)

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

    def exhausted(self) -> str | None:
        if self.used_tools >= self.tool_calls:
            return f"工具调用已达上限 {self.tool_calls} 次"
        if time.monotonic() - self.started > self.seconds:
            return f"分析时长已超过 {self.seconds} 秒"
        return None


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
    compare: Literal["previous_period", "previous_year"] | None = Field(
        None, description="与上一期 / 去年同期对比（不能与 time_grain 同用）")
    order: Literal["desc", "asc"] | None = None
    limit: int | None = Field(None, ge=1, le=100)
    purpose: str = Field(description="这次查询要回答什么（写进步骤记录）")


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
    dataset: str
    current: str
    previous: str
    match: dict[str, str] = Field(default_factory=dict)
    current_period: tuple[date, date]
    previous_period: tuple[date, date]


class CorrelateArgs(BaseModel):
    dataset: str
    x: str
    y: str


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
    text: str = Field(description="结论；数字照抄数据集中的值，可按显示精度四舍五入")
    refs: list[str] = Field(default_factory=list, description="引用的数据集编号，如 ['D2']")
    premise: str | None = Field(None, description="推断与建议的前提")
    validation: str | None = Field(None, description="建议的验证方式")


class ReportArgs(BaseModel):
    title: str
    summary: str = Field(description="一两句话的结论（不写未经数据集支持的数字）")
    claims: list[Claim]
    unexplained: str | None = Field(None, description="仍未解释的部分")


# --- 工具箱 ---------------------------------------------------------------------------

class AnalysisToolbox:
    def __init__(self, session: Session, user: User, run: AnalysisRun, catalog: service.Catalog, budget: Budget):
        self.session, self.user, self.run, self.catalog, self.budget = session, user, run, catalog, budget
        self.plan: dict | None = None
        self.report: dict | None = None
        self._seq = 0
        self._datasets: dict[str, AnalysisDataset] = {}

    # -- 记录 --
    def _step(self, kind: str, tool: str | None, args: dict, status: str, output: dict, reason: str | None = None,
              dataset: AnalysisDataset | None = None, started: float | None = None) -> None:
        self._seq += 1
        self.session.add(AnalysisStep(
            run_id=self.run.id, seq=self._seq, kind=kind, tool=tool, args=json.loads(_json(args)), status=status,
            output=json.loads(_json(output)), reason=reason, dataset_id=dataset.id if dataset else None,
            duration_ms=int((time.monotonic() - started) * 1000) if started else None))
        self.session.flush()

    def _dataset(self, kind: str, title: str, columns: list[str], rows: list[list], *, answer_id: str | None = None,
                 inputs: list[str] | None = None, meta: dict | None = None, truncated: bool = False) -> AnalysisDataset:
        ds = AnalysisDataset(run_id=self.run.id, seq=len(self._datasets) + 1, kind=kind, title=title[:255],
                             answer_id=answer_id, inputs=inputs or [], columns=columns,
                             rows=json.loads(_json(rows)), truncated=truncated, meta=json.loads(_json(meta or {})))
        self.session.add(ds)
        self.session.flush()
        self._datasets[f"D{ds.seq}"] = ds
        return ds

    def datasets(self) -> dict[str, AnalysisDataset]:
        return dict(self._datasets)

    def _rows(self, ref: str) -> tuple[AnalysisDataset, list[dict]]:
        ds = self._datasets.get(ref)
        if ds is None:
            raise calc.CalcError(f"数据集 {ref} 不存在（可用：{'、'.join(self._datasets) or '无'}）")
        return ds, [dict(zip(ds.columns, r)) for r in ds.rows]

    def _view(self, ref: str, ds: AnalysisDataset) -> dict:
        return {"dataset": ref, "title": ds.title, "kind": ds.kind, "columns": ds.columns, "row_count": len(ds.rows),
                "rows": ds.rows[:MODEL_ROWS], "rows_shown": min(len(ds.rows), MODEL_ROWS),
                **({"summary": ds.meta.get("summary")} if ds.meta.get("summary") else {})}

    def _guard(self, tool: str, args: dict, run) -> str:
        """统一的预算检查、异常与步骤记录。"""
        started = time.monotonic()
        over = self.budget.exhausted()
        if over:
            self._step("tool", tool, args, "denied", {"error": over}, started=started)
            return _json({"error": f"{over}：请基于已有数据集提交报告（submit_report），并在 unexplained 中说明未完成的部分"})
        self.budget.used_tools += 1
        try:
            output, dataset, status = run()
        except (calc.CalcError, HTTPException, ValueError) as exc:
            message = exc.detail if isinstance(exc, HTTPException) else str(exc)
            self._step("tool", tool, args, "error", {"error": message}, started=started)
            return _json({"error": message})
        self._step("tool", tool, args, status, output if dataset is None else {"dataset": f"D{dataset.seq}"},
                   reason=args.get("purpose"), dataset=dataset, started=started)
        return _json(output)

    # -- 计划与报告 --
    def submit_plan(self, hypotheses: list[str], steps: list[str]) -> str:
        self.plan = {"hypotheses": hypotheses, "steps": steps}
        self._step("plan", "submit_plan", self.plan, "ok", {})
        return _json({"ok": True, "note": "计划已记录；按计划调用工具，中间结果推翻假设时可再次提交计划"})

    def submit_report(self, title: str, summary: str, claims: list[Claim], unexplained: str | None = None) -> str:
        self.report = {"title": title, "summary": summary, "claims": [_model(Claim, c).model_dump() for c in claims],
                       "unexplained": unexplained}
        self._step("note", "submit_report", {"title": title}, "ok", {"claims": len(claims)})
        return _json({"ok": True})

    # -- 上下文 --
    def search_metrics(self, keyword: str = "") -> str:
        def run():
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
            if code not in self.catalog.versions:
                raise ValueError(f"指标 {code} 不存在或不可见")
            from avelis.api.governance import version_view

            metric, version = self.catalog.versions[code]
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
        allowed = access.domain_ids(self.session, self.user)
        for a in self.session.scalars(select(Asset).where(Asset.catalog_name == parts[0], Asset.db_name == parts[1],
                                                          Asset.name == parts[2], Asset.status == "active")):
            if access.can_read_asset(self.session, self.user, a, allowed):
                return a
        raise ValueError(f"资产 {qualified} 不存在或无权访问")

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
            sets = []
            for cs in self.session.scalars(select(ChangeSet).where(ChangeSet.created_at >= lo,
                                                                   ChangeSet.created_at < hi)
                                           .order_by(ChangeSet.created_at)):
                v = change_set_view(self.session, cs, full=True, user=self.user)
                if v["events"]:
                    sets.append({"at": v["created_at"], "events": [
                        {"kind": e["kind"], "asset": e["asset"], "column": e["column"]} for e in v["events"]][:30],
                        "impacts": len(v["impacts"])})
            transitions = []
            codes = [metric_code] if metric_code else list(self.catalog.versions)
            for code in codes:
                if code not in self.catalog.versions:
                    continue
                metric = self.catalog.versions[code][0]
                for t, mv in self.session.execute(
                        select(MetricVersionTransition, MetricVersion).join(
                            MetricVersion, MetricVersion.id == MetricVersionTransition.metric_version_id)
                        .where(MetricVersion.metric_id == metric.id, MetricVersionTransition.at >= lo,
                               MetricVersionTransition.at < hi)):
                    transitions.append({"metric": metric.name, "version_no": mv.version_no, "from": t.from_status,
                                        "to": t.to_status, "reason": t.reason, "at": t.at})
            note = ("元数据变更按采集时间记录（只能确定发生在两次采集之间）；口径版本流转为治理操作时间。"
                    "本窗口内没有变更不代表数据没有问题，还需查看数据健康与数据本身。")
            return {"change_sets": sets, "metric_transitions": transitions, "note": note}, None, "ok"
        return self._guard("changes", {"start_date": start_date, "end_date": end_date, "metric_code": metric_code},
                           run)

    def data_health(self, metric_code: str, start_date: date, end_date: date) -> str:
        def run():
            if metric_code not in self.catalog.versions:
                raise ValueError(f"指标 {metric_code} 不存在或不可见")
            metric, version = self.catalog.versions[metric_code]
            versions = [version]
            if version.kind == "derived":
                versions = [self.session.get(MetricVersion, c["version_id"]) for c in version.components]
            out = []
            for v in versions:
                base = self.session.get(Asset, v.base_asset_id)
                runs = list(self.session.scalars(select(CollectionRun).where(
                    CollectionRun.datasource_id == base.datasource_id).order_by(CollectionRun.started_at.desc())
                    .limit(5)))
                boundary = freshness_boundary(self.session, base.datasource_id)
                out.append({"metric_version": v.id, "base_asset": f"{base.catalog_name}.{base.db_name}.{base.name}",
                            "last_collected_at": boundary["last_collected_at"],
                            "recent_collections": [{"status": r.status, "started_at": r.started_at,
                                                    "error": r.error} for r in runs],
                            "metric_status": v.status, "drift": bool(v.status != "published")})
            note = ("数据完整性需要看数据本身：用 query_metric 按 time_grain=day 并按门店等维度分组，"
                    "找出某天某些成员缺失或骤降；再用 lineage 查看口径的主表（缺失可能来自上游）。")
            return {"sources": out, "note": note}, None, "ok"
        return self._guard("data_health", {"metric_code": metric_code, "start_date": start_date,
                                           "end_date": end_date}, run)

    # -- 受治理查询 --
    def query_metric(self, **kwargs) -> str:
        args = QueryArgs(**kwargs)

        def run():
            if self.budget.used_queries >= self.budget.queries:
                raise ValueError(f"受治理查询已达上限 {self.budget.queries} 次：请基于已有数据集继续")
            self.budget.used_queries += 1
            intent = Intent(metric_codes=args.metric_codes, start_date=args.start_date, end_date=args.end_date,
                            filters=args.filters, group_by=args.group_by, time_grain=args.time_grain,
                            compare=args.compare, order=args.order, limit=args.limit)
            started = time.monotonic()
            answer = service.start_answer(self.session, self.user, f"[分析 {self.run.id}] {args.purpose}",
                                          self.run.model_adapter or "analysis", None)
            answer.intent = intent.model_dump(mode="json")
            service.fulfil(self.session, self.user, intent, answer, self.catalog)
            service.finish_answer(self.session, self.user, answer, started)
            if answer.status != "answered":
                return ({"status": answer.status, "error_code": answer.error_code, "message": answer.message,
                         "answer_id": answer.id}, None, answer.status)
            result = answer.result
            names = [self.catalog.versions[c][0].name for c in args.metric_codes]
            columns = list(result["columns"])
            if columns and columns[-1] == "value" and len(names) == 1:  # 单指标下推时结果列名为 value：统一成指标名
                columns[-1] = names[0]
            title = f"{'、'.join(names)} {args.start_date}~{args.end_date}" + (
                f" 按{'、'.join(args.group_by)}" if args.group_by else "") + (
                f" 按{'日' if args.time_grain == 'day' else '月'}" if args.time_grain else "") + (
                f" 对比{'上一期' if args.compare == 'previous_period' else '去年同期'}" if args.compare else "")
            ds = self._dataset("governed", title, columns, result["rows"], answer_id=answer.id,
                               truncated=bool(result.get("truncated")),
                               meta={"sql": answer.sql, "metric_version_ids": answer.metric_version_ids,
                                     "intent": answer.intent, "freshness": answer.freshness,
                                     "purpose": args.purpose})
            return self._view(f"D{ds.seq}", ds), ds, "ok"
        return self._guard("query_metric", args.model_dump(mode="json"), run)

    # -- 计算 --
    def _calc(self, tool: str, args: dict, inputs: list[str], fn) -> str:
        def run():
            title, columns, rows, summary = fn()
            ds = self._dataset("calc", title, columns, rows, inputs=inputs, meta={"summary": summary, "args": args})
            return self._view(f"D{ds.seq}", ds), ds, "ok"
        return self._guard(tool, args, run)

    def contribution(self, dataset: str, keys: list[str], current: str, previous: str) -> str:
        def fn():
            _, rows = self._rows(dataset)
            out = calc.contribution(rows, keys, current, previous)
            cols = [*keys, "previous", "current", "change", "change_rate", "share_of_change"]
            return (f"{dataset} 按{'、'.join(keys)}的差额拆解", cols, [[r[c] for c in cols] for r in out["rows"]],
                    {"total": out["total"]})
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
            return (f"{dataset} {total.name}的因子拆解", cols, [[f[c] for c in cols] for f in out["factors"]],
                    {"total": out["total"]})
        return self._calc("factor_decompose", {"dataset": dataset, "total": total.model_dump(),
                                               "factors": [f.model_dump() for f in factors], "match": match},
                          [dataset], fn)

    def calendar_adjust(self, dataset: str, current: str, previous: str, current_period: tuple[date, date],
                        previous_period: tuple[date, date], match: dict[str, str] | None = None) -> str:
        def fn():
            _, rows = self._rows(dataset)
            row = self._pick(rows, match or {})
            out = calc.calendar_adjust(row[current], row[previous], tuple(current_period), tuple(previous_period),
                                       HOLIDAYS)
            cols = ["period", "days", "weekend_days", "holidays", "value", "daily"]
            rows_out = [["本期", *[out["current"][c] for c in cols[1:]]], ["上期", *[out["previous"][c] for c in cols[1:]]]]
            return (f"{dataset} {current}的日历折算", cols, rows_out,
                    {k: out[k] for k in ("change", "day_count_effect", "daily_level_effect", "daily_change_rate")})
        return self._calc("calendar_adjust", {"dataset": dataset, "current": current, "previous": previous,
                                              "current_period": current_period, "previous_period": previous_period,
                                              "match": match}, [dataset], fn)

    def correlate(self, dataset: str, x: str, y: str) -> str:
        def fn():
            _, rows = self._rows(dataset)
            out = calc.correlate(rows, x, y)
            cols = list(out)
            return f"{dataset} {x} 与 {y} 的相关性", cols, [[out[c] for c in cols]], out
        return self._calc("correlate", {"dataset": dataset, "x": x, "y": y}, [dataset], fn)

    def segment_compare(self, dataset: str, segment: str, measures: list[str]) -> str:
        def fn():
            _, rows = self._rows(dataset)
            out = calc.segment_compare(rows, segment, measures)
            cols = list(out[0]) if out else [segment]
            return f"{dataset} 按{segment}分组对比", cols, [[r[c] for c in cols] for r in out], None
        return self._calc("segment_compare", {"dataset": dataset, "segment": segment, "measures": measures},
                          [dataset], fn)

    def outliers(self, dataset: str, value: str, keys: list[str]) -> str:
        def fn():
            _, rows = self._rows(dataset)
            out = calc.outliers(rows, value, keys)
            cols = [*keys, value, "side"]
            return (f"{dataset} {value}的异常值", cols, [[r.get(c) for c in cols] for r in out["rows"]],
                    {k: v for k, v in out.items() if k != "rows"})
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
            t(self.contribution, ContributionArgs, "两期差额按维度拆解（贡献、变化率、占总变化比例）"),
            t(self.factor_decompose, FactorArgs, "乘法关系的因子拆解（LMDI，各因子贡献相加等于总差额）"),
            t(self.calendar_adjust, CalendarArgs, "按天数、周末、节假日折算两期差异"),
            t(self.correlate, CorrelateArgs, "两列的相关系数、样本量与显著性"),
            t(self.segment_compare, SegmentArgs, "按某列分组对比各度量"),
            t(self.outliers, OutlierArgs, "四分位距法找异常值"),
            t(self.submit_report, ReportArgs, "提交最终报告：结论分事实 / 计算 / 推断 / 建议，事实与计算必须引用数据集"),
        ]


# 演示数据期间的法定节假日（日历折算用）
HOLIDAYS = {date(2026, 6, 19), date(2026, 6, 20), date(2026, 6, 21), date(2026, 9, 25), date(2026, 9, 26),
            date(2026, 9, 27)}


def _model(cls, value):
    """工具参数里的嵌套对象：LangChain 可能传入模型实例或 dict。"""
    return value if isinstance(value, cls) else cls.model_validate(value)
