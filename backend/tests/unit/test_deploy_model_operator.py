"""bootstrap model 的操作人：部署环境不依赖演示管理员（issue #28）。"""

from __future__ import annotations

import dataclasses
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import pytest

from avelis import bootstrap, modelcfg
from avelis.config import build_settings

SECRET = "offline-unit-test-secret-0123456789"


@pytest.fixture
def fake_db(monkeypatch):
    """session.scalar 依次返回 results 里的值，并记下每条查询的 SQL。"""
    state = SimpleNamespace(results=[], statements=[], session=MagicMock())

    def scalar(statement):
        state.statements.append(str(statement.compile(compile_kwargs={"literal_binds": True})))
        return state.results.pop(0) if state.results else None

    state.session.scalar.side_effect = scalar

    @contextmanager
    def scope():
        yield state.session

    monkeypatch.setattr(bootstrap, "session_scope", scope)
    monkeypatch.setattr(bootstrap, "ensure_admin", Mock(side_effect=AssertionError("不应创建演示管理员")))
    return state


def use_env(monkeypatch, **env):
    settings = dataclasses.replace(build_settings({"AVELIS_SECRET": SECRET}), env={"AVELIS_SECRET": SECRET, **env})
    monkeypatch.setattr(bootstrap, "get_settings", lambda: settings)


def test_operator_is_existing_initial_admin_without_demo_password(monkeypatch, fake_db):
    use_env(monkeypatch, AVELIS_INITIAL_ADMIN_USERNAME="ops-lead")
    fake_db.results = [SimpleNamespace(id="initial-admin-id")]
    assert bootstrap.model_operator() == "initial-admin-id"
    sql = fake_db.statements[0]
    assert "user_role.role = 'admin'" in sql and "is_active IS true" in sql and "'ops-lead'" in sql
    bootstrap.ensure_admin.assert_not_called()


def test_operator_falls_back_to_earliest_active_admin(monkeypatch, fake_db):
    use_env(monkeypatch)
    fake_db.results = [None, SimpleNamespace(id="earliest-admin-id")]
    assert bootstrap.model_operator() == "earliest-admin-id"
    assert "'admin'" in fake_db.statements[0]
    assert "ORDER BY" in fake_db.statements[1] and "LIMIT" in fake_db.statements[1]


def test_operator_without_any_admin_explains_init_admin(monkeypatch, fake_db):
    use_env(monkeypatch)
    with pytest.raises(SystemExit, match="init-admin"):
        bootstrap.model_operator()


def test_demo_environment_keeps_automation_admin(monkeypatch, fake_db):
    use_env(monkeypatch, AVELIS_DEMO_USER_PASSWORD="demo-password-123")
    monkeypatch.setattr(bootstrap, "ensure_admin", Mock(return_value="ops-admin-id"))
    assert bootstrap.model_operator() == "ops-admin-id"
    bootstrap.ensure_admin.assert_called_once_with(superadmin=False)
    assert fake_db.statements == []


def test_model_from_env_registers_with_initial_admin_and_no_demo_password(monkeypatch, fake_db):
    use_env(
        monkeypatch,
        AVELIS_MODEL_PROVIDER="openai",
        AVELIS_MODEL_NAME="deploy-model",
        AVELIS_MODEL_API_KEY="sk-offline-test",
    )
    admin = SimpleNamespace(id="initial-admin-id")
    fake_db.results = [admin, None]  # 操作人；尚无 env-default 提供方
    fake_db.session.get.side_effect = lambda model, key: admin if key == "initial-admin-id" else None
    created = SimpleNamespace(id="provider-id")
    monkeypatch.setattr(modelcfg, "create_provider", Mock(return_value=created))
    monkeypatch.setattr(modelcfg, "check", Mock())
    monkeypatch.setattr(modelcfg, "save_settings", Mock())
    assert bootstrap.model_from_env() == "provider-id"
    assert modelcfg.create_provider.call_args.args[1] is admin
    modelcfg.check.assert_called_once_with(fake_db.session, admin, "provider-id", "deploy-model")
    bootstrap.ensure_admin.assert_not_called()
