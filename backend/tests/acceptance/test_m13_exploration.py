"""探索查询在真实引擎上的验收（ADR 0009 第 6 点）：演示 StarRocks（合成电商数据集）与 PostgreSQL 门店库。

经分析工具箱的统一分发层（与模型调用同一路径）发起 query_sql（模型写 Avelis 标准 SQL，不填数据源：系统按 SQL 里的
资产确定数据源，生成目标方言再执行），断言：
- 正常执行：结果与用 root / postgres 直接查询的值一致，执行的是重新生成的 SQL（带行数上限，PostgreSQL 用 FROM ONLY），
  审计记录执行身份与结果摘要；
- 超时：数据库在服务端取消（StarRocks query_timeout，errno 5024；PostgreSQL statement_timeout，SQLSTATE 57014），
  执行期间能在 processlist / pg_stat_activity 看到这条查询，返回后不再运行；StarRocks 客户端等待超时时发 KILL QUERY，
  执行中的查询被取消；
- 拒绝：写语句、注释与 hint、禁用函数、方言写法、非等值 / 不相连的 JOIN、SELECT *、多语句、未采集的表、不可见的表、
  跨数据源、关闭了探索的数据源、没有探索档案的引擎，一律不发到数据库（连接器的 execute_explore 未被调用），审计记为 rejected。
"""

from __future__ import annotations

import json
import threading
import time
from decimal import Decimal

import pytest

from tests.acceptance.test_m11_analysis import toolbox
from tests.conftest import Api

pytestmark = [pytest.mark.acceptance, pytest.mark.usefixtures("stack")]
ctx: dict = {}
PG_SOURCE = "门店库（PostgreSQL）"
MYSQL_SOURCE = "电商维表库（MySQL）"
LOAD_TABLE = "explore_load"  # 超时用例临时建在门店库里的大表（门店库只有几行，最多 4 个 JOIN 构造不出慢查询）
SR_SLOW = ("SELECT COUNT(*) AS n FROM default_catalog.ec_member.customer AS m1 "
           "JOIN default_catalog.ec_member.customer AS m2 ON m1.member_level = m2.member_level "
           "JOIN default_catalog.ec_member.customer AS m3 ON m2.member_level = m3.member_level")
PG_SLOW = (f"SELECT COUNT(*) AS n FROM storedb.store.{LOAD_TABLE} AS a "
           f"JOIN storedb.store.{LOAD_TABLE} AS b ON a.bucket = b.bucket "
           f"JOIN storedb.store.{LOAD_TABLE} AS c ON b.bucket = c.bucket")


@pytest.fixture
def sent(monkeypatch):
    """记录真正发到数据库的探索查询：包装两个连接器的 execute_explore（仍然调用原方法）。"""
    from avelis.connectors.postgres import PostgresConnector
    from avelis.connectors.starrocks import StarRocksConnector

    calls: list[tuple[str, str]] = []
    for cls in (StarRocksConnector, PostgresConnector):
        original = cls.execute_explore

        def spy(self, sql, *args, _original=original, _name=cls.__name__, **kwargs):
            calls.append((_name, sql))
            return _original(self, sql, *args, **kwargs)
        monkeypatch.setattr(cls, "execute_explore", spy)
    return calls


def explore_audits(session, run_id: str) -> list[dict]:
    from avelis.models import AuditEvent
    from sqlalchemy import select

    session.flush()
    return [e.detail for e in session.scalars(select(AuditEvent).where(
        AuditEvent.action == "analysis.explore", AuditEvent.object_id == run_id).order_by(AuditEvent.at))]


def query(box, sql: str, purpose: str = "验收：探索查询") -> dict:
    return json.loads(box.dispatch("query_sql", {"sql": sql, "purpose": purpose}))


def sr_reader_queries(env) -> list[tuple]:
    """avelis_reader 正在执行的语句（root 查 processlist）：(连接号, 命令, 语句)。"""
    import demo

    conn = demo.sr_root(env)
    try:
        with conn.cursor() as cur:
            cur.execute("SHOW FULL PROCESSLIST")  # 列序随版本不同（3.x 前面有 ServerName）：按列名取
            names = [d[0] for d in cur.description]
            rows = [dict(zip(names, r)) for r in cur.fetchall()]
        return [(int(r["Id"]), r["Command"], r["Info"] or "") for r in rows
                if r["User"] == "avelis_reader" and r["Command"] == "Query"]
    finally:
        conn.close()


