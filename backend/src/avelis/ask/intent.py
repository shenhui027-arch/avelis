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
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field


class DimensionFilter(BaseModel):
    dimension: str
    values: list[str] = Field(min_length=1, max_length=50)


class Intent(BaseModel):
    metric_code: str | None = None
    start_date: date | None = None
    end_date: date | None = None
    filters: list[DimensionFilter] = Field(default_factory=list)
    group_by: list[str] = Field(default_factory=list)
    clarification: str | None = None
    candidates: list[str] = Field(default_factory=list)


@dataclass
class MetricCard:
    """交给模型的指标元数据。"""

    code: str
    name: str
    aliases: list[str]
    dimensions: list[dict[str, Any]]
    dimension_values: dict[str, list[str]] = field(default_factory=dict)


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
        matches: list[tuple[int, MetricCard, str]] = []
        for card in metrics:
            for label in [card.name, *card.aliases]:
                if label and label in text:
                    matches.append((len(label), card, label))
        if not matches:
            return Intent(clarification="没有找到与问题匹配的已发布指标，请说明要查询的指标",
                          candidates=[m.name for m in metrics])
        longest = max(m[0] for m in matches)
        best = {m[1].code: m[1] for m in matches if m[0] == longest}
        # 较长的匹配包含较短的匹配时，以较长者为准（如“退款后净销售额”包含“销售额”）
        if len(best) > 1:
            return Intent(clarification="问题可能指多个指标，请明确是哪一个",
                          candidates=[c.name for c in best.values()])
        card = next(iter(best.values()))

        days = self._dates(text, today)
        if not days:
            return Intent(metric_code=card.code, clarification="请指定日期或日期范围（例如 2026-09-15）")
        start, end = min(days), max(days)

        filters, group_by = [], []
        for dim in card.dimensions:
            hit = [v for v in card.dimension_values.get(dim["name"], []) if v and v in text]
            if hit:
                filters.append(DimensionFilter(dimension=dim["name"], values=sorted(set(hit))))
            label = dim.get("label") or dim["name"]
            if f"按{label}" in text or f"各{label}" in text or f"分{label}" in text:
                group_by.append(dim["name"])
        return Intent(metric_code=card.code, start_date=start, end_date=end, filters=filters, group_by=group_by)

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


def get_model() -> IntentModel:
    """v0.1 只接演示适配器；Anthropic / OpenAI 兼容适配器在 M2 接入，未拿到 key 前标“未验证”。"""
    return DemoIntentModel()
