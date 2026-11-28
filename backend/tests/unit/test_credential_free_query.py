"""免口令的认证方式（Hive / Spark / Trino 的 NONE）：查询账号与采集账号一样可不填，可只填账号作会话用户。

2026-11-28 在真实部署里联调发现：此前查询账号必须带密码，NONE 认证下只能编一个密码，都不填则问数报“未配置查询账号”。
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from avelis.api.datasources import Credential, _set_credential
from avelis.connectors.hive import HiveConnector
from avelis.connectors.spark import SparkConnector
from avelis.connectors.trino import TrinoConnector
from avelis.engines import CredentialMissing, query_connector
from avelis.models import Datasource


class _NoCredential:
    def scalar(self, _):
        return None

    def add(self, _):
        raise AssertionError("不应写入")


class _Recorder(_NoCredential):
    def __init__(self):
        self.added = []

    def add(self, row):
        self.added.append(row)


@pytest.mark.parametrize(
    ("kind", "options", "cls"),
    [
        ("hive", {"databases": ["d"]}, HiveConnector),
        ("spark", {"databases": ["d"], "auth": "NOSASL"}, SparkConnector),
        ("trino", {"catalog": "memory", "schemas": ["s"]}, TrinoConnector),
    ],
)
def test_query_connector_without_credential_under_credential_free_auth(kind, options, cls):
    ds = Datasource(name="源", kind=kind, host="h", port=1, options=options)
    conn = query_connector(_NoCredential(), ds)
    assert isinstance(conn, cls)


def test_query_connector_still_requires_credential_otherwise():
    for kind, options in (("mysql", {"databases": ["d"]}), ("hive", {"databases": ["d"], "auth": "LDAP"})):
        ds = Datasource(name="源", kind=kind, host="h", port=1, options=options)
        with pytest.raises(CredentialMissing):
            query_connector(_NoCredential(), ds)


def test_username_without_password_only_under_credential_free_auth():
    hive = Datasource(id="ds1", name="源", kind="hive", host="h", port=1, options={"databases": ["d"]})
    recorder = _Recorder()
    _set_credential(recorder, hive, "query", Credential(username="ro_user"))
    assert recorder.added[0].username == "ro_user"
    mysql = Datasource(id="ds2", name="源", kind="mysql", host="h", port=1, options={"databases": ["d"]})
    with pytest.raises(HTTPException) as caught:
        _set_credential(_NoCredential(), mysql, "query", Credential(username="ro_user"))
    assert caught.value.status_code == 422
    ldap = Datasource(id="ds3", name="源", kind="hive", host="h", port=1, options={"databases": ["d"], "auth": "LDAP"})
    with pytest.raises(HTTPException):
        _set_credential(_NoCredential(), ldap, "collect", Credential(username="u"))
