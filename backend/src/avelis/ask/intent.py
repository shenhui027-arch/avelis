"""问数意图与模型适配器。

模型（或演示适配器）只输出结构化意图，不写 SQL；意图在后端逐项校验：
指标必须是用户可见的、维度必须在指标允许的维度里、过滤值只作为字面量绑定。
发给模型的只有元数据（指标名称、别名、维度名称与可选值），不含查询结果与凭据。
数据库注释、问题文本都按数据处理，不改变系统规则。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Literal, Protocol
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field, model_validator

MAX_METRICS = 4
MAX_LIMIT = 100


class DimensionFilter(BaseModel):
    dimension: str
    values: list[str] = Field(min_length=1, max_length=50)


class Intent(BaseModel):
    """结构化查询意图。v2 在单指标之外支持：多个指标（metric_codes）、按第一个指标排序与取前 N（order / limit）、
    与上一期或去年同期对比（compare）。每一项都在后端校验，模型不写 SQL。"""

    metric_code: str | None = None
    metric_codes: list[str] = Field(default_factory=list, max_length=MAX_METRICS)
    start_date: date | None = None
    end_date: date | None = None
    filters: list[DimensionFilter] = Field(default_factory=list)
    group_by: list[str] = Field(default_factory=list)
    order: Literal["desc", "asc"] | None = None
    limit: int | None = Field(default=None, ge=1, le=MAX_LIMIT)
    compare: Literal["previous_period", "previous_year"] | None = None
    clarification: str | None = None
    candidates: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _codes(self) -> "Intent":
        if self.metric_codes and not self.metric_code:
            self.metric_code = self.metric_codes[0]
        if self.metric_code and not self.metric_codes:
            self.metric_codes = [self.metric_code]
        return self

    @property
    def is_simple(self) -> bool:
        """单指标、无对比：可以整体下推为一条 SQL（排序与 LIMIT 也在 SQL 中完成）。"""
        return len(self.metric_codes) <= 1 and self.compare is None


def comparison_range(start: date, end: date, compare: str) -> tuple[date, date]:
    """对比期：上一期为紧邻的同长度区间（单日即前一天）；同比为去年同日期（2 月 29 日按 28 日）。"""
    if compare == "previous_period":
        days = (end - start).days + 1
        return start - timedelta(days=days), start - timedelta(days=1)

    def last_year(d: date) -> date:
        try:
            return d.replace(year=d.year - 1)
        except ValueError:
            return d.replace(year=d.year - 1, day=28)

    return last_year(start), last_year(end)


COMPARE_WORDS = {"previous_year": ("同比", "去年同期"), "previous_period": ("环比", "上一期", "前一天", "上期")}
TOP_N = re.compile(r"(?:前|[Tt][Oo][Pp]\s*|最高的?|最多的?)\s*(\d{1,3})")
BOTTOM_N = re.compile(r"(?:后|最低的?|最少的?)\s*(\d{1,3})")


@dataclass
class MetricCard:
    """交给模型的指标元数据。"""

    code: str
    name: str
    aliases: list[str]
    dimensions: list[dict[str, Any]]
    dimension_values: dict[str, list[str]] = field(default_factory=dict)
    description: str = ""
    status: str = "published"


class IntentModel(Protocol):
    name: str
    is_demo: bool

    def parse(self, question: str, metrics: list[MetricCard], *, today: date) -> Intent: ...


DATE_PATTERNS = [
    re.compile(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})"),
    re.compile(r"(\d{4})年(\d{1,2})月(\d{1,2})[日号]"),
]


class DemoIntentModel:
    """演示适配器：确定性规则，不调用任何外部模型。界面与答案中标记为“演示模型”。"""

    name = "demo-rules"
    is_demo = True

    def parse(self, question: str, metrics: list[MetricCard], *, today: date) -> Intent:
        text = question.strip()
        spans: list[tuple[int, int, MetricCard]] = []  # (起点, 终点, 指标)
        for card in metrics:
            for label in [card.name, *card.aliases]:
                start = text.find(label) if label else -1
                while start >= 0:
                    spans.append((start, start + len(label), card))
                    start = text.find(label, start + 1)
        if not spans:
            return Intent(clarification="没有找到与问题匹配的已发布指标，请说明要查询的指标",
                          candidates=[m.name for m in metrics])
        # 被更长的匹配完全包含的短匹配不算（如“退款后净销售额”包含“销售额”）
        spans = [s for s in spans if not any(o[0] <= s[0] and s[1] <= o[1] and (o[1] - o[0]) > (s[1] - s[0])
                                             for o in spans)]
        by_position: dict[tuple[int, int], dict[str, MetricCard]] = {}
        for begin, end_, card in spans:
            by_position.setdefault((begin, end_), {})[card.code] = card
        ambiguous = [cards for cards in by_position.values() if len(cards) > 1]
        if ambiguous:  # 同一段文字对应多个指标（同名或同别名）：追问
            return Intent(clarification="问题可能指多个指标，请明确是哪一个",
                          candidates=[c.name for c in ambiguous[0].values()])
        ordered: list[MetricCard] = []
        for _, cards in sorted(by_position.items()):
            card = next(iter(cards.values()))
            if card.code not in {c.code for c in ordered}:
                ordered.append(card)
        if len(ordered) > MAX_METRICS:
            return Intent(clarification=f"一次最多比较 {MAX_METRICS} 个指标，请缩小范围",
                          candidates=[c.name for c in ordered])
        card = ordered[0]

        days = self._dates(text, today)
        if not days:
            return Intent(metric_code=card.code, clarification="请指定日期或日期范围（例如 2026-09-15）")
        start, end = min(days), max(days)

        filters, group_by = [], []
        # 多个指标时，过滤与分组只能用它们共有的维度
        shared = [d for d in card.dimensions if all(any(x["name"] == d["name"] for x in c.dimensions) for c in ordered)]
        for dim in shared:
            hit = [v for v in card.dimension_values.get(dim["name"], []) if v and v in text]
            if hit:
                filters.append(DimensionFilter(dimension=dim["name"], values=sorted(set(hit))))
            label = dim.get("label") or dim["name"]
            if f"按{label}" in text or f"各{label}" in text or f"分{label}" in text:
                group_by.append(dim["name"])
        compare = next((kind for kind, words in COMPARE_WORDS.items() if any(w in text for w in words)), None)
        order, limit = None, None
        if m := TOP_N.search(text):
            order, limit = "desc", int(m.group(1))
        elif m := BOTTOM_N.search(text):
            order, limit = "asc", int(m.group(1))
        elif any(w in text for w in ("排名", "最高", "最多", "排行")):
            order = "desc"
        if order and not group_by:
            return Intent(metric_codes=[c.code for c in ordered],
                          clarification="排名需要一个维度，请说明按什么排（例如“按区域”）",
                          candidates=[d.get("label") or d["name"] for d in shared])
        return Intent(metric_codes=[c.code for c in ordered], start_date=start, end_date=end, filters=filters,
                      group_by=group_by, order=order, limit=min(limit, MAX_LIMIT) if limit else None,
                      compare=compare)

    @staticmethod
    def _dates(text: str, today: date) -> list[date]:
        found = []
        for pattern in DATE_PATTERNS:
            for y, m, d in pattern.findall(text):
                try:
                    found.append(date(int(y), int(m), int(d)))
                except ValueError:
                    continue
        if "昨天" in text:
            found.append(today - timedelta(days=1))
        if "今天" in text:
            found.append(today)
        return found


def business_today(timezone: str) -> date:
    return datetime.now(ZoneInfo(timezone)).date()
