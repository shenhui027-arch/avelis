"""演示数据源连接配置回归：替身 API，不连接数据库。"""

from unittest.mock import Mock

import pytest

from avelis import bootstrap, config, demo_setup
from avelis.api import app


@pytest.mark.parametrize("source_host", [None, "host.docker.internal"])
def test_demo_source_host_and_existing_sources_are_reused(monkeypatch, tmp_path, source_host):
    config_file = tmp_path / "avelis.toml"
    config_file.write_text("", encoding="utf-8")
    monkeypatch.setenv("AVELIS_CONFIG", str(config_file))
    monkeypatch.setenv("AVELIS_ENV_FILE", str(tmp_path / "missing.env"))
    monkeypatch.delenv("AVELIS_DEMO_SOURCE_HOST", raising=False)
    if source_host is not None:
        monkeypatch.setenv("AVELIS_DEMO_SOURCE_HOST", source_host)
    for key, value in {
        "AVELIS_DEMO_USER_PASSWORD": "demo-test",
        "SHOP_MYSQL_PORT": "3306",
        "SHOP_MYSQL_COLLECTOR_PASSWORD": "collector-test",
        "SR_QUERY_PORT": "9030",
        "SR_COLLECTOR_PASSWORD": "collector-test",
        "SR_READER_PASSWORD": "reader-test",
        "SHOP_PG_PORT": "5432",
        "SHOP_PG_COLLECTOR_PASSWORD": "collector-test",
        "SHOP_PG_READER_PASSWORD": "reader-test",
    }.items():
        monkeypatch.setenv(key, value)

    records = {"/api/users": [], "/api/domains": [], "/api/datasources": [], "/api/assets": []}
    created_sources = []
    aliases = []

    class Api:
        def __init__(self, *_):
            pass

        def __call__(self, method, path, **kwargs):
            if method == "GET":
                return records[path]
            if method == "POST" and path in records:
                body = kwargs["json"]
                record = {"id": f"{path}-{len(records[path])}", "aliases": [], **body}
                records[path].append(record)
                if path == "/api/datasources":
                    created_sources.append(body)
                return record
            if method == "POST" and path == "/api/endpoint-aliases":
                aliases.append(kwargs["json"])
                for source in records["/api/datasources"]:
                    if source["id"] == kwargs["json"]["datasource_id"]:
                        source["aliases"].append(kwargs["json"])
                return None
            if method == "POST" and path.endswith("/collect?wait=true"):
                return None
            if method == "PUT" and path.startswith("/api/domains/"):
                return None
            raise AssertionError((method, path, kwargs))

    client = Mock()
    monkeypatch.setattr(bootstrap, "ensure_admin", lambda: None)
    monkeypatch.setattr(app, "create_app", lambda: object())
    monkeypatch.setattr(demo_setup, "TestClient", lambda _: client)
    monkeypatch.setattr(demo_setup, "_Api", Api)
    monkeypatch.setattr(demo_setup, "get_settings", config.build_settings)

    demo_setup.setup(reset=False, ec=True, mfg=True)

    assert {source["name"] for source in created_sources} == {
        "电商维表库（MySQL）",
        "演示 StarRocks",
        "门店库（PostgreSQL）",
        demo_setup.EC_SOURCE,
        demo_setup.MFG_SOURCE,
    }
    assert len(created_sources) == 5
    expected_host = source_host or "127.0.0.1"
    assert all(source["host"] == expected_host for source in created_sources)
    assert [alias["host"] for alias in aliases] == ["shop-mysql"]

    monkeypatch.setenv("AVELIS_DEMO_SOURCE_HOST", "another-host.example")
    demo_setup.setup(reset=False, ec=True, mfg=True)

    assert len(created_sources) == 5
    assert all(source["host"] == expected_host for source in records["/api/datasources"])
    client.close.assert_called()
