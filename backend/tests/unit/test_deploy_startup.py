"""部署启动与首个管理员：只用内存对象，不连接数据库、Redis 或模型。"""

from __future__ import annotations

import dataclasses
import importlib.util
import logging
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import pytest

from avelis import bootstrap, db, jobs, server
from avelis.config import ConfigError, build_settings
from avelis.models import AuditEvent, User, UserRole
from avelis.security import hash_password, verify_password


@pytest.fixture
def settings():
    return build_settings({"AVELIS_SECRET": "offline-unit-test-secret-0123456789"})


@pytest.fixture(autouse=True)
def no_external_connections(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("单元测试不得连接外部服务")

    monkeypatch.setattr(bootstrap, "get_engine", forbidden)
    monkeypatch.setattr(bootstrap, "session_scope", forbidden)


@pytest.mark.parametrize("module", [bootstrap, db, jobs, server])
def test_startup_modules_import_without_reading_settings(monkeypatch, module):
    from avelis import config

    def forbidden():
        pytest.fail("模块导入时不得读取 Settings")

    monkeypatch.setattr(config, "get_settings", forbidden)
    name = f"avelis._deploy_import_test_{module.__name__.rsplit('.', 1)[-1]}"
    spec = importlib.util.spec_from_file_location(name, module.__file__)
    imported = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, imported)
    spec.loader.exec_module(imported)


@pytest.mark.parametrize(
    "username, password, message",
    [
        ("admin", "", "须为 12–128"),
        ("admin", "Abcd-123456", "须为 12–128"),
        ("admin", "Xy9!" * 33, "须为 12–128"),
        ("admin", "XXaDmIn-123456", "不能包含用户名"),
        ("Straße", "XXSTRASSE-123456", "不能包含用户名"),
        ("admin", "aaaaaaaaaaaa", "4 种不同字符"),
        ("admin", "abcabcabcabc", "4 种不同字符"),
    ],
)
def test_initial_admin_password_rejects_invalid_values_without_echoing(username, password, message):
    with pytest.raises(ValueError, match=message) as error:
        bootstrap.validate_initial_admin_password(username, password)
    assert "AVELIS_INITIAL_ADMIN_PASSWORD" in str(error.value)
    if password:
        assert password not in str(error.value)


@pytest.mark.parametrize("password", ["abcdabcdabcd", "Zebra-2026-key!"])
def test_initial_admin_password_accepts_four_distinct_characters(password):
    assert bootstrap.validate_initial_admin_password("admin", password) is None


@pytest.mark.parametrize("username", ["", "   "])
def test_initial_admin_rejects_empty_username(username):
    with pytest.raises(bootstrap.InitialAdminError, match="AVELIS_INITIAL_ADMIN_USERNAME 不能为空"):
        bootstrap.validate_initial_admin_password(username, "Zebra-2026-key!")


@pytest.fixture
def admin_session(monkeypatch, settings):
    settings = dataclasses.replace(settings, env={"AVELIS_INITIAL_ADMIN_PASSWORD": "Zebra-2026-key!"})
    monkeypatch.setattr(bootstrap, "get_settings", lambda: settings)
    session = MagicMock()
    session.scalar.side_effect = [None, None]
    writes = []
    session.add.side_effect = writes.append

    def flush():
        next(obj for obj in writes if isinstance(obj, User)).id = "initial-admin-id"

    session.flush.side_effect = flush

    @contextmanager
    def scope():
        yield session

    monkeypatch.setattr(bootstrap, "session_scope", scope)
    monkeypatch.setattr(bootstrap, "hash_password", Mock(return_value="hashed-password"))
    return session, writes


def test_init_admin_creates_only_admin_role_with_forced_password_change_and_audit(admin_session, capsys, caplog):
    session, writes = admin_session
    assert bootstrap.init_admin() == "initial-admin-id"
    user, role, event = writes
    assert isinstance(user, User) and user.username == "admin" and user.is_active is True
    assert user.password_hash == "hashed-password" and user.must_change_password is True
    bootstrap.hash_password.assert_called_once_with("Zebra-2026-key!")
    assert isinstance(role, UserRole) and role.user_id == user.id and role.role == "admin"
    assert isinstance(event, AuditEvent) and event.actor_id is None and event.actor_name == "system"
    assert event.action == "user.create" and event.object_type == "user" and event.object_id == user.id
    assert event.detail == {"username": "admin", "roles": ["admin"], "via": "cli"}
    statement = session.scalar.call_args_list[0].args[0]
    assert "app_user.is_active IS true" in str(statement) and "user_role.role" in str(statement)
    assert statement.compile().params["role_1"] == "admin"
    assert "pg_advisory_xact_lock" in str(session.execute.call_args.args[0])
    assert "Zebra-2026-key!" not in capsys.readouterr().out
    assert not caplog.records


