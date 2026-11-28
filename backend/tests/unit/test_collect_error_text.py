"""采集错误归类：各引擎驱动的真实报错样例 → 类别；认不出的不给说明。"""

import pytest

from avelis.collect import error_text as e


@pytest.mark.parametrize(
    ("raw", "kind"),
    [
        # 连不上
        (
            "OperationalError: (2003, \"Can't connect to MySQL server on 'host.docker.internal' "
            '([Errno 101] Network is unreachable)")',
            "unreachable",
        ),
        (
            "psycopg.OperationalError: connection failed: Connection refused\n\tIs the server running on that host?",
            "unreachable",
        ),
        ('could not translate host name "db.internal" to address: Name or service not known', "unreachable"),
        ("DatabaseError: ORA-12541: TNS:no listener", "unreachable"),
        ("OSError: [Errno 111] Connect call failed", "unreachable"),
        # 超时
        ("TimeoutError: timed out", "timeout"),
        ("连接超时（8秒）", "timeout"),
        ("Read timeout on endpoint URL", "timeout"),
        # 口令
        (
            "OperationalError: (1045, \"Access denied for user 'avelis_collect'@'10.0.0.1' (using password: YES)\")",
            "auth",
        ),
        ('FATAL:  password authentication failed for user "avelis_ro"', "auth"),
        ("Login failed for user 'avelis'. (18456)", "auth"),
        ("DatabaseError: ORA-01017: invalid username/password; logon denied", "auth"),
        ("Code: 516. DB::Exception: avelis: Authentication failed: password is incorrect", "auth"),
        # 权限
        ("OperationalError: (1142, \"SELECT command denied to user 'ro'@'%' for table 'tables'\")", "permission"),
        ("permission denied for schema finance", "permission"),
        ("DatabaseError: ORA-01031: insufficient privileges", "permission"),
        ("Access denied; you need (at least one of) the PROCESS privilege(s)", "permission"),
        # TLS（优先于其他：证书错误里常带 connection 字样）
        ("SSL connection has been closed unexpectedly", "tls"),
        ("[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: unable to get local issuer certificate", "tls"),
        # 认不出
        ("KeyError: 'TABLE_SCHEMA'", "unknown"),
    ],
)
def test_classify_real_driver_messages(raw, kind):
    assert e.classify(raw) == kind


@pytest.mark.parametrize("raw", [None, "", "   "])
def test_empty_raw_has_no_kind_or_text(raw):
    assert e.classify(raw) is None
    assert e.explain(raw) is None


def test_explain_gives_text_only_for_known_kinds():
    assert e.explain("TimeoutError: timed out") == "连接数据库超时：请检查网络，或稍后重试"
    assert e.explain("Login failed for user 'x'") == "账号或口令错误：请检查采集账号"
    assert e.explain("KeyError: 'x'") is None
    assert set(e.ERROR_KINDS) == {"unreachable", "timeout", "auth", "permission", "tls", "unknown"}
