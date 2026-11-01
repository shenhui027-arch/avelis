"""示例问题（ADR 0010 第 3 点）：由数据决定，不写死在前端。

业务域可以配置示例问题（example_questions：[{text, mode}]，mode 为 ask、analysis 或 both）。接口返回当前用户可见业务域
的示例问题；都没有配置时，用模板从当前用户可见的已发布口径生成（按编码排序，取有维度的指标）。
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from avelis.ask.intent import business_today
from avelis.config import get_settings
from avelis.governance import access
from avelis.governance.errors import GovernanceError
from avelis.i18n import gettext
from avelis.models import Domain, User

MODES = ("ask", "analysis")
LIMIT = 6


def configured(session: Session, user: User, mode: str) -> list[dict[str, Any]]:
    allowed = access.domain_ids(session, user)
    out = []
    for domain in session.scalars(select(Domain).where(Domain.id.in_(allowed)).order_by(Domain.code)):
        for item in domain.example_questions or []:
            item = item if isinstance(item, dict) else {"text": str(item), "mode": "both"}
            if item.get("text") and item.get("mode", "both") in (mode, "both"):
                out.append({"text": item["text"], "domain": domain.name, "source": "configured"})
    return out[:LIMIT]


def generated(session: Session, user: User, mode: str) -> list[dict[str, Any]]:
    """没有配置时按模板从可见口径生成。"""
    from avelis.ask.service import visible_catalog

    try:
        catalog = visible_catalog(session, user, with_values=False)
    except GovernanceError:  # 没有查询角色：不给示例
        return []
    month_start = business_today(get_settings().business_timezone).replace(day=1)
    end = month_start - timedelta(days=1)
    start = end.replace(day=1)
    period = gettext("{start} 至 {end}", start=start.isoformat(), end=end.isoformat())
    picks = []
    for code in sorted(catalog.versions):
        metric, version = catalog.versions[code]
        dims = {d["name"]: d.get("label") or d["name"] for d in (version.dimensions or []) if d.get("name")}
        if dims:
            picks.append((metric.name, dims))
    if mode == "ask":
        texts = [t for name, dims in picks[:2] for t in (
            gettext("{name} {period} 按{dimension}的排名", name=name, period=period,
                    dimension=next(iter(dims.values()))),
            gettext("{name} {period} 环比", name=name, period=period))]
    else:
        texts = [gettext("{name} {period} 相比上一期为什么变化？主要来自哪些{dimension}？",
                         name=name, period=period, dimension=next(iter(dims.values())))
                 for name, dims in picks[:2]]
        if len(picks) >= 2:
            shared = next((label for name, label in picks[0][1].items() if name in picks[1][1]), None)
            if shared:
                texts.append(gettext("{first} 和 {second} {period} 相比上一期按{dimension}有什么关系？",
                                     first=picks[0][0], second=picks[1][0], period=period, dimension=shared))
    return [{"text": t, "domain": None, "source": "generated"} for t in texts[:LIMIT]]


def example_questions(session: Session, user: User, mode: str) -> list[dict[str, Any]]:
    return configured(session, user, mode) or generated(session, user, mode)