@pytest.mark.parametrize("password", [None, "", "short", "Zebra-2026-key!"])
def test_init_admin_skips_any_existing_enabled_admin_without_writing(
    monkeypatch, settings, admin_session, capsys, caplog, password
):
    env = {} if password is None else {"AVELIS_INITIAL_ADMIN_PASSWORD": password}
    monkeypatch.setattr(bootstrap, "get_settings", lambda: dataclasses.replace(settings, env=env))
    session, writes = admin_session
    session.scalar.side_effect = ["another-admin-id"]
    assert bootstrap.init_admin() is None
    assert writes == [] and session.scalar.call_count == 1
    bootstrap.hash_password.assert_not_called()
    assert capsys.readouterr().out.strip() == "已存在管理员，跳过"
    assert not caplog.records


@pytest.mark.parametrize("password", [None, "Zebra-2026-key!"])
def test_init_admin_refuses_existing_same_name_without_modifying_it(monkeypatch, settings, admin_session, password):
    env = {} if password is None else {"AVELIS_INITIAL_ADMIN_PASSWORD": password}
    monkeypatch.setattr(bootstrap, "get_settings", lambda: dataclasses.replace(settings, env=env))
    session, writes = admin_session
    session.scalar.side_effect = [None, "existing-user-id"]
    with pytest.raises(ValueError, match="同名用户已存在"):
        bootstrap.init_admin()
    assert writes == []
    bootstrap.hash_password.assert_not_called()


@pytest.mark.parametrize("password", ["123456", "short", "Xy9!" * 33, "XXaDmIn-123456", "abcabcabcabc"])
def test_init_admin_rejects_explicit_invalid_password_without_writing(monkeypatch, settings, admin_session, password):
    settings = dataclasses.replace(settings, env={"AVELIS_INITIAL_ADMIN_PASSWORD": password})
    monkeypatch.setattr(bootstrap, "get_settings", lambda: settings)
    with pytest.raises(bootstrap.InitialAdminError, match="AVELIS_INITIAL_ADMIN_PASSWORD") as error:
        bootstrap.init_admin()
    assert password not in str(error.value)
    assert admin_session[1] == []
    bootstrap.hash_password.assert_not_called()


@pytest.mark.parametrize("password", [None, ""])
@pytest.mark.parametrize("username", [None, "deploy-admin"])
def test_init_admin_default_password_is_verifiable_without_forced_change(
    monkeypatch, settings, admin_session, caplog, capsys, password, username
):
    env = {} if password is None else {"AVELIS_INITIAL_ADMIN_PASSWORD": password}
    if username is not None:
        env["AVELIS_INITIAL_ADMIN_USERNAME"] = username
    settings = dataclasses.replace(settings, env=env, database_url="postgresql+psycopg://avelis@db.example/avelis")
    monkeypatch.setattr(bootstrap, "get_settings", lambda: settings)
    monkeypatch.setattr(bootstrap, "hash_password", hash_password)
    monkeypatch.setattr(bootstrap, "_connection_is_local", Mock(side_effect=AssertionError("不得限制数据库位置")))
    with caplog.at_level(logging.WARNING):
        assert bootstrap.init_admin() == "initial-admin-id"
    user, role, event = admin_session[1]
    assert user.username == (username or "admin") and user.is_active is True
    assert user.must_change_password is False
    assert verify_password("123456", user.password_hash)
    assert isinstance(role, UserRole) and role.user_id == user.id and role.role == "admin"
    assert isinstance(event, AuditEvent) and event.action == "user.create"
    assert len(caplog.records) == 1 and caplog.records[0].levelno == logging.WARNING
    assert f"使用默认初始管理员 {user.username}，默认口令请登录后在界面修改" in caplog.text
    assert "必须修改密码" not in capsys.readouterr().out