def pg_reader_queries(env) -> list[str]:
    import demo

    with demo.pg_connect(env) as conn:
        return [r[0] for r in conn.execute(
            "SELECT query FROM pg_stat_activity WHERE usename = 'avelis_reader' AND state = 'active'").fetchall()]


def run_while_watching(call, watch, marker: str) -> tuple[object, list, float]:
    """在线程里执行 call，同时轮询数据库里正在运行的语句；返回 (结果, 执行期间看到的语句, 耗时)。"""
    out: dict = {}
    seen: list = []
    started = time.monotonic()
    worker = threading.Thread(target=lambda: out.setdefault("value", call()))
    worker.start()
    while worker.is_alive():
        seen += [q for q in watch() if marker in str(q)]
        time.sleep(0.3)
    worker.join()
    return out.get("value"), seen, time.monotonic() - started


def test_00_setup(request, admin: Api):
    import demo
    import ec_data
    from avelis import demo_setup

    _, env, _ = request.getfixturevalue("stack")  # 不作为参数接收：失败回溯会打印参数值（含口令）
    conn = demo.sr_root(env)
    try:
        assert ec_data.loaded_checksum(conn), "电商数据集未灌入：先运行 uv run scripts/ec_data.py load"
    finally:
        conn.close()
    demo_setup.setup(reset=False, ec=True)
    sources = {d["name"]: d for d in admin.get("/api/datasources")}
    ctx.update(sr=sources[demo_setup.EC_SOURCE], pg=sources[PG_SOURCE], mysql=sources[MYSQL_SOURCE])
    assert ctx["sr"]["allow_exploration"] and ctx["sr"]["exploration_supported"]  # 默认开启
    assert ctx["pg"]["allow_exploration"] and ctx["pg"]["exploration_supported"]
    assert not ctx["mysql"]["exploration_supported"]  # 第一版只有 StarRocks、PostgreSQL 有探索档案
    ctx["env"] = env


def test_01_normal_execution_on_starrocks_and_postgres(sent):
    import demo

    env = ctx["env"]
    scope, session, run, box = toolbox("wang")
    try:
        sr = query(box, "SELECT region_code, COUNT(order_id) AS orders "
                   "FROM default_catalog.ec_trade.orders WHERE order_date >= '2026-09-01' AND order_date <= '2026-09-30' "
                   "GROUP BY region_code ORDER BY region_code")
        assert "error" not in sr, sr
        root = demo.sr_root(env)
        try:
            with root.cursor() as cur:
                cur.execute("SELECT region_code, COUNT(order_id) FROM ec_trade.orders "
                            "WHERE order_date BETWEEN '2026-09-01' AND '2026-09-30' GROUP BY region_code ORDER BY region_code")
                expected = [[r[0], int(r[1])] for r in cur.fetchall()]
        finally:
            root.close()
        assert [[r[0], int(r[1])] for r in sr["rows"]] == expected and len(expected) >= 4
        assert "LIMIT" in sr["sql"] and "default_catalog.ec_trade.orders" in sr["sql"].replace("`", "")
        ds_sr = box.datasets()[sr["dataset"]]
        assert ds_sr.kind == "explore" and ds_sr.meta["engine"] == "starrocks"
        assert ds_sr.meta["units"]["orders"]["kind"] == "count"

        pg = query(box, "SELECT h.city, SUM(s.amount) AS amount FROM store.sales AS s "  # 两段名：数据源的默认库
                   "JOIN store.shops AS h ON h.shop_id = s.shop_id WHERE s.status = 'paid' "
                   "GROUP BY h.city ORDER BY h.city")
        assert "error" not in pg, pg
        assert (sr["datasource"], sr["engine"], pg["datasource"], pg["engine"]) == (
            demo_setup_ec_source(), "starrocks", PG_SOURCE, "postgres")  # 按资产路由到各自的数据源
        assert "`" in sr["sql"] and '"' in pg["sql"]  # 生成各自的方言
        with demo.pg_connect(env) as conn:
            expected = [[r[0], r[1]] for r in conn.execute(
                "SELECT h.city, SUM(s.amount) FROM store.sales s JOIN store.shops h ON h.shop_id = s.shop_id "
                "WHERE s.status = 'paid' GROUP BY h.city ORDER BY h.city").fetchall()]
        assert [[r[0], Decimal(str(r[1]))] for r in pg["rows"]] == expected
        assert {r[0]: Decimal(str(r[1])) for r in pg["rows"]} == {"上海": Decimal("290.00"), "北京": Decimal("80.00")}
        assert pg["sql"].count("FROM ONLY") + pg["sql"].count("JOIN ONLY") == 2 and "LIMIT" in pg["sql"]

        assert [name for name, _ in sent] == ["StarRocksConnector", "PostgresConnector"]
        assert [s for _, s in sent] == [sr["sql"], pg["sql"]]  # 发到数据库的是校验后重新生成的 SQL
        audits = explore_audits(session, run.id)
        assert [(a["outcome"], a["sent_to_database"], a["engine"]) for a in audits] == [
            ("ok", True, "starrocks"), ("ok", True, "postgres")]
        assert all(a["execution_identity"] and a["digest"] and a["limits"]["timeout_seconds"] > 0 for a in audits)
        assert audits[1]["rows"] == 2 and audits[1]["sql"] == pg["sql"]
        assert box.budget.used_explore == 2
    finally:
        scope.__exit__(None, None, None)


