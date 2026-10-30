"""示例问题（ADR 0010 第 3 点）：由数据决定，不写死在前端。

业务域可以配置示例问题（example_questions：[{text, mode}]，mode 为 ask、analysis 或 both）。接口返回当前用户可见业务域
的示例问题；都没有配置时，用模板从当前用户可见的已发布口径生成（按编码排序，取有维度的指标）。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from avelis.governance import access
from avelis.governance.errors import GovernanceError
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
    picks = []
    for code in sorted(catalog.versions):
        metric, version = catalog.versions[code]
        dims = [d.get("label") or d["name"] for d in (version.dimensions or []) if d.get("name")]
        if dims:
            picks.append((metric.name, dims[0]))
    if mode == "ask":
        texts = [t for name, dim in picks[:2] for t in (f"{name} 按{dim}的最近一期排名", f"{name} 最近一期 环比")]
    else:
        texts = [f"{name} 最近一期为什么比上一期变化？主要来自哪些{dim}？" for name, dim in picks[:2]]
        if len(picks) >= 2:
            texts.append(f"{picks[0][0]} 和 {picks[1][0]} 按{picks[0][1]}有什么关系？")
    return [{"text": t, "domain": None, "source": "generated"} for t in texts[:LIMIT]]


def example_questions(session: Session, user: User, mode: str) -> list[dict[str, Any]]:
    return configured(session, user, mode) or generated(session, user, mode)