@pytest.mark.parametrize("username", ["", "   "])
def test_init_admin_default_password_still_rejects_empty_username(monkeypatch, settings, admin_session, username):
    settings = dataclasses.replace(settings, env={"AVELIS_INITIAL_ADMIN_USERNAME": username})
    monkeypatch.setattr(bootstrap, "get_settings", lambda: settings)
    with pytest.raises(bootstrap.InitialAdminError, match="AVELIS_INITIAL_ADMIN_USERNAME 不能为空"):
        bootstrap.init_admin()
    assert admin_session[1] == []
    bootstrap.hash_password.assert_not_called()


def test_init_admin_uses_configured_username(monkeypatch, settings, admin_session):
    settings = dataclasses.replace(
        settings,
        env={"AVELIS_INITIAL_ADMIN_USERNAME": "deploy-admin", "AVELIS_INITIAL_ADMIN_PASSWORD": "Zebra-2026-key!"},
    )
    monkeypatch.setattr(bootstrap, "get_settings", lambda: settings)
    bootstrap.init_admin()
    assert admin_session[1][0].username == "deploy-admin"


@pytest.mark.parametrize("failure", [None, "lock", "migration", "graph", "unlock"])
def test_migrate_locks_upgrade_and_graph_and_invalidates_on_failure(monkeypatch, failure):
    events = []
    connection = MagicMock()
    connection.execution_options.return_value = connection
    engine = MagicMock()
    engine.connect.return_value.__enter__.return_value = connection
    monkeypatch.setattr(bootstrap, "get_engine", lambda: MagicMock())
    lock_engines = []
    monkeypatch.setattr(bootstrap, "create_engine", lambda url, poolclass: lock_engines.append(poolclass) or engine)
    config = object()
    monkeypatch.setattr(bootstrap, "alembic_config", lambda: config)

    def step(name):
        events.append(name)
        if failure == name:
            raise RuntimeError("模拟启动失败")

    def execute(statement, params):
        assert params == {"key": bootstrap.MIGRATION_LOCK_KEY}
        step("unlock" if "pg_advisory_unlock" in str(statement) else "lock")
        assert "pg_try_advisory_lock" not in str(statement)

    def upgrade(actual_config, revision):
        assert actual_config is config and revision == "head"
        step("migration")

    connection.execute.side_effect = execute
    monkeypatch.setattr(bootstrap.command, "upgrade", upgrade)
    monkeypatch.setitem(sys.modules, "avelis.ask.agent", SimpleNamespace(graph=lambda: step("graph")))
    if failure:
        with pytest.raises(RuntimeError, match="模拟启动失败"):
            bootstrap.migrate()
        connection.invalidate.assert_called_once_with()
    else:
        bootstrap.migrate()
        connection.invalidate.assert_not_called()
    connection.detach.assert_not_called()  # 分离后的连接不能再设置隔离级别（真实 SQLAlchemy 会报错）
    connection.execution_options.assert_called_once_with(isolation_level="AUTOCOMMIT")
    assert lock_engines == [bootstrap.NullPool]  # 锁连接不占迁移要用的连接池
    expected = ["lock", "migration", "graph", "unlock"]
    if failure == "lock":
        expected = ["lock"]
    elif failure == "migration":
        expected = ["lock", "migration", "unlock"]
    assert events == expected
    engine.connect.return_value.__exit__.assert_called_once()


def test_bootstrap_migrate_calls_shared_function(monkeypatch):
    migrate = Mock()
    monkeypatch.setattr(bootstrap, "sys", SimpleNamespace(stdout=SimpleNamespace(reconfigure=Mock())))
    monkeypatch.setattr(bootstrap, "migrate", migrate)
    bootstrap.main(["migrate"])
    migrate.assert_called_once_with()


def test_bootstrap_init_admin_reports_validation_failure_with_exit(monkeypatch):
    monkeypatch.setattr(bootstrap, "sys", SimpleNamespace(stdout=SimpleNamespace(reconfigure=Mock())))
    monkeypatch.setattr(bootstrap, "init_admin", Mock(side_effect=bootstrap.InitialAdminError("口令不满足初始化策略")))
    with pytest.raises(SystemExit, match="口令不满足初始化策略"):
        bootstrap.main(["init-admin"])


def test_uvicorn_options_preserve_defaults(settings):
    assert server.uvicorn_options(settings) == {
        "host": "127.0.0.1",
        "port": 8000,
        "workers": 1,
        "proxy_headers": True,
        "forwarded_allow_ips": "127.0.0.1",
        "log_level": "info",
    }


