"""系统设置：业务日历可供登录用户查看，仅管理员可维护。"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from avelis.api import schemas as S
from avelis.api.deps import current_user, get_session
from avelis.governance import access, business_calendar
from avelis.models import User

router = APIRouter(prefix="/api/settings", tags=["系统设置"])


@router.get("/business-calendar", response_model=S.BusinessCalendarOut)
def get_business_calendar(user: User = Depends(current_user), session: Session = Depends(get_session)) -> dict:
    return business_calendar.business_calendar_view(session)


@router.put("/business-calendar", response_model=S.BusinessCalendarOut)
def put_business_calendar(
    body: S.BusinessCalendarIn, user: User = Depends(current_user), session: Session = Depends(get_session)
) -> dict:
    access.require_role(session, user, "admin")
    return business_calendar.save_business_calendar(session, user, body.model_dump())
