"""M4A 单元测试：8 连接器 TLS/CA 安全校验、只读会话加固、MultiFernet 双密钥轮换与 JWT 回退解码。"""

from __future__ import annotations

import datetime
import ssl
from types import SimpleNamespace

import pytest
from cryptography import x509
from cryptography.fernet import InvalidToken
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from fastapi import HTTPException
from sqlalchemy import JSON, create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from avelis import bootstrap, config, security
from avelis.api import datasources as ds_api
from avelis.connectors import (
    clickhouse as ch_mod,
)
from avelis.connectors import (
    doris as doris_mod,
)
from avelis.connectors import (
    hive as hive_mod,
)
from avelis.connectors import (
    mysql as mysql_mod,
)
from avelis.connectors import (
    oracle as oracle_mod,
)
from avelis.connectors import (
    postgres as pg_mod,
)
from avelis.connectors import (
    sqlserver as mssql_mod,
)
from avelis.connectors import (
    starrocks as sr_mod,
)
from avelis.connectors import (
    tls,
)
from avelis.db import Base, utcnow
from avelis.engines import get_engine
from avelis.models import AppSetting, Datasource, DatasourceCredential, ModelProvider, User, WebhookSubscription


def _write_self_signed_pem(path) -> None:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "avelis-test-ca")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=30))
        .sign(key, hashes.SHA256())
    )
    path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))


def test_tls_parse_modes_and_ca_dir_containment(tmp_path):
    ca_dir = tmp_path / "certs"
    ca_dir.mkdir()
    pem = ca_dir / "db-ca.pem"
    _write_self_signed_pem(pem)
    outside = tmp_path / "outside.pem"
    outside.write_text("OUTSIDE", encoding="utf-8")

    # 默认 prefer 且无 CA -> 各驱动返回空 kwargs（兼容现有默认直连）
    default_cfg = tls.parse_tls_options({})
    assert default_cfg.mode == "prefer" and default_cfg.ca_cert is None
    assert tls.pymysql_ssl_kwargs(default_cfg) == {}
    assert tls.psycopg_ssl_kwargs(default_cfg) == {}
    assert tls.clickhouse_ssl_kwargs(default_cfg) == {}
    assert tls.pymssql_ssl_kwargs(default_cfg) == {}
    assert tls.oracledb_ssl_kwargs(default_cfg) == {}
    assert tls.impyla_ssl_kwargs(default_cfg) == {}

    # 合法 verify-full + 目录内相对路径 CA
    cfg = tls.parse_tls_options(
        {"tls_mode": "verify-full", "tls_ca_cert": "db-ca.pem"},
        ca_dir=str(ca_dir),
        check_exists=True,
    )
    assert cfg.mode == "verify-full" and cfg.ca_cert == str(pem.resolve())
    assert tls.pymysql_ssl_kwargs(cfg) == {
        "ssl_verify_cert": True,
        "ssl_verify_identity": True,
        "ssl_ca": str(pem.resolve()),
    }
    assert tls.psycopg_ssl_kwargs(cfg) == {"sslmode": "verify-full", "sslrootcert": str(pem.resolve())}
    assert tls.clickhouse_ssl_kwargs(cfg) == {"secure": True, "verify": True, "ca_cert": str(pem.resolve())}
    assert tls.pymssql_ssl_kwargs(cfg) == {"encryption": "require"}
    ora = tls.oracledb_ssl_kwargs(cfg)
    assert ora["protocol"] == "tcps" and ora["ssl_server_dn_match"] is True
    assert isinstance(ora["ssl_context"], ssl.SSLContext) and ora["ssl_context"].verify_mode == ssl.CERT_REQUIRED
    assert tls.impyla_ssl_kwargs(cfg) == {"use_ssl": True, "verify_cert": True, "ca_cert": str(pem.resolve())}

    # 越权路径、目录穿越、非证书扩展名及 disable+CA 均拒绝
    for bad in (
        {"tls_mode": "invalid"},
        {"tls_mode": "verify-ca", "tls_ca_cert": "../outside.pem"},
        {"tls_mode": "verify-ca", "tls_ca_cert": str(outside)},
        {"tls_mode": "verify-ca", "tls_ca_cert": "secret.txt"},
        {"tls_mode": "disable", "tls_ca_cert": "db-ca.pem"},
        {"tls_mode": "verify-full", "tls_ca_cert": "missing.pem"},
    ):
        with pytest.raises(ValueError):
            tls.parse_tls_options(bad, ca_dir=str(ca_dir), check_exists=True)


