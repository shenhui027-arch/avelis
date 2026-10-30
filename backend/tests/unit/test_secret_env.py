"""验收用的环境变量在失败回溯里不显示口令（验收模块的 stack、demo_env 都是这个对象）。"""

from __future__ import annotations

import pytest

from tests.conftest import SecretEnv

SECRET = "s3cr3t-Pa55word-XYZ"


def _fails(stack, demo_env):
    client, env, _ = stack
    assert env["SR_QUERY_PORT"] == "0"  # 故意失败


def test_failure_traceback_hides_secrets():
    env = SecretEnv({"SR_QUERY_PORT": "9030", "SR_ROOT_PASSWORD": SECRET, "AVELIS_MODEL_API_KEY": SECRET,
                     "AVELIS_JWT_SECRET": SECRET, "SOME_TOKEN": SECRET})
    with pytest.raises(AssertionError) as info:
        _fails((object(), env, lambda: None), env)
    for style in ("long", "short", "native"):
        text = str(info.getrepr(style=style, showlocals=True, funcargs=True))
        assert SECRET not in text, style
    assert "SR_QUERY_PORT" in repr(env) and "'9030'" in repr(env) and "***" in repr(env)
    assert env["SR_ROOT_PASSWORD"] == SECRET and dict(env)["SR_ROOT_PASSWORD"] == SECRET  # 取值不受影响
