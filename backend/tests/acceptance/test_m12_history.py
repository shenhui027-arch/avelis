"""变更留底验收（ADR 0008，真实演示库）：

01–04 归因：不同数据库账号改表结构 → 采集 → 变更事件记录正确的数据库用户（PostgreSQL 事件触发器、ClickHouse
      query_log、Doris audit_log）；审计记录缺失时为 unmatched；未接入审计的 StarRocks 为 unsupported。
      列注释变化生成 column_comment_changed。
05    检索与导出：按数据源、用户、类型筛选；CSV / JSON 导出与筛选一致，内容哈希与行哈希可核对。
06    只追加：应用账号直接 UPDATE / DELETE / TRUNCATE 历史表被拒绝；状态列只能改一次。
07    防篡改：超级权限绕过触发器改内容、改状态列、删行后，核验指出断链位置；恢复原值后链恢复完整。
08    保留期：模拟时间推移后清理，当前版本、被已发布口径引用的版本与未处理的变更集保留；链仍可核验；
      链不完整时拒绝清理。
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import secrets
import time
from datetime import timedelta

import pytest
from sqlalchemy import text

from tests.conftest import Api

pytestmark = [pytest.mark.acceptance, pytest.mark.engines]

DEV_USER = "avelis_dev"  # 验收临时建的第二个数据库账号（每次随机口令，结束时删除）


def _events(api: Api, ds: str, **params) -> list[dict]:
    return api.get("/api/change-events", params={"datasource_id": ds, "limit": 200, **params})["items"]


def _by(events: list[dict], kind: str, asset_suffix: str, column: str | None) -> dict:
    hits = [e for e in events if e["kind"] == kind and (e["asset"] or "").endswith(asset_suffix)
            and e["column"] == column]
    assert len(hits) == 1, (kind, asset_suffix, column, [(e["kind"], e["asset"], e["column"]) for e in events])
    return hits[0]


def page_all(api: Api) -> int:
    return api.get("/api/change-events", params={"limit": 1})["total"]


def _collect(api: Api, ds: str) -> None:
    run = api.post(f"/api/datasources/{ds}/collect?wait=true")
    assert run["status"] == "succeeded", run


def _past_boundary() -> None:
    """按时间判断窗口的引擎（ClickHouse、Doris）：快照开始前容差秒内的语句只算边界候选。等过容差再采集。"""
    from avelis.config import get_settings

    time.sleep(get_settings().ddl_audit_slack_seconds + 1)


def _wait(probe, seconds: int, label: str) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if probe():
            return
        time.sleep(3)
    pytest.fail(f"等待超时：{label}")


@pytest.fixture(scope="module")
def ctx(stack, admin: Api):
    import demo

    from avelis import demo_setup

    client, env, _ = stack
    running = demo.engines_running(env)
    if not (running["clickhouse"] and running["doris"]):
        pytest.fail("ClickHouse / Doris 演示库未启动：先运行 uv run scripts/demo.py up --engines")
    demo.seed_clickhouse(env)
    demo.seed_doris(env)
    demo_setup.setup(reset=False, metrics=True)
    sources = {d["kind"]: d["id"] for d in admin.get("/api/datasources")}
    for kind, port, keys, options in (
            ("clickhouse", "SHOP_CH_HTTP_PORT", ("SHOP_CH_COLLECTOR_PASSWORD", "SHOP_CH_READER_PASSWORD"),
             {"databases": ["store"]}),
            ("doris", "SHOP_DORIS_QUERY_PORT", ("SHOP_DORIS_COLLECTOR_PASSWORD", "SHOP_DORIS_READER_PASSWORD"),
             {"databases": ["store"]})):
        ds = admin.post("/api/datasources", {
            "name": f"门店库（{kind} 留底验收）", "kind": kind, "host": "127.0.0.1", "port": int(env[port]),
            "collect": {"username": "avelis_collector", "password": env[keys[0]]},
            "query": {"username": "avelis_reader", "password": env[keys[1]]}, "options": options}, expect=201)
        _collect(admin, ds["id"])
        sources[kind] = ds["id"]
    password = "Dev" + secrets.token_hex(12)
    data = {"env": env, "ds": sources, "dev_password": password}
    yield data
    # 清理临时账号（演示库下一次 seed 也会重建）
    with demo.pg_connect(env) as conn:
        conn.execute(f"DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{DEV_USER}') THEN "
                     f"EXECUTE 'REASSIGN OWNED BY {DEV_USER} TO postgres'; EXECUTE 'DROP OWNED BY {DEV_USER}'; "
                     f"EXECUTE 'DROP ROLE {DEV_USER}'; END IF; END $$")
    ch = demo.ch_connect(env)
    try:
        ch.command(f"DROP USER IF EXISTS {DEV_USER}")
    finally:
        ch.close()
    conn = demo.doris_connect(env)
    try:
        with conn.cursor() as cur:
            cur.execute(f"DROP USER IF EXISTS '{DEV_USER}'@'%'")
    finally:
        conn.close()


def test_01_postgres_attribution(ctx, admin: Api):
    import demo

    env, ds = ctx["env"], ctx["ds"]["postgres"]
    pw = ctx["dev_password"]
    with demo.pg_connect(env) as conn:
        conn.execute(f"CREATE ROLE {DEV_USER} LOGIN PASSWORD '{pw}'")
        conn.execute(f"GRANT USAGE, CREATE ON SCHEMA store TO {DEV_USER}")
        conn.execute(f"ALTER TABLE store.sales OWNER TO {DEV_USER}")
    import psycopg

    with psycopg.connect(host="127.0.0.1", port=int(env["SHOP_PG_PORT"]), user=DEV_USER, password=pw,
                         dbname="storedb", autocommit=True) as dev:
        dev.execute("ALTER TABLE store.sales ADD COLUMN channel varchar(16)")
    with demo.pg_connect(env) as conn:
        conn.execute("COMMENT ON COLUMN store.shops.city IS '门店所在城市'")
    _collect(admin, ds)
    events = _events(admin, ds)
    added = _by(events, "column_added", "store.sales", "channel")
    assert added["attribution_status"] == "matched", added["attribution"]
    assert added["actor_db_user"] == DEV_USER
    assert added["attribution"]["source"].startswith("postgres:")
    statement = added["attribution"]["statements"][0]
    assert statement["statement_type"] == "ALTER TABLE" and statement["xid"] and "summary" not in statement
    comment = _by(events, "column_comment_changed", "store.shops", "city")
    assert comment["before"] == {"comment": None} and comment["after"] == {"comment": "门店所在城市"}
    assert comment["actor_db_user"] == "postgres" and comment["attribution_status"] == "matched"
    # 字段推导变化（下游视图）不是对该对象执行的 DDL
    assert all(e["attribution_status"] == "not_applicable" for e in events if e["kind"] == "derivation_changed")

    # 审计记录缺失：关掉事件触发器后改动 → 如实标为 unmatched，不猜测
    with demo.pg_connect(env) as conn:
        conn.execute("ALTER EVENT TRIGGER avelis_ddl_end DISABLE")
        try:
            conn.execute("COMMENT ON COLUMN store.shops.shop_name IS '门店名称'")
        finally:
            conn.execute("ALTER EVENT TRIGGER avelis_ddl_end ENABLE")
    _collect(admin, ds)
    e = _by(_events(admin, ds, kind="column_comment_changed"), "column_comment_changed", "store.shops", "shop_name")
    assert e["attribution_status"] == "unmatched" and e["actor_db_user"] is None
    assert "没有" in e["attribution"]["reason"]

    # 4：采集时事件触发器仍是关闭的 → 审计视为未开启，不能拿关闭前的残留记录当确定依据
    with demo.pg_connect(env) as conn:
        conn.execute("ALTER EVENT TRIGGER avelis_ddl_end DISABLE")
        conn.execute("COMMENT ON COLUMN store.returns.return_date IS '退货日期'")
    try:
        _collect(admin, ds)
    finally:
        with demo.pg_connect(env) as conn:
            conn.execute("ALTER EVENT TRIGGER avelis_ddl_end ENABLE")
    e = _by(_events(admin, ds, kind="column_comment_changed"), "column_comment_changed", "store.returns", "return_date")
    assert e["attribution_status"] == "unavailable" and e["attribution"]["cause"] == "not_enabled"
    assert "未开启" in e["attribution"]["reason"] and e["actor_db_user"] is None

    # 审计来源不可读：撤销采集账号的读权限 → unavailable，附数据库报错；采集本身仍成功
    with demo.pg_connect(env) as conn:
        conn.execute("REVOKE SELECT ON avelis_audit.ddl_event FROM avelis_collector")
        conn.execute("COMMENT ON COLUMN store.shops.shop_id IS '门店编号'")
    try:
        _collect(admin, ds)
    finally:
        with demo.pg_connect(env) as conn:
            conn.execute("GRANT SELECT ON avelis_audit.ddl_event TO avelis_collector")
    e = _by(_events(admin, ds, kind="column_comment_changed"), "column_comment_changed", "store.shops", "shop_id")
    assert e["attribution_status"] == "unavailable" and "permission denied" in e["attribution"]["reason"]
    assert e["attribution"]["cause"] == "no_permission" and "采集账号无权读取" in e["attribution"]["reason"]

    # 视图整体重定义：定义变化与新增列都归因到执行 CREATE OR REPLACE 的用户；旧血缘边被取代并保留
    with demo.pg_connect(env) as conn:
        conn.execute("CREATE OR REPLACE VIEW store.city_daily_sales AS SELECT s.sale_date, h.city, "
                     "SUM(s.amount) AS sales_amount, COUNT(DISTINCT s.sale_id) AS sales_count, "
                     "MAX(s.amount) AS max_amount FROM store.sales s JOIN store.shops h ON h.shop_id = s.shop_id "
                     "WHERE s.status = 'paid' GROUP BY s.sale_date, h.city")
    _collect(admin, ds)
    events = _events(admin, ds, kind="definition_changed,column_added")
    for e in (_by(events, "definition_changed", "store.city_daily_sales", None),
              _by(events, "column_added", "store.city_daily_sales", "max_amount")):
        assert (e["attribution_status"], e["actor_db_user"]) == ("matched", "postgres"), e["attribution"]


def test_02_clickhouse_attribution(ctx, admin: Api):
    import clickhouse_connect
    import demo

    env, ds = ctx["env"], ctx["ds"]["clickhouse"]
    root = demo.ch_connect(env)
    try:
        root.command(f"DROP USER IF EXISTS {DEV_USER}")
        root.command(f"CREATE USER {DEV_USER} IDENTIFIED WITH sha256_password BY {{p:String}}",
                     parameters={"p": ctx["dev_password"]})
        root.command(f"GRANT ALTER ON store.* TO {DEV_USER}")
        dev = clickhouse_connect.get_client(host="127.0.0.1", port=int(env["SHOP_CH_HTTP_PORT"]), username=DEV_USER,
                                            password=ctx["dev_password"], autogenerate_session_id=False)
        try:
            dev.command("ALTER TABLE store.sales ADD COLUMN channel String")
        finally:
            dev.close()
        root.command("ALTER TABLE store.shops COMMENT COLUMN city '门店所在城市'")
        root.command("SYSTEM FLUSH LOGS")  # query_log 默认每 7.5 秒落盘；验收直接落盘，不等待
    finally:
        root.close()
    _past_boundary()
    _collect(admin, ds)
    events = _events(admin, ds)
    added = _by(events, "column_added", "store.sales", "channel")
    assert (added["attribution_status"], added["actor_db_user"]) == ("matched", DEV_USER), added["attribution"]
    assert added["attribution"]["source"] == "clickhouse:system.query_log"
    comment = _by(events, "column_comment_changed", "store.shops", "city")
    assert (comment["attribution_status"], comment["actor_db_user"]) == ("matched", "default"), comment["attribution"]


def test_03_doris_attribution(ctx, admin: Api):
    import demo

    env, ds = ctx["env"], ctx["ds"]["doris"]
    pw = ctx["dev_password"]
    conn = demo.doris_connect(env)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT UNIX_TIMESTAMP(now())")
            started = int(cur.fetchone()[0])
            cur.execute(f"DROP USER IF EXISTS '{DEV_USER}'@'%'")
            cur.execute(f"CREATE USER '{DEV_USER}'@'%' IDENTIFIED BY '{pw}'")
            cur.execute(f"GRANT ALTER_PRIV, SELECT_PRIV ON internal.store.* TO '{DEV_USER}'@'%'")
    finally:
        conn.close()
    import pymysql

    dev = pymysql.connect(host="127.0.0.1", port=int(env["SHOP_DORIS_QUERY_PORT"]), user=DEV_USER, password=pw,
                          autocommit=True, charset="utf8mb4", connect_timeout=5)
    try:
        with dev.cursor() as cur:
            cur.execute("ALTER TABLE store.sales ADD COLUMN channel VARCHAR(16) NULL")
    finally:
        dev.close()
    conn = demo.doris_connect(env)
    try:
        with conn.cursor() as cur:
            cur.execute("ALTER TABLE store.shops MODIFY COLUMN city COMMENT '门店所在城市'")
    finally:
        conn.close()

    def landed() -> bool:  # 审计批量写入，最长约 60 秒
        c = demo.doris_connect(env)
        try:
            with c.cursor() as cur:
                cur.execute("SELECT count(*) FROM __internal_schema.audit_log WHERE stmt_type = 'ALTER' "
                            "AND `time` >= FROM_UNIXTIME(%s) AND (stmt LIKE '%%ADD COLUMN channel%%' "
                            "OR stmt LIKE '%%MODIFY COLUMN city COMMENT%%')", (started,))
                return cur.fetchone()[0] >= 2
        finally:
            c.close()

    _wait(landed, 120, "Doris 审计日志落盘")

    def schema_applied() -> bool:  # 加列是异步作业（轻量 schema change 通常即时完成）
        c = demo.doris_connect(env)
        try:
            with c.cursor() as cur:
                cur.execute("SELECT count(*) FROM information_schema.columns WHERE TABLE_SCHEMA = 'store' "
                            "AND TABLE_NAME = 'sales' AND COLUMN_NAME = 'channel'")
                return cur.fetchone()[0] == 1
        finally:
            c.close()

    _wait(schema_applied, 120, "Doris 加列完成")
    _past_boundary()
    _collect(admin, ds)
    events = _events(admin, ds)
    added = _by(events, "column_added", "store.sales", "channel")
    assert (added["attribution_status"], added["actor_db_user"]) == ("matched", DEV_USER), added["attribution"]
    comment = _by(events, "column_comment_changed", "store.shops", "city")
    assert (comment["attribution_status"], comment["actor_db_user"]) == ("matched", "root"), comment["attribution"]


def test_04_engine_without_audit_is_unknown(ctx, stack, admin: Api):
    _, env, sr = stack
    ds = ctx["ds"]["starrocks"]
    conn = sr()
    try:
        with conn.cursor() as cur:
            cur.execute("ALTER TABLE shop.orders MODIFY COLUMN customer_id COMMENT '客户编号（留底验收）'")
    finally:
        conn.close()
    time.sleep(3)
    _collect(admin, ds)
    e = _by(_events(admin, ds, kind="column_comment_changed"), "column_comment_changed", "shop.orders", "customer_id")
    assert e["attribution_status"] == "unsupported" and e["actor_db_user"] is None
    assert "未接入" in e["attribution"]["reason"]
    engine = {x["kind"]: x for x in admin.get("/api/engines")}
    assert "ddl_audit" in engine["postgres"]["capabilities"] and "ddl_audit" not in engine["starrocks"]["capabilities"]


def test_05_search_and_export(ctx, admin: Api, stack):
    from avelis.db import get_engine

    client, env, _ = stack
    ds = ctx["ds"]["postgres"]
    page = admin.get("/api/change-events", params={"datasource_id": ds, "actor": DEV_USER})
    assert page["total"] == 1 and page["items"][0]["column"] == "channel"
    assert DEV_USER in page["actors"] and "default" in page["actors"] and "root" in page["actors"]
    kinds = admin.get("/api/change-events", params={"kind": "column_comment_changed", "limit": 2})
    assert kinds["total"] >= 5 and len(kinds["items"]) == 2
    paged = admin.get("/api/change-events", params={"kind": "column_comment_changed", "limit": 2, "offset": 2})
    assert {e["id"] for e in paged["items"]}.isdisjoint({e["id"] for e in kinds["items"]})
    unknown = admin.get("/api/change-events", params={"attribution_status": "unmatched,unavailable,unsupported"})
    assert unknown["total"] >= 3 and all(e["actor_db_user"] is None for e in unknown["items"])
    # 查询者不是任何业务域成员时看不到未归属的资产事件
    wang = Api(client, "wang", env["AVELIS_DEMO_USER_PASSWORD"])
    seen = wang.get("/api/change-events", params={"limit": 200})
    assert all(e["asset_id"] for e in seen["items"]) and seen["total"] < page_all(admin)
    # 用户下拉也只列可见事件里的数据库用户
    assert set(seen["actors"]) == {e["actor_db_user"] for e in seen["items"] if e["actor_db_user"]}

    params = {"kind": "column_comment_changed,column_added"}
    expected = admin.get("/api/change-events", params={**params, "limit": 200})
    resp = client.get("/api/change-events/export", params={**params, "format": "csv"}, headers=admin.headers)
    assert resp.status_code == 200, resp.text
    raw = resp.content
    data = b"".join(line for line in raw.splitlines(keepends=True) if not line.startswith(b"#"))
    assert hashlib.sha256(data).hexdigest() == resp.headers["X-Avelis-Export-Sha256"]
    rows = list(csv.DictReader(io.StringIO(data.decode("utf-8-sig"))))
    assert {r["event_id"] for r in rows} == {e["id"] for e in expected["items"]}
    assert int(resp.headers["X-Avelis-Export-Rows"]) == expected["total"] == len(rows)
    with get_engine().connect() as conn:
        db_hashes = dict(conn.execute(text("SELECT id, row_hash FROM change_event")).all())
    assert all(db_hashes[r["event_id"]] == r["row_hash"] for r in rows)
    resp = client.get("/api/change-events/export", params={**params, "format": "json"}, headers=admin.headers)
    body = resp.json()
    canonical = json.dumps(body["events"], ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    assert hashlib.sha256(canonical).hexdigest() == body["manifest"]["content_sha256"]
    assert body["manifest"]["rows"] == expected["total"] and body["manifest"]["chain_head"]["seq"] > 0
    assert {e["id"] for e in body["events"]} == {e["id"] for e in expected["items"]}
    with get_engine().connect() as conn:
        exports = conn.execute(text("SELECT detail FROM audit_event WHERE action = 'history.export'")).scalars().all()
    assert len(exports) == 2 and {e["format"] for e in exports} == {"csv", "json"}


def _rejected(sql: str, **params) -> str:
    from avelis.db import get_engine

    with get_engine().connect() as conn:
        with pytest.raises(Exception) as info:
            conn.execute(text(sql), params)
        conn.rollback()
    assert "avelis_history_append_only" in str(info.value), str(info.value)
    return str(info.value)


def test_06_append_only(ctx):
    from avelis.db import get_engine

    with get_engine().connect() as conn:
        event = conn.execute(text("SELECT id FROM change_event WHERE actor_db_user IS NOT NULL LIMIT 1")).scalar()
        superseded = conn.execute(text("SELECT id FROM asset_version WHERE superseded_at IS NOT NULL LIMIT 1")).scalar()
        edge = conn.execute(text("SELECT id FROM lineage_edge WHERE status = 'superseded' LIMIT 1")).scalar()
        resolved = conn.execute(text("SELECT id FROM change_set WHERE status = 'resolved' LIMIT 1")).scalar()
    assert event and superseded and edge and resolved
    _rejected("UPDATE change_event SET actor_db_user = 'mallory' WHERE id = :id", id=event)
    _rejected("DELETE FROM change_event WHERE id = :id", id=event)
    _rejected("DELETE FROM audit_event")
    _rejected("TRUNCATE column_version CASCADE")
    _rejected("UPDATE definition SET raw_sql = 'SELECT 1'")
    _rejected("UPDATE metric_version_transition SET reason = 'x'")
    # 状态列只能从空改为非空一次；已取代的血缘边完全冻结
    _rejected("UPDATE asset_version SET superseded_at = superseded_at + interval '1 day' WHERE id = :id", id=superseded)
    _rejected("UPDATE asset_version SET superseded_at = NULL WHERE id = :id", id=superseded)
    _rejected("UPDATE asset_version SET name = 'renamed' WHERE id = :id", id=superseded)
    _rejected("UPDATE lineage_edge SET status = 'active', superseded_at = NULL WHERE id = :id", id=edge)
    _rejected("UPDATE lineage_edge SET expression = 'x' WHERE id = :id", id=edge)
    _rejected("UPDATE change_set SET status = 'open' WHERE id = :id", id=resolved)
    _rejected("UPDATE change_event SET chain_seq = chain_seq + 100000 WHERE id = :id", id=event)
    # 合法的状态变化留下标记（标记本身也在链上）
    with get_engine().connect() as conn:
        marks = conn.execute(text("SELECT count(*) FROM history_mark WHERE table_name = 'asset_version' "
                                  "AND column_name = 'superseded_at'")).scalar()
    assert marks >= 1


def _replica(sql: str, **params) -> None:
    """模拟拥有超级权限的人绕过触发器直接改库。"""
    from avelis.db import get_engine

    with get_engine().begin() as conn:
        conn.execute(text("SET LOCAL session_replication_role = replica"))
        conn.execute(text(sql), params)


def test_07_tamper_detected(ctx, admin: Api):
    from avelis.db import get_engine

    assert admin.get("/api/history/verify")["ok"] is True
    with get_engine().connect() as conn:
        event_id, seq, actor = conn.execute(text(
            "SELECT id, chain_seq, actor_db_user FROM change_event WHERE actor_db_user = :u"), {"u": DEV_USER}).first()
        version_id, version_seq, superseded_at = conn.execute(text(
            "SELECT id, chain_seq, superseded_at FROM asset_version WHERE superseded_at IS NOT NULL "
            "ORDER BY chain_seq LIMIT 1")).first()
        audit_row = conn.execute(text("SELECT * FROM audit_event ORDER BY chain_seq LIMIT 1 OFFSET 3")
                                 ).mappings().first()

    # 1. 改内容：actor 被改成别人
    _replica("UPDATE change_event SET actor_db_user = 'mallory' WHERE id = :id", id=event_id)
    report = admin.get("/api/history/verify")
    assert report["ok"] is False
    assert report["first_break"] == {**report["first_break"], "kind": "content_mismatch", "seq": seq,
                                     "table": "change_event", "row_id": event_id}
    _replica("UPDATE change_event SET actor_db_user = :a WHERE id = :id", a=actor, id=event_id)
    assert admin.get("/api/history/verify")["ok"] is True

    # 2. 改状态列（绕过触发器，不留标记）
    _replica("UPDATE asset_version SET superseded_at = superseded_at - interval '30 day' WHERE id = :id", id=version_id)
    report = admin.get("/api/history/verify")
    assert not report["ok"] and {"kind": "mark_mismatch", "seq": version_seq, "table": "asset_version"}.items() \
        <= next(i for i in report["issues"] if i["row_id"] == version_id).items()
    _replica("UPDATE asset_version SET superseded_at = :t WHERE id = :id", t=superseded_at, id=version_id)
    assert admin.get("/api/history/verify")["ok"] is True

    # 3. 删一行：序号空洞没有清理锚点登记；补回原行后恢复
    _replica("DELETE FROM audit_event WHERE id = :id", id=audit_row["id"])
    report = admin.get("/api/history/verify")
    gap = report["first_break"]
    assert gap["kind"] == "gap_unregistered" and gap["seq"] == audit_row["chain_seq"] + 1
    assert str(audit_row["chain_seq"]) in gap["detail"]

    # 链不完整时拒绝清理，并写审计
    from avelis.history import retention

    with pytest.raises(retention.PurgeRefused) as refused:
        retention.purge(retention_days=1, now=_future())
    assert refused.value.report["first_break"]["kind"] == "gap_unregistered"

    cols = list(audit_row.keys())
    values = ", ".join(f"CAST(:{c} AS jsonb)" if c == "detail" else f":{c}" for c in cols)
    _replica(f"INSERT INTO audit_event ({', '.join(cols)}) VALUES ({values})",
             **{k: (json.dumps(v, ensure_ascii=False) if k == "detail" else v) for k, v in audit_row.items()})
    assert admin.get("/api/history/verify")["ok"] is True


def _future():
    from avelis.db import utcnow

    return utcnow() + timedelta(days=2)


def test_08_retention(ctx, admin: Api):
    from avelis.db import get_engine
    from avelis.history import retention

    with get_engine().connect() as conn:
        current = set(conn.execute(text("SELECT current_version_id FROM asset WHERE current_version_id IS NOT NULL"))
                      .scalars())
        referenced = set(conn.execute(text(
            "SELECT e.value->>'version_id' FROM metric_version mv, jsonb_each(mv.asset_versions) e "
            "WHERE mv.status = 'published'")).scalars())
        open_sets = set(conn.execute(text("SELECT id FROM change_set WHERE status = 'open'")).scalars())
        before = {t: conn.execute(text(f"SELECT count(*) FROM {t}")).scalar()
                  for t in ("asset_version", "lineage_edge", "change_set", "audit_event")}
    assert referenced, "演示口径应引用资产版本"
    dry = retention.purge(retention_days=1, now=_future(), dry_run=True)
    assert dry["status"] == "dry_run" and dry["counts"].get("asset_version")
    # 保留期未到：什么都不删
    assert retention.purge(retention_days=1095)["status"] == "nothing"

    result = retention.purge(retention_days=1, now=_future())
    assert result["status"] == "purged" and result["anchor_id"] and result["gaps"]
    counts = result["counts"]
    assert counts["asset_version"] > 0 and counts["lineage_edge"] > 0 and counts["audit_event"] > 0
    with get_engine().connect() as conn:
        left = set(conn.execute(text("SELECT id FROM asset_version")).scalars())
        assert current <= left and referenced <= left
        assert open_sets <= set(conn.execute(text("SELECT id FROM change_set")).scalars())
        assert conn.execute(text("SELECT count(*) FROM asset_version")).scalar() == \
            before["asset_version"] - counts["asset_version"]
        assert conn.execute(text("SELECT count(*) FROM lineage_edge WHERE status = 'superseded'")).scalar() == 0
        purge_audit = conn.execute(text("SELECT detail FROM audit_event WHERE action = 'history.purge' "
                                        "AND object_id = :a"), {"a": result["anchor_id"]}).scalar()
        assert purge_audit["counts"] == counts
    report = admin.get("/api/history/verify")
    assert report["ok"] is True and report["anchors"] == 1, report["first_break"]
    # 清理后再篡改一行，仍能指出位置
    with get_engine().connect() as conn:
        row_id, seq, data_type = conn.execute(text(
            "SELECT id, chain_seq, data_type FROM column_version ORDER BY chain_seq LIMIT 1")).first()
    _replica("UPDATE column_version SET data_type = 'text' WHERE id = :id", id=row_id)
    report = admin.get("/api/history/verify")
    assert report["first_break"] == {**report["first_break"], "seq": seq, "kind": "content_mismatch",
                                     "table": "column_version", "row_id": row_id}
    _replica("UPDATE column_version SET data_type = :t WHERE id = :id", t=data_type, id=row_id)
    assert admin.get("/api/history/verify")["ok"] is True
    status = admin.get("/api/history/status")
    assert status["retention_days"] == 1095 and status["anchors"][0]["id"] == result["anchor_id"]