def demo_setup_ec_source() -> str:
    from avelis import demo_setup

    return demo_setup.EC_SOURCE


def test_02_starrocks_timeout_is_cancelled_on_server(sent):
    env = ctx["env"]
    scope, session, run, box = toolbox("wang")
    box.EXPLORE_MAX_SECONDS = 2  # 本次运行的探索超时（统一截止时间内的上限）
    try:
        view, seen, seconds = run_while_watching(lambda: query(box, SR_SLOW, "验收：超时"),
                                                 lambda: sr_reader_queries(env), "ec_member")
        assert view["code"] == "timeout" and "timeout" in view["error"], view
        assert seen, "执行期间应能在 processlist 看到这条查询"
        assert seconds < 8  # 2 秒服务端超时，不等客户端读超时
        time.sleep(0.5)
        assert not [q for q in sr_reader_queries(env) if "ec_member" in q[2]]  # 返回后服务端不再运行
        audit = explore_audits(session, run.id)[-1]
        assert audit["outcome"] == "failed" and audit["sent_to_database"] and audit["code"] == "timeout"
        assert audit["limits"]["timeout_seconds"] == 2 and "5024" not in json.dumps(view)  # 不向模型回传原始错误码
        assert len(sent) == 1 and not box.datasets()
    finally:
        scope.__exit__(None, None, None)


def test_03_starrocks_kill_query_cancels_running_statement():
    """客户端等待超时的兜底：KILL QUERY 取消执行中的查询（连接仍在，查询收到中断）；目标连接不存在时如实报已结束。"""
    import pymysql

    from avelis.connectors.base import QueryError
    from avelis.connectors.starrocks import StarRocksConnector

    env = ctx["env"]
    reader = StarRocksConnector("127.0.0.1", int(env["SR_QUERY_PORT"]), "avelis_reader", env["SR_READER_PASSWORD"], {})
    conn = reader._connect(read_timeout=60)
    try:
        cur = conn.cursor(pymysql.cursors.Cursor)
        cur.execute("SELECT CONNECTION_ID()")
        connection_id = int(cur.fetchone()[0])
        cur.execute("SET query_timeout = 60")
        errors: list = []

        def heavy():
            try:
                cur.execute(SR_SLOW)
            except pymysql.MySQLError as exc:
                errors.append(exc)
        worker = threading.Thread(target=heavy)
        worker.start()
        deadline = time.monotonic() + 10
        while not [q for q in sr_reader_queries(env) if q[0] == connection_id] and time.monotonic() < deadline:
            time.sleep(0.2)
        assert [q for q in sr_reader_queries(env) if q[0] == connection_id], "查询应已在服务端运行"
        started = time.monotonic()
        assert reader._kill_query(connection_id) == ("kill_sent", None)
        worker.join(15)
        assert not worker.is_alive() and errors and time.monotonic() - started < 10  # 查询收到中断，不等 60 秒超时
        assert not [q for q in sr_reader_queries(env) if q[0] == connection_id]
        cur.execute("SELECT 1")  # 连接仍然可用：取消的是查询，不是连接
        assert cur.fetchone()[0] == 1
    finally:
        conn.close()
    assert reader._kill_query(2_000_000_000) == ("already_finished", None)  # 1094：目标连接不存在

    original = reader._connect  # 端到端：客户端读超时先到（服务端超时设得更长）时，execute_explore 发 KILL 并如实报告
    reader._connect = lambda read_timeout=60: original(read_timeout=3 if read_timeout > 10 else read_timeout)
    started = time.monotonic()
    with pytest.raises(QueryError) as timeout:
        reader.execute_explore(SR_SLOW, timeout_seconds=30, max_rows=10, mem_limit_bytes=2 * 1024 ** 3)
    assert timeout.value.code == "timeout" and timeout.value.db_errno == 2013
    assert timeout.value.cancel == "kill_sent" and time.monotonic() - started < 10
    time.sleep(0.5)
    assert not [q for q in sr_reader_queries(env) if "ec_member" in q[2]]