def test_all_eight_engines_expose_tls_options_and_validate_at_api(monkeypatch):
    for kind in ("starrocks", "mysql", "postgres", "clickhouse", "sqlserver", "oracle", "doris", "hive"):
        desc = get_engine(kind).describe()
        keys = {o["key"] for o in desc["options"]}
        assert "tls_mode" in keys and "tls_ca_cert" in keys

    monkeypatch.setattr(ds_api.access, "require_role", lambda *a, **kw: None)

    class DummySession:
        def scalar(self, *_):
            return None

    bad_body = ds_api.DatasourceCreate(
        name="bad-tls",
        kind="postgres",
        host="db",
        port=5432,
        options={"database": "app", "schemas": ["public"], "tls_mode": "verify-ca", "tls_ca_cert": "../../etc/passwd"},
    )
    with pytest.raises(HTTPException) as exc:
        ds_api.create_datasource(bad_body, user=SimpleNamespace(id="u1"), session=DummySession())
    assert exc.value.status_code == 422


def test_all_eight_connectors_wire_tls_and_readonly_sessions(monkeypatch, tmp_path):
    ca = tmp_path / "ca.pem"
    ca.write_text("CA", encoding="utf-8")
    opts_require = {"tls_mode": "require"}
    opts_full = {"tls_mode": "verify-full", "tls_ca_cert": str(ca)}

    seen: list[tuple[str, dict]] = []

    class FakeCursor:
        description = [("v",)]

        def __init__(self, stmts):
            self.stmts = stmts

        def execute(self, sql, *args, **kwargs):
            self.stmts.append(sql)

        def fetchmany(self, n):
            return [(1,)]

        def fetchone(self):
            return (1,)

        def close(self):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

    class FakeConn:
        def __init__(self):
            self.stmts: list[str] = []

        def cursor(self, *a, **kw):
            return FakeCursor(self.stmts)

        def autocommit(self, *a):
            pass

        def rollback(self):
            pass

        def close(self):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

    # StarRocks / Doris: TLS kwargs + SET SESSION transaction_read_only = 1
    sr_conn = FakeConn()
    monkeypatch.setattr(sr_mod.pymysql, "connect", lambda **kw: (seen.append(("starrocks", kw)), sr_conn)[1])
    sr = sr_mod.StarRocksConnector("h", 9030, "u", "p", {"databases": ["d"], **opts_require})
    sr.execute_select("SELECT 1", timeout_seconds=5, mem_limit_bytes=1024, max_rows=10)
    assert seen[-1][1]["ssl"]["check_hostname"] is False
    assert "SET SESSION transaction_read_only = 1" in sr_conn.stmts

    dr_conn = FakeConn()
    monkeypatch.setattr(doris_mod.pymysql, "connect", lambda **kw: (seen.append(("doris", kw)), dr_conn)[1])
    dr = doris_mod.DorisConnector("h", 9030, "u", "p", {"databases": ["d"], **opts_require})
    dr.execute_select("SELECT 1", timeout_seconds=5, mem_limit_bytes=1024, max_rows=10)
    assert seen[-1][1]["ssl"]["check_hostname"] is False
    assert "SET SESSION transaction_read_only = 1" in dr_conn.stmts

    # MySQL / Postgres / ClickHouse / SQL Server / Oracle / Hive TLS kwargs
    my_conn = FakeConn()
    monkeypatch.setattr(mysql_mod.pymysql, "connect", lambda **kw: (seen.append(("mysql", kw)), my_conn)[1])
    mysql_mod.MySQLMetaConnector("h", 3306, "u", "p", {"databases": ["d"], **opts_full})._connect()
    assert seen[-1][1]["ssl_verify_identity"] is True and seen[-1][1]["ssl_ca"] == str(ca)

    monkeypatch.setattr(pg_mod.psycopg, "connect", lambda **kw: (seen.append(("postgres", kw)), FakeConn())[1])
    pg_mod.PostgresConnector("h", 5432, "u", "p", {"database": "app", "schemas": ["public"], **opts_full})._connect()
    assert seen[-1][1]["sslmode"] == "verify-full" and seen[-1][1]["sslrootcert"] == str(ca)

    monkeypatch.setattr(ch_mod.clickhouse_connect, "get_client", lambda **kw: (seen.append(("ch", kw)), object())[1])
    ch_mod.ClickHouseConnector("h", 8123, "u", "p", {"databases": ["d"], **opts_full})._client()
    assert seen[-1][1]["secure"] is True and seen[-1][1]["verify"] is True and seen[-1][1]["ca_cert"] == str(ca)

    ms_conn = FakeConn()
    monkeypatch.setattr(mssql_mod.pymssql, "connect", lambda **kw: (seen.append(("sqlserver", kw)), ms_conn)[1])
    ms = mssql_mod.SqlServerConnector("h", 1433, "u", "p", {"database": "app", "schemas": ["dbo"], **opts_require})
    ms.execute_select("SELECT 1", timeout_seconds=5, mem_limit_bytes=0, max_rows=10)
    assert seen[-1][1]["encryption"] == "require" and seen[-1][1]["read_only"] is True

    monkeypatch.setattr(oracle_mod.oracledb, "connect", lambda **kw: (seen.append(("oracle", kw)), FakeConn())[1])
    oracle_opts = {"service": "FREEPDB1", "schemas": ["HR"], **opts_require}
    oracle_mod.OracleConnector("h", 1521, "u", "p", oracle_opts)._connect()
    assert seen[-1][1]["protocol"] == "tcps" and seen[-1][1]["ssl_server_dn_match"] is False

    monkeypatch.setattr(hive_mod, "connect", lambda **kw: (seen.append(("hive", kw)), FakeConn())[1])
    hive_mod.HiveConnector("h", 10000, "u", "p", {"databases": ["d"], **opts_full})._connect()
    assert seen[-1][1]["use_ssl"] is True and seen[-1][1]["verify_cert"] is True and seen[-1][1]["ca_cert"] == str(ca)


