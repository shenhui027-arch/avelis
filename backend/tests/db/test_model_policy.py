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
