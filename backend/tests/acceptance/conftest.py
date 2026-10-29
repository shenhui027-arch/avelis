from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tests.conftest import Api


@pytest.fixture(scope="module")
def stack(demo_env):
    """重置演示库与元数据库，返回 (TestClient, 环境变量, root 连接工厂)。"""
    import demo

    from avelis import cache
    from avelis.api.app import create_app
    from avelis.bootstrap import ensure_admin
    from avelis.devtools import reset_metadata_db

    demo.seed(demo_env)
    assert demo.check(demo_env), "演示库基线不符"
    reset_metadata_db()
    keys = list(cache.client().scan_iter("avelis:*"))
    if keys:
        cache.client().delete(*keys)
    ensure_admin()
    client = TestClient(create_app())
    yield client, demo_env, lambda: demo.sr_connect(demo_env)
    client.close()


@pytest.fixture(scope="module")
def admin(stack):
    client, env, _ = stack
    return Api(client, "admin", env["AVELIS_DEMO_USER_PASSWORD"])