def test_uvicorn_options_pass_configured_proxy_trust_and_workers(settings):
    settings = dataclasses.replace(
        settings, host="0.0.0.0", port=8080, workers=3, forwarded_allow_ips="10.0.0.1,10.0.0.2", log_level="warning"
    )
    options = server.uvicorn_options(settings)
    assert options == {
        "host": "0.0.0.0",
        "port": 8080,
        "workers": 3,
        "proxy_headers": True,
        "forwarded_allow_ips": "10.0.0.1,10.0.0.2",
        "log_level": "warning",
    }


@pytest.fixture
def startup(monkeypatch, settings):
    monkeypatch.setattr(server, "get_settings", lambda: settings)
    migrate, init_admin, run = Mock(), Mock(), Mock()
    monkeypatch.setattr(server, "migrate", migrate)
    monkeypatch.setattr(server, "init_admin", init_admin)
    monkeypatch.setattr(server.uvicorn, "run", run)
    return settings, migrate, init_admin, run


def test_server_default_startup_does_not_initialize(startup):
    settings, migrate, init_admin, run = startup
    server.main()
    migrate.assert_not_called()
    init_admin.assert_not_called()
    run.assert_called_once_with("avelis.api.app:app", **server.uvicorn_options(settings))


def test_server_initializes_before_serving(monkeypatch, startup):
    settings, migrate, init_admin, run = startup
    settings = dataclasses.replace(
        settings, auto_migrate=True, auto_init_admin=True, env={"AVELIS_INITIAL_ADMIN_PASSWORD": "Zebra-2026-key!"}
    )
    monkeypatch.setattr(server, "get_settings", lambda: settings)
    events = []
    basic_config = Mock(side_effect=lambda **kwargs: events.append("logging"))
    monkeypatch.setattr(server.logging, "basicConfig", basic_config)
    migrate.side_effect = lambda: events.append("migrate")
    init_admin.side_effect = lambda: events.append("init-admin")
    run.side_effect = lambda *args, **kwargs: events.append("serve")
    server.main()
    assert events == ["logging", "migrate", "init-admin", "serve"]
    basic_config.assert_called_once_with(level="INFO", format="%(asctime)s %(levelname)s %(name)s %(message)s")


