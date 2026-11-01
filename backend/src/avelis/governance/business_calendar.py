"""管理员维护的业务日历；缺少设置时保留“节假日未知”的语义。"""

from __future__ import annotations

from sqlalchemy.orm import Session

from avelis import audit
from avelis.db import utcnow
from avelis.models import AppSetting, User

BUSINESS_CALENDAR_KEY = "business_calendar"
DEFAULT_WEEKEND_DAYS = (5, 6)


def get_business_calendar(session: Session) -> dict | None:
    """未配置返回 None，不把默认周末误认成已维护的业务日历。"""
    row = session.get(AppSetting, BUSINESS_CALENDAR_KEY)
    if row is None:
        return None
    return {key: list(row.value[key]) for key in ("weekend_days", "holidays", "workdays")}


def business_calendar_view(session: Session) -> dict:
    calendar = get_business_calendar(session)
    if calendar is None:
        return {"configured": False, "weekend_days": list(DEFAULT_WEEKEND_DAYS), "holidays": [], "workdays": []}
    return {"configured": True, **calendar}


def save_business_calendar(session: Session, actor: User, value: dict) -> dict:
    """保存接口已校验并规范化的日历，设置与审计由请求事务一起提交。"""
    row = session.get(AppSetting, BUSINESS_CALENDAR_KEY)
    before = get_business_calendar(session)
    calendar = {key: list(value[key]) for key in ("weekend_days", "holidays", "workdays")}
    if row is None:
        session.add(AppSetting(key=BUSINESS_CALENDAR_KEY, value=calendar, updated_by=actor.id))
    else:
        row.value, row.updated_by, row.updated_at = calendar, actor.id, utcnow()
    audit.record(session, actor, "business_calendar.update", "app_setting", BUSINESS_CALENDAR_KEY,
                 before=before, after=calendar)
    return {"configured": True, **calendar}
