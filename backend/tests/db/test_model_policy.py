"""模型数据外发策略的部分更新（审计 #3）。"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.db


def test_partial_policy_update_keeps_saved_false(factory):
    from avelis import modelcfg
    from avelis.db import session_scope
    from avelis.models import AppSetting, User

    admin = factory.user(roles=("admin",))
    with session_scope() as s:
        s.query(AppSetting).filter(AppSetting.key == modelcfg.POLICY_KEY).delete()
    with session_scope() as s:
        assert modelcfg.policy(s) == modelcfg.DEFAULT_POLICY  # 新安装：默认允许，不变
        modelcfg.save_settings(s, s.get(User, admin), {}, {"send_results": False, "send_code": False})
    with session_scope() as s:  # 只提交其中一项：另一项保持已保存的 false，不被默认值重新打开
        modelcfg.save_settings(s, s.get(User, admin), {}, {"send_code": False})
    with session_scope() as s:
        assert modelcfg.policy(s) == {"send_results": False, "send_code": False}
        modelcfg.save_settings(s, s.get(User, admin), {}, {"send_code": True})
    with session_scope() as s:
        assert modelcfg.policy(s) == {"send_results": False, "send_code": True}


def test_notifications_list_unread_first(factory):
    """未读优先：最新 30 条都已读时，更早的未读仍排在前面，能看到也能标为已读（复核 P3）。"""
    from datetime import timedelta

    from avelis.api import analysis as api
    from avelis.db import session_factory, session_scope, utcnow
    from avelis.models import Notification, User

    uid = factory.user()
    now = utcnow()
    with session_scope() as s:
        s.add(Notification(user_id=uid, kind="analysis_failed", title="很早的未读", created_at=now - timedelta(days=9)))
        for i in range(35):
            s.add(Notification(user_id=uid, kind="analysis_finished", title=f"已读 {i}", read_at=now,
                               created_at=now - timedelta(minutes=i)))
    session = session_factory()()
    try:
        items = api.list_notifications(unread=False, user=session.get(User, uid), session=session)
    finally:
        session.close()
    assert items[0]["title"] == "很早的未读" and items[0]["read_at"] is None
    assert [n["title"] for n in items[1:3]] == ["已读 0", "已读 1"]