def test_multifernet_dual_key_jwt_and_rotate_secret_cli(monkeypatch):
    old_secret = "old-secret-0123456789abcdef"
    new_secret = "new-secret-fedcba9876543210"

    # 1. 在旧密钥下加密 6 类敏感字段 + 签发旧 JWT
    s_old = config.build_settings({"AVELIS_SECRET": old_secret})
    monkeypatch.setattr(security, "get_settings", lambda: s_old)
    ds_enc = security.encrypt_secret("db-pass")
    mp_enc = security.encrypt_secret("sk-provider-secret")
    wh_enc = security.encrypt_secret("wh-signing-secret")
    totp_enc = security.encrypt_secret("JBSWY3DPEHPK3PXP")
    totp_pend = security.encrypt_secret("JBSWY3DPEHPK3PXQ")
    fw_enc = security.encrypt_secret("fw-shared")
    oidc_enc = security.encrypt_secret("oidc-client-secret")
    old_jwt, _, _ = security.create_access_token("u1", "alice")

    # 2. 切换为新主密钥 + AVELIS_SECRET_PREVIOUS=old_secret：新密钥可读旧密文与旧 JWT
    s_dual = config.build_settings({"AVELIS_SECRET": new_secret, "AVELIS_SECRET_PREVIOUS": old_secret})
    monkeypatch.setattr(security, "get_settings", lambda: s_dual)
    assert security.decrypt_secret(ds_enc) == "db-pass"
    assert security.needs_secret_rotation(ds_enc) is True
    assert security.decode_access_token(old_jwt)["sub"] == "u1"

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        execution_options={"schema_translate_map": {"avelis_agent": None}},
    )
    for table in Base.metadata.sorted_tables:
        for col in table.columns:
            if isinstance(col.type, JSONB):
                col.type = JSON()
    Base.metadata.create_all(engine)
    try:
        with Session(engine) as s:
            u = User(
                id="u1",
                username="alice",
                display_name="Alice",
                password_hash="h",
                is_active=True,
                totp_secret_enc=totp_enc,
                totp_pending_secret_enc=totp_pend,
            )
            ds = Datasource(id="ds1", name="pg", kind="postgres", host="h", port=5432, options={})
            cred = DatasourceCredential(datasource_id="ds1", purpose="query", username="q", secret_enc=ds_enc)
            prov = ModelProvider(
                id="p1",
                code="ds",
                name="DeepSeek",
                kind="openai_compatible",
                base_url="https://api.deepseek.com",
                api_key_enc=mp_enc,
                models=[],
                created_by="u1",
            )
            sub = WebhookSubscription(
                id="wh1", name="hooks", url="https://hooks.invalid/a", event_types=[], secret_enc=wh_enc
            )
            cfg = AppSetting(
                key="auth_config",
                value={"forward_auth_secret_enc": fw_enc, "oidc": {"client_secret_enc": oidc_enc}},
                updated_at=utcnow(),
            )
            s.add_all([u, ds, cred, prov, sub, cfg])
            s.commit()

            # dry-run 不修改数据库密文
            dry = bootstrap.rotate_secrets(s, dry_run=True)
            assert dry["total_rotated"] == 7
            assert s.get(DatasourceCredential, cred.id).secret_enc == ds_enc

            # 实际轮换后，移除 AVELIS_SECRET_PREVIOUS 仍可解密全部 7 处密文，旧密钥单独无法再解
            res = bootstrap.rotate_secrets(s, dry_run=False)
            assert res["total_rotated"] == 7
            s.commit()

            s_new_only = config.build_settings({"AVELIS_SECRET": new_secret})
            monkeypatch.setattr(security, "get_settings", lambda: s_new_only)
            assert security.decrypt_secret(s.get(DatasourceCredential, cred.id).secret_enc) == "db-pass"
            assert security.decrypt_secret(s.get(ModelProvider, "p1").api_key_enc) == "sk-provider-secret"
            assert security.decrypt_secret(s.get(WebhookSubscription, "wh1").secret_enc) == "wh-signing-secret"
            assert security.decrypt_secret(s.get(User, "u1").totp_secret_enc) == "JBSWY3DPEHPK3PXP"
            assert security.decrypt_secret(s.get(User, "u1").totp_pending_secret_enc) == "JBSWY3DPEHPK3PXQ"
            cfg_after = s.get(AppSetting, "auth_config").value
            assert security.decrypt_secret(cfg_after["forward_auth_secret_enc"]) == "fw-shared"
            assert security.decrypt_secret(cfg_after["oidc"]["client_secret_enc"]) == "oidc-client-secret"

            # 再次执行轮换为幂等 0 条
            res2 = bootstrap.rotate_secrets(s, dry_run=False)
            assert res2["total_rotated"] == 0

            # 用旧密钥单独解密新密文会失败
            monkeypatch.setattr(security, "get_settings", lambda: s_old)
            with pytest.raises(InvalidToken):
                security.decrypt_secret(s.get(ModelProvider, "p1").api_key_enc)
    finally:
        engine.dispose()