@pytest.fixture
def load_table(admin: Api):
    """门店库里临时建一张 2 万行的表并采集、归入交易域；结束后删除并重新采集。"""
    import demo

    env = ctx["env"]
    with demo.pg_connect(env) as conn:
        conn.execute(f"DROP TABLE IF EXISTS store.{LOAD_TABLE}")
        conn.execute(f"CREATE TABLE store.{LOAD_TABLE} (id integer PRIMARY KEY, bucket integer NOT NULL)")
        conn.execute(f"INSERT INTO store.{LOAD_TABLE} SELECT g, g % 4 FROM generate_series(1, 20000) AS g")
        conn.execute(f"GRANT SELECT ON store.{LOAD_TABLE} TO avelis_reader")
    try:
        assert admin.post(f"/api/datasources/{ctx['pg']['id']}/collect?wait=true")["status"] == "succeeded"
        asset = next(a for a in admin.get("/api/assets", params={"datasource_id": ctx["pg"]["id"]})
                     if a["qualified"] == f"storedb.store.{LOAD_TABLE}")
        trade = next(d for d in admin.get("/api/domains") if d["code"] == "trade")
        admin.patch(f"/api/assets/{asset['id']}", {"domain_id": trade["id"]})
        yield
    finally:
        with demo.pg_connect(env) as conn:
            conn.execute(f"DROP TABLE IF EXISTS store.{LOAD_TABLE}")
        admin.post(f"/api/datasources/{ctx['pg']['id']}/collect?wait=true")


def test_04_postgres_timeout_is_cancelled_on_server(load_table, sent):
    env = ctx["env"]
    scope, session, run, box = toolbox("wang")
    box.EXPLORE_MAX_SECONDS = 2
    try:
        view, seen, seconds = run_while_watching(lambda: query(box, PG_SLOW, "验收：超时"),
                                                 lambda: pg_reader_queries(env), LOAD_TABLE)
        assert view["code"] == "timeout", view
        assert seen and "ONLY" in seen[0]  # 执行期间 pg_stat_activity 里是重新生成的 SQL
        assert seconds < 8
        assert not [q for q in pg_reader_queries(env) if LOAD_TABLE in q]  # statement_timeout 已在服务端取消
        audit = explore_audits(session, run.id)[-1]
        assert audit["outcome"] == "failed" and audit["sent_to_database"] and audit["code"] == "timeout"
        assert audit["limits"]["timeout_seconds"] == 2
        assert len(sent) == 1 and sent[0][0] == "PostgresConnector" and not box.datasets()
    finally:
        scope.__exit__(None, None, None)


REJECTIONS = [  # (引擎, 标准 SQL, 拒绝码)
    ("pg", "DELETE FROM storedb.store.sales WHERE sale_id = 9001", "not_select"),
    ("pg", "SELECT pg_sleep(5) AS s FROM storedb.store.sales AS s", None),
    ("pg", "SELECT /* 注释 */ sale_id FROM storedb.store.sales", "comment"),
    ("sr", "SELECT /*+ SET_VAR(query_timeout = 3000) */ order_id FROM default_catalog.ec_trade.orders", None),
    ("pg", "SELECT * FROM storedb.store.sales", "star"),
    ("pg", "SELECT sale_id FROM storedb.store.sales; DROP TABLE storedb.store.sales", "multiple_statements"),
    ("pg", "SELECT s.sale_id FROM storedb.store.sales AS s JOIN storedb.store.shops AS h ON s.shop_id > h.shop_id",
     "non_equi_join"),
    ("sr", "SELECT COUNT(*) AS n FROM default_catalog.ec_trade.orders AS a JOIN default_catalog.ec_trade.orders AS b "
           "ON a.order_id = a.order_id", None),
    ("pg", "SELECT rolname FROM storedb.pg_catalog.pg_authid", None),
    ("sr", "SELECT table_name FROM default_catalog.information_schema.tables", None),
    ("sr", "SELECT `order_id` FROM default_catalog.ec_trade.orders", "parse_error"),  # 方言写法不是标准 SQL
    ("sr", "SELECT o.order_id FROM default_catalog.ec_trade.orders AS o JOIN storedb.store.sales AS s "
           "ON o.order_id = s.sale_id", "cross_datasource"),  # 一条查询只能用同一个数据源
]