@pytest.mark.parametrize("log_level, expected", [("warning", "WARNING"), ("trace", logging.DEBUG)])
def test_server_configures_supported_log_levels(monkeypatch, startup, log_level, expected):
    settings = dataclasses.replace(startup[0], log_level=log_level)
    monkeypatch.setattr(server, "get_settings", lambda: settings)
    basic_config = Mock()
    monkeypatch.setattr(server.logging, "basicConfig", basic_config)
    server.main()
    basic_config.assert_called_once_with(level=expected, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    assert startup[3].call_args.kwargs["log_level"] == log_level


def test_server_logs_listen_and_public_addresses(monkeypatch, startup, caplog):
    settings = dataclasses.replace(startup[0], host="0.0.0.0", port=8080, public_url="https://avelis.example")
    monkeypatch.setattr(server, "get_settings", lambda: settings)
    with caplog.at_level(logging.INFO):
        server.main()
    assert "监听地址：0.0.0.0:8080" in caplog.text
    assert "访问地址：https://avelis.example" in caplog.text


@pytest.mark.parametrize("module", [server, jobs, bootstrap])
def test_entry_reports_config_problems_before_external_work(monkeypatch, module, capsys):
    config_path = Path("offline-avelis.toml")
    problems = ["[server] port（环境变量 AVELIS_PORT）：格式有误。", "[security] secret：至少 16 字符。"]
    error = ConfigError(problems, config_path=config_path)
    if module is bootstrap:
        monkeypatch.setattr(bootstrap, "sys", SimpleNamespace(stdout=SimpleNamespace(reconfigure=Mock())))
        monkeypatch.setattr(bootstrap, "migrate", Mock(side_effect=error))
        args = (["migrate"],)
    else:
        monkeypatch.setattr(module, "get_settings", Mock(side_effect=error))
        args = ()
        if module is server:
            migrate, run = Mock(), Mock()
            monkeypatch.setattr(server, "migrate", migrate)
            monkeypatch.setattr(server.uvicorn, "run", run)
        else:
            signal = Mock()
            monkeypatch.setattr(jobs.signal, "signal", signal)
    with pytest.raises(SystemExit) as exc:
        module.main(*args)
    assert exc.value.code == 2 and exc.value.__suppress_context__
    output = capsys.readouterr()
    assert output.out == ""
    assert "配置有误（共 2 项）：" in output.err
    assert str(config_path) in output.err
    assert all(problem in output.err for problem in problems)
    assert "Traceback" not in output.err
    if module is server:
        migrate.assert_not_called()
        run.assert_not_called()
    elif module is jobs:
        signal.assert_not_called()


def test_server_migration_failure_prevents_startup(monkeypatch, startup):
    settings, migrate, init_admin, run = startup
    monkeypatch.setattr(server, "get_settings", lambda: dataclasses.replace(settings, auto_migrate=True))
    migrate.side_effect = RuntimeError("迁移失败")
    with pytest.raises(RuntimeError, match="迁移失败"):
        server.main()
    init_admin.assert_not_called()
    run.assert_not_called()


@pytest.mark.parametrize("password", [None, ""])
def test_server_missing_admin_password_creates_default_admin_and_serves(
    monkeypatch, startup, admin_session, caplog, password
):
    settings, _, init_admin, run = startup
    env = {} if password is None else {"AVELIS_INITIAL_ADMIN_PASSWORD": password}
    settings = dataclasses.replace(settings, auto_init_admin=True, env=env)
    monkeypatch.setattr(server, "get_settings", lambda: settings)
    monkeypatch.setattr(bootstrap, "get_settings", lambda: settings)
    monkeypatch.setattr(bootstrap, "hash_password", hash_password)
    init_admin.side_effect = bootstrap.init_admin
    with caplog.at_level(logging.WARNING):
        server.main()
    init_admin.assert_called_once_with()
    user = admin_session[1][0]
    assert user.username == "admin" and user.must_change_password is False
    assert verify_password("123456", user.password_hash)
    assert "使用默认初始管理员 admin" in caplog.text
    assert caplog.records[-1].levelno == logging.WARNING
    run.assert_called_once()


@pytest.mark.parametrize(
    "failure",
    [
        bootstrap.InitialAdminError("口令不满足初始化策略"),
        ValueError("连接详情-不能泄露"),
        RuntimeError("连接详情-不能泄露"),
    ],
)
def test_server_admin_failure_logs_error_without_leaking_connection(monkeypatch, startup, caplog, failure):
    settings, _, init_admin, run = startup
    settings = dataclasses.replace(
        settings, auto_init_admin=True, env={"AVELIS_INITIAL_ADMIN_PASSWORD": "Zebra-2026-key!"}
    )
    monkeypatch.setattr(server, "get_settings", lambda: settings)
    init_admin.side_effect = failure
    with caplog.at_level(logging.ERROR):
        server.main()
    assert caplog.records[-1].levelno == logging.ERROR
    assert "连接详情-不能泄露" not in caplog.text and "Zebra-2026-key!" not in caplog.text
    run.assert_called_once()


def test_database_pool_settings_reach_both_engine_and_session_factory(monkeypatch, settings):
    db._engine.cache_clear()
    db._session_factory.cache_clear()
    create_engine = Mock(side_effect=[object(), object()])
    sessionmaker = Mock(side_effect=[object(), object()])
    monkeypatch.setattr(db, "create_engine", create_engine)
    monkeypatch.setattr(db, "sessionmaker", sessionmaker)
    monkeypatch.setattr(db, "get_settings", lambda: settings)
    try:
        engine, factory = db.get_engine(), db.session_factory()
        assert db.get_engine() is engine and db.session_factory() is factory
        create_engine.assert_called_once_with(
            settings.database_url, pool_pre_ping=True, pool_size=5, max_overflow=10, pool_recycle=1800, future=True
        )
        sessionmaker.assert_called_once_with(bind=engine, expire_on_commit=False, future=True)
        changed = dataclasses.replace(settings, db_pool_size=2, db_max_overflow=0, db_pool_recycle_seconds=90)
        monkeypatch.setattr(db, "get_settings", lambda: changed)
        assert db.get_engine() is not engine and db.session_factory() is not factory
        create_engine.assert_called_with(
            settings.database_url, pool_pre_ping=True, pool_size=2, max_overflow=0, pool_recycle=90, future=True
        )
        assert sessionmaker.call_count == 2
    finally:
        db._engine.cache_clear()
        db._session_factory.cache_clear()
