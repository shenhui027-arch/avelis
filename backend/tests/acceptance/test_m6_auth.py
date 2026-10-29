"""认证产品化验收：首次登录改口令、改口令后旧会话失效、登录失败锁定、管理员重置口令与停用、自我保护。

全部经 HTTP API；断言实际行为（令牌是否还能用、数据库中的状态、审计），不只看状态码。
"""

from __future__ import annotations

import pytest

from tests.conftest import Api

pytestmark = pytest.mark.acceptance
ctx: dict = {}


def login(client, username: str, password: str):
    return client.post("/api/auth/token", data={"username": username, "password": password})


def bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def test_00_first_login_must_change_password(stack, admin: Api):
    client, env, _ = stack
    created = admin.post("/api/users", {"username": "zhou", "display_name": "周雪", "password": "Init-pass-2026",
                                        "roles": ["analyst"]}, expect=201)
    assert created["must_change_password"] is True  # 管理员创建的账号默认首次登录必须改口令
    resp = login(client, "zhou", "Init-pass-2026")
    assert resp.status_code == 200 and resp.json()["must_change_password"] is True
    old = resp.json()["access_token"]
    me = client.get("/api/me", headers=bearer(old)).json()
    assert me["must_change_password"] is True and me["last_login_at"]
    ctx.update(zhou=created["id"], old=old)


def test_01_change_password_ends_other_sessions(stack):
    client, _, _ = stack
    old = ctx["old"]
    other = login(client, "zhou", "Init-pass-2026").json()["access_token"]  # 另一处登录
    wrong = client.post("/api/me/password", headers=bearer(old),
                        json={"current_password": "nope-nope", "new_password": "Brand-new-2026"})
    assert wrong.status_code == 403
    same_as_user = client.post("/api/me/password", headers=bearer(old),
                               json={"current_password": "Init-pass-2026", "new_password": "zhouzhou"})
    assert same_as_user.status_code == 422
    import time
    time.sleep(1.1)  # 令牌 iat 精确到秒：确保两次登录与改口令不在同一秒
    done = client.post("/api/me/password", headers=bearer(old),
                       json={"current_password": "Init-pass-2026", "new_password": "Brand-new-2026"})
    assert done.status_code == 200 and done.json()["must_change_password"] is False
    fresh = done.json()["access_token"]
    for stale in (old, other):
        r = client.get("/api/me", headers=bearer(stale))
        assert r.status_code == 401 and "会话已失效" in r.json()["detail"]
    assert client.get("/api/me", headers=bearer(fresh)).json()["must_change_password"] is False
    assert login(client, "zhou", "Init-pass-2026").status_code == 401
    assert login(client, "zhou", "Brand-new-2026").status_code == 200


def test_02_repeated_failures_lock_login(stack, admin: Api):
    from avelis import cache

    client, _, _ = stack
    for i in range(5):
        r = login(client, "zhou", f"wrong-{i}")
        assert r.status_code == 401
    assert "再失败" not in r.json()["detail"]  # 第 5 次已到上限
    locked = login(client, "zhou", "Brand-new-2026")  # 口令正确也拒绝
    assert locked.status_code == 429 and int(locked.headers["Retry-After"]) > 0
    # 不透露账号是否存在：不存在的用户名同样计数、同样提示
    assert login(client, "ghost", "x").json()["detail"].startswith("用户名或密码错误")
    actions = [e["action"] for e in admin.get("/api/audit", params={"limit": 20})]
    assert "auth.login_locked" in actions and actions.count("auth.login_failed") >= 5
    keys = list(cache.client().scan_iter("avelis:login-fail:*"))
    cache.client().delete(*keys)
    assert login(client, "zhou", "Brand-new-2026").status_code == 200


def test_03_admin_reset_and_deactivate(stack, admin: Api):
    import time

    client, _, _ = stack
    token = login(client, "zhou", "Brand-new-2026").json()["access_token"]
    time.sleep(1.1)
    reset = admin.post(f"/api/users/{ctx['zhou']}/password", {"new_password": "Reset-by-admin-1"})
    assert reset["must_change_password"] is True
    assert client.get("/api/me", headers=bearer(token)).status_code == 401
    again = login(client, "zhou", "Reset-by-admin-1")
    assert again.status_code == 200 and again.json()["must_change_password"] is True

    time.sleep(1.1)
    off = admin.patch(f"/api/users/{ctx['zhou']}", {"is_active": False, "roles": ["analyst", "reviewer"]})
    assert off["is_active"] is False and off["roles"] == ["analyst", "reviewer"]
    assert client.get("/api/me", headers=bearer(again.json()["access_token"])).status_code == 401
    assert login(client, "zhou", "Reset-by-admin-1").status_code == 401
    audit = admin.get("/api/audit", params={"object_type": "user", "object_id": ctx["zhou"]})
    assert {"user.password_reset", "user.update"} <= {e["action"] for e in audit}


def test_04_admin_self_protection_and_permissions(stack, admin: Api):
    client, env, _ = stack
    me = admin.get("/api/me")
    admin.patch(f"/api/users/{me['id']}", {"is_active": False}, expect=409)
    admin.patch(f"/api/users/{me['id']}", {"roles": ["steward"]}, expect=409)
    pw = env["AVELIS_DEMO_USER_PASSWORD"]
    admin.post("/api/users", {"username": "wu", "display_name": "吴", "password": pw, "roles": ["steward"],
                              "must_change_password": False}, expect=201)
    wu = Api(client, "wu", pw)
    wu.patch(f"/api/users/{ctx['zhou']}", {"is_active": True}, expect=403)
    wu.post(f"/api/users/{ctx['zhou']}/password", {"new_password": "Steward-try-1"}, expect=403)