def test_05_rejections_never_reach_the_database(admin: Api, sent):
    from avelis.analysis.tools import Budget

    scope, session, run, box = toolbox("wang", Budget(explore=20))  # 一次连发十几条；预算本身见 test_06
    try:
        views = [query(box, sql, "验收：拒绝") for _, sql, _ in REJECTIONS]
        for (src, sql, code), view in zip(REJECTIONS, views):
            assert "error" in view and view.get("sent_to_database") is False, (sql, view)
            if code:
                assert view["code"] == code, (sql, view)
        assert views[1]["code"] in ("banned_function", "function_not_allowed")
        assert views[3]["code"] in ("comment", "select_clause_not_allowed")  # StarRocks 方言把 hint 解析成子句
        assert views[7]["code"] in ("join_not_linked", "non_equi_join", "cartesian")
        assert views[8]["code"] in ("asset_not_visible", "unbound_table") and views[9]["code"] in (
            "asset_not_visible", "unbound_table")
        disabled = query(box, "SELECT COUNT(*) AS x FROM storedb.store.sales")  # 有效查询，下面关掉数据源后再试
        assert "error" not in disabled, disabled
        sent.clear()
        admin.put(f"/api/datasources/{ctx['pg']['id']}/exploration", {"allow": False})
        try:
            off = query(box, "SELECT COUNT(*) AS x FROM storedb.store.sales")
        finally:
            admin.put(f"/api/datasources/{ctx['pg']['id']}/exploration", {"allow": True})
        assert off["code"] == "exploration_disabled" and off["sent_to_database"] is False
        # 两段名 shop_dim.dim_product：StarRocks 里同名表在外部 catalog shop_mysql，不是默认 catalog，只剩 MySQL 的那张
        mysql = query(box, "SELECT product_id FROM shop_dim.dim_product")
        assert mysql["code"] == "engine_unsupported", mysql
        external = query(box, "SELECT product_id FROM shop_mysql.shop_dim.dim_product")  # 三段名：StarRocks 外部表
        assert external["code"] == "kind_not_allowed", external
        assert sent == []  # 以上拒绝一条都没有发到数据库
        audits = explore_audits(session, run.id)
        rejected = [a for a in audits if a["outcome"] == "rejected"]
        assert len(rejected) == len(REJECTIONS) + 3 and not any(a["sent_to_database"] for a in rejected)
        assert all(a.get("code") for a in rejected)
    finally:
        scope.__exit__(None, None, None)

    scope, session, run, box = toolbox("sun")  # 孙悦不在电商经营域：不可见的表与不存在的表报同样的错
    try:
        hidden = query(box, "SELECT order_id FROM default_catalog.ec_trade.orders")
        missing = query(box, "SELECT order_id FROM default_catalog.ec_trade.no_such_table")
        assert hidden["code"] == missing["code"] == "asset_not_visible" and hidden["sent_to_database"] is False
        assert hidden["error"].replace("orders", "X") == missing["error"].replace("no_such_table", "X")
        assert sent == []
    finally:
        scope.__exit__(None, None, None)


def test_06_budget_counts_rejected_calls(sent):
    from avelis.analysis.tools import Budget

    scope, session, run, box = toolbox("wang", Budget(explore=2))
    try:
        query(box, "DELETE FROM storedb.store.sales")
        query(box, "SELECT * FROM storedb.store.sales")
        over = query(box, "SELECT sale_id FROM storedb.store.sales")
        assert "上限" in over["error"] and box.budget.used_explore == 2 and sent == []
    finally:
        scope.__exit__(None, None, None)
