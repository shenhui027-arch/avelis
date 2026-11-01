"""测试公共设施。

验收测试（-m acceptance）需要完整演示环境：会重置演示 StarRocks（demo.py seed）、清空元数据库并迁移、
清除 Redis 中 avelis:* 键。元数据库后端由 AVELIS_META_BACKEND=pg|mysql 选择。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts"))


class Api:
    """以指定用户身份调用 API，并断言状态码。"""

    def __init__(self, client, username: str, password: str):
        self.client = client
        self.username = username
        resp = client.post("/api/auth/token", data={"username": username, "password": password})
        assert resp.status_code == 200, resp.text
        self.headers = {"Authorization": f"Bearer {resp.json()['access_token']}"}

    def call(self, method: str, path: str, expect: int | tuple[int, ...] = 200, **kwargs):
        resp = self.client.request(method, path, headers=self.headers, **kwargs)
        expected = (expect,) if isinstance(expect, int) else expect
        assert resp.status_code in expected, f"{method} {path} → {resp.status_code}: {resp.text[:2000]}"
        return resp.json()

    def get(self, path, **kw):
        return self.call("GET", path, **kw)

    def post(self, path, json=None, **kw):
        return self.call("POST", path, json=json, **kw)

    def put(self, path, json=None, **kw):
        return self.call("PUT", path, json=json, **kw)

    def patch(self, path, json=None, **kw):
        return self.call("PATCH", path, json=json, **kw)


SECRET_WORDS = ("PASSWORD", "SECRET", "KEY", "TOKEN")


class SecretEnv(dict):
    """演示环境变量：取值照常，repr 隐去口令、密钥类字段。pytest 的失败回溯会打印测试参数（如 stack、demo_env），
    不能把口令打进日志与模型上下文。"""

    def __repr__(self) -> str:
        return (
            "{"
            + ", ".join(
                f"{k!r}: {'***' if any(w in k.upper() for w in SECRET_WORDS) else repr(v)}" for k, v in self.items()
            )
            + "}"
        )

    __str__ = __repr__


@pytest.fixture(scope="session")
def demo_env() -> dict[str, str]:
    import demo  # scripts/demo.py

    if not demo.ENV_FILE.exists():
        pytest.skip("缺少 .env：先运行 uv run scripts/demo.py up")
    return SecretEnv(demo.load_env())
