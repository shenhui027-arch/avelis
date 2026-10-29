"""变更留底（ADR 0008）的纯逻辑：DDL 词法、摘要脱敏、归因规则（不猜测）、哈希链核验与清理锚点空洞、CSV 防公式。
每条复现 PR #9 审计中的一个场景（# 编号对应审计报告）。"""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

from avelis.collect.attribution import Window, attribute
from avelis.connectors.ddl import (
    AuditUnavailable,
    DdlEvent,
    is_batch,
    mentions_column,
    objects_in,
    redefines,
    summarize,
)
from avelis.engines import get_engine
from avelis.history import integrity
from avelis.history.records import _cell
from avelis.history.retention import _new_gaps

T0 = datetime(2026, 10, 30, 8, 0, 0)
# 上次快照 T0−10 分钟读取 2 秒；本次快照 T0+1 分钟读取 2 秒
WINDOW = Window(T0 - timedelta(minutes=10), T0 - timedelta(minutes=10) + timedelta(seconds=2),
                T0 + timedelta(minutes=1), T0 + timedelta(minutes=1, seconds=2))


def test_objects_in_ddl_statements():
    assert objects_in("ALTER TABLE store.sales ADD COLUMN channel VARCHAR(16)") == [("store", "sales")]
    assert objects_in("ALTER TABLE `store`.`returns` RENAME refunds") == [("store", "returns"), ("store", "refunds")]
    assert objects_in("CREATE OR REPLACE VIEW store.v AS SELECT 1") == [("store", "v")]
    assert objects_in("DROP TABLE IF EXISTS store.refunds FORCE") == [("store", "refunds")]
    assert objects_in("RENAME TABLE store.a TO store.b") == [("store", "a"), ("store", "b")]
    assert objects_in("ALTER TABLE sales MODIFY COLUMN city COMMENT 'x'", "store") == [("store", "sales")]
    assert objects_in("ALTER TABLE sales ADD COLUMN c INT") == []  # 没有库名也没有当前库：不猜
    assert objects_in("ALTER TABLE store.a ADD COLUMN x INT; ALTER TABLE private.b ADD COLUMN y INT") == []  # 多语句
    assert objects_in("CREATE USER 'u'@'%' IDENTIFIED BY 'p'") == []


def test_summary_is_dropped_for_batches_and_secrets():
    # 1：同一请求里另一条语句的口令（美元引用、E'' 转义、'' 转义）与不可见对象都不能进入摘要
    batch = "ALTER ROLE app PASSWORD $$s3cr3t$$; ALTER TABLE store.visible ADD COLUMN c text;"
    assert is_batch(batch) and summarize(batch) is None
    other_object = "ALTER TABLE store.visible ADD COLUMN c text; ALTER TABLE private.secret ADD COLUMN d text"
    assert summarize(other_object) is None
    for secret in ("ALTER ROLE app PASSWORD E'a\\'b'", "ALTER ROLE app PASSWORD 'a''sensitive'",
                   "CREATE USER bob IDENTIFIED BY 'hunter2'", "SELECT $tag$x$tag$"):
        assert summarize(secret) is None, secret
    # 字符串里的分号不算多语句；单条普通 DDL 保留摘要（压缩空白、截断）
    single = "COMMENT ON COLUMN store.shops.city IS '城市; 含分号'  ;\n"
    assert not is_batch(single) and summarize(single) == "COMMENT ON COLUMN store.shops.city IS '城市; 含分号' ;"
    assert len(summarize("ALTER TABLE t ADD COLUMN " + "x" * 1000)) == 300


def test_column_mentions_ignore_strings_comments_and_object_names():
    assert mentions_column("ALTER TABLE store.sales ADD COLUMN channel text", "channel", "sales", "lower")
    assert not mentions_column("ALTER TABLE store.sales ADD COLUMN channel_no text", "channel", "sales", "lower")
    # 7：注释值、字符串、注释里的单词不是列引用
    assert not mentions_column("COMMENT ON COLUMN store.sales.notes IS 'amount'", "amount", "sales", "lower")
    assert not mentions_column("ALTER TABLE store.sales ADD COLUMN x int -- amount", "amount", "sales", "lower")
    assert mentions_column("COMMENT ON COLUMN store.sales.amount IS 'x'", "amount", "sales", "lower")
    # 列名与表名相同：对象自身的限定名不算，无法确定时不当作点名
    assert not mentions_column("ALTER TABLE store.sales OWNER TO bob", "sales", "sales", "lower")
    # 标识符大小写：PostgreSQL 不带引号折小写，带引号精确
    assert mentions_column('ALTER TABLE store.t ADD COLUMN "Amount" int', "Amount", "t", "lower")
    assert not mentions_column("ALTER TABLE store.t ADD COLUMN Amount int", "Amount", "t", "lower")
    assert mentions_column("ALTER TABLE store.t ADD COLUMN AMOUNT int", "amount", "t", "lower")
    assert redefines("CREATE OR REPLACE VIEW v AS SELECT *") and redefines("ALTER VIEW v AS SELECT 1")
    assert not redefines("ALTER TABLE t COMMENT COLUMN city 'x'")


class _Session:
    def __init__(self, assets):
        self.assets = assets

    def get(self, _model, key):
        return self.assets.get(key)


class _Connector:
    ddl_audit_source = "test:audit"
    ddl_identifier_fold = "lower"

    def __init__(self, events=None, error=None):
        self.events, self.error, self.calls = events or [], error, []

    def ddl_events(self, since, until, cursors=None):
        self.calls.append((since, until, cursors))
        if self.error:
            raise self.error
        return [e for e in self.events if since <= e.at <= until]


def _asset(db="store", name="sales"):
    return SimpleNamespace(db_name=db, name=name)


def _event(kind, column=None, asset_id="a1", **extra):
    return {"kind": kind, "asset_id": asset_id, "column_name": column, "before": {}, "after": {}, **extra}


def _ddl(user, stmt, at=T0, objects=(("store", "sales"),), certain=None):
    return DdlEvent(at=at, db_user=user, objects=list(objects), statement_type="ALTER", statement=stmt,
                    certain=certain)


def _attribute(events, connector, window=WINDOW, engine="postgres", assets=None):
    session = _Session(assets or {"a1": _asset(), "a2": _asset(name="shops")})
    return attribute(session, get_engine(engine), connector, events, window)


def test_attribution_matched_ambiguous_unmatched():
    conn = _Connector([_ddl("alice", "ALTER TABLE store.sales ADD COLUMN channel text"),
                       _ddl("bob", "ALTER TABLE store.sales ALTER COLUMN amount TYPE numeric(20,2)"),
                       _ddl("carol", "ALTER TABLE store.sales ALTER COLUMN amount SET NOT NULL")])
    added, typed, other, table = (_event("column_added", "channel"), _event("column_type_changed", "amount"),
                                  _event("column_comment_changed", "status"), _event("comment_changed"))
    _attribute([added, typed, other, table], conn)
    assert (added["attribution_status"], added["actor_db_user"]) == ("matched", "alice")
    assert added["attribution"]["statements"][0]["certain"] is True
    assert typed["attribution_status"] == "ambiguous" and typed["actor_db_user"] is None
    assert typed["attribution"]["candidates"] == ["bob", "carol"]
    assert other["attribution_status"] == "unmatched" and "status" in other["attribution"]["reason"]
    assert table["attribution_status"] == "ambiguous"


def test_attribution_union_of_column_and_redefinition_candidates():
    # 6：alice 用 SELECT * 重建视图带出新列 x，postgres 随后给 x 写注释 → 两人都是候选，不能只归 postgres
    conn = _Connector([_ddl("alice", "CREATE OR REPLACE VIEW store.v AS SELECT * FROM store.base",
                            objects=[("store", "v")]),
                       _ddl("postgres", "COMMENT ON COLUMN store.v.x IS 'x'", objects=[("store", "v")])])
    e = _event("column_added", "x", asset_id="v")
    _attribute([e], conn, assets={"v": _asset(name="v")})
    assert e["attribution_status"] == "ambiguous" and e["attribution"]["candidates"] == ["alice", "postgres"]
    assert e["attribution"]["redefinitions"] == 1


def test_attribution_comment_text_is_not_column_evidence():
    # 7：bob 的注释内容是 'amount'，不算点名 amount 列
    conn = _Connector([_ddl("alice", "ALTER TABLE store.sales ALTER COLUMN amount TYPE bigint"),
                       _ddl("bob", "COMMENT ON COLUMN store.sales.notes IS 'amount'")])
    e = _event("column_type_changed", "amount")
    _attribute([e], conn)
    assert (e["attribution_status"], e["actor_db_user"]) == ("matched", "alice")


def test_attribution_matches_object_names_exactly():
    # 8：PostgreSQL 中 store."Orders" 与 store.orders 是两个对象
    conn = _Connector([_ddl("alice", 'ALTER TABLE store."Orders" ADD COLUMN x int', objects=[("store", "Orders")]),
                       _ddl("bob", "ALTER TABLE store.orders ADD COLUMN x int", objects=[("store", "orders")])])
    upper, lower = _event("column_added", "x", asset_id="u"), _event("column_added", "x", asset_id="l")
    _attribute([upper, lower], conn, assets={"u": _asset(name="Orders"), "l": _asset(name="orders")})
    assert upper["actor_db_user"] == "alice" and lower["actor_db_user"] == "bob"


def test_attribution_redefinition_counts_for_columns_and_rename_matches_old_name():
    conn = _Connector([_ddl("dave", "CREATE OR REPLACE VIEW store.v AS SELECT *", objects=[("store", "v")]),
                       _ddl("erin", "ALTER TABLE store.old RENAME TO new", objects=[("store", "old")])])
    col = _event("column_added", "x", asset_id="v")
    renamed = _event("renamed", asset_id="n", before={"name": "pg.store.old"}, after={"name": "pg.store.new"})
    _attribute([col, renamed], conn, assets={"v": _asset(name="v"), "n": _asset(name="new")})
    assert col["actor_db_user"] == "dave"
    assert renamed["actor_db_user"] == "erin"


def test_attribution_boundary_candidates_are_never_certain():
    # 5：语句时间落在上次快照读取期间或之前（可能跨采集提交），只算边界候选，不能确定归因
    stmt = "COMMENT ON COLUMN store.sales.amount IS 'x'"
    boundary = WINDOW.previous_done - timedelta(seconds=1)
    e = [_event("column_comment_changed", "amount")]
    conn = _Connector([_ddl("bob", stmt, at=boundary)])
    _attribute(e, conn)
    assert e[0]["attribution_status"] == "ambiguous" and "边界" in e[0]["attribution"]["reason"]
    # 查询范围覆盖上次快照开始 − 容差 − 生效延迟，到本次快照结束 + 容差
    since, until, _ = conn.calls[0]
    assert since < WINDOW.previous_start and until > WINDOW.current_done
    # 边界候选与窗口内候选来自不同用户：多人，不确定
    e = [_event("column_comment_changed", "amount")]
    _attribute(e, _Connector([_ddl("bob", stmt, at=boundary), _ddl("alice", stmt)]))
    assert e[0]["attribution_status"] == "ambiguous" and e[0]["attribution"]["candidates"] == ["alice", "bob"]
    # 连接器按事务可见性确定的（PostgreSQL 记了两次快照）以连接器为准
    e = [_event("column_comment_changed", "amount")]
    _attribute(e, _Connector([_ddl("bob", stmt, at=T0 - timedelta(hours=1), certain=True)]),
               window=Window(T0 - timedelta(hours=2), T0 - timedelta(hours=2), T0, T0, {"pg_snapshot": "1:2:"},
                             {"pg_snapshot": "3:4:"}))
    assert (e[0]["attribution_status"], e[0]["actor_db_user"]) == ("matched", "bob")


def test_async_effect_lag_only_widens_the_boundary_for_lagging_statements():
    # Doris 的 ALTER 可能异步生效：上次快照前 lag 秒内的 ALTER 是边界候选；同期的 CREATE 已在上次快照里，不参与
    before = WINDOW.previous_start - timedelta(seconds=100)
    stmt = "ALTER TABLE store.sales MODIFY COLUMN amount BIGINT"
    conn = _Connector([_ddl("root", "CREATE TABLE store.sales (amount INT)", at=before),
                       DdlEvent(before, "bob", [("store", "sales")], "ALTER", stmt, may_lag=True),
                       _ddl("alice", stmt)])
    conn.ddl_effect_lag_seconds = 300
    e = [_event("column_type_changed", "amount")]
    _attribute(e, conn)
    assert e[0]["attribution_status"] == "ambiguous" and e[0]["attribution"]["candidates"] == ["alice", "bob"]
    conn.events = conn.events[:1] + conn.events[2:]  # 没有可能晚生效的语句：root 的 CREATE 不参与
    e = [_event("column_type_changed", "amount")]
    _attribute(e, conn)
    assert (e[0]["attribution_status"], e[0]["actor_db_user"]) == ("matched", "alice")


def test_attribution_unknown_reasons():
    events = [_event("column_added", "c"), _event("derivation_changed", "c")]
    _attribute(events, _Connector(error=AuditUnavailable("permission denied for table ddl_event", "no_permission")))
    assert events[0]["attribution_status"] == "unavailable"
    assert events[0]["attribution"]["cause"] == "no_permission" and "无权读取" in events[0]["attribution"]["reason"]
    assert "permission denied" in events[0]["attribution"]["reason"]
    assert events[1]["attribution_status"] == "not_applicable"

    for cause, label in (("not_enabled", "未开启"), ("incompatible", "结构"), ("error", "读取 DDL 审计失败")):
        events = [_event("column_added", "c")]
        _attribute(events, _Connector(error=AuditUnavailable("x", cause)))
        assert events[0]["attribution"]["cause"] == cause and label in events[0]["attribution"]["reason"]
    # 18：缺列不是“未开启”；未知原因归为 error
    assert AuditUnavailable('column "statement" does not exist', "incompatible").cause == "incompatible"
    assert AuditUnavailable("x", "whatever").cause == "error"

    events = [_event("column_added", "c")]
    _attribute(events, _Connector(error=ConnectionError("refused")))
    assert events[0]["attribution"]["cause"] == "error" and "ConnectionError" in events[0]["attribution"]["reason"]

    events = [_event("column_added", "c")]
    _attribute(events, _Connector(), engine="starrocks")
    assert events[0]["attribution_status"] == "unsupported" and events[0]["actor_db_user"] is None

    events = [_event("added")]
    _attribute(events, _Connector([_ddl("x", "CREATE TABLE store.sales (a int)")]), window=None)
    assert events[0]["attribution_status"] == "not_applicable"

    events = [_event("column_added", "channel")]
    _attribute(events, _Connector([_ddl("alice", "ALTER TABLE store.sales ADD COLUMN channel text",
                                        at=T0 - timedelta(hours=1))]))
    assert events[0]["attribution_status"] == "unmatched"


def test_persisted_statements_hold_no_batch_text():
    # 1：批处理记录可以作为候选（点名了该列），但落库的 summary 为空
    batch = "ALTER ROLE app PASSWORD $$s3cr3t$$; ALTER TABLE store.sales ADD COLUMN channel text"
    e = [_event("column_added", "channel")]
    _attribute(e, _Connector([_ddl("alice", batch)]))
    assert e[0]["actor_db_user"] == "alice"
    assert e[0]["attribution"]["statements"][0]["summary"] is None
    assert "s3cr3t" not in repr(e[0]["attribution"])


def _chain(n, table="audit_event"):
    rows, prev = {}, integrity.GENESIS
    for seq in range(1, n + 1):
        digest = f"d{seq}"
        h = integrity.chain_hash(prev, seq, table, digest)
        rows[seq] = integrity.ChainRow(seq, table, f"r{seq}", prev, h, digest)
        prev = h
    return integrity.Chain(rows=rows, issues=[], counts={table: n}, head={"seq": n, "hash": prev})


def test_chain_check_detects_content_link_and_gaps():
    chain = _chain(6)
    assert integrity.check(chain, {}) == []
    chain.rows[3].digest = "tampered"
    assert [(i["kind"], i["seq"]) for i in integrity.check(chain, {})] == [("content_mismatch", 3)]
    chain = _chain(6)
    del chain.rows[4]
    assert [(i["kind"], i["seq"]) for i in integrity.check(chain, {})] == [("gap_unregistered", 5)]
    chain = _chain(6)
    chain.head = {"seq": 9, "hash": "x"}
    assert integrity.check(chain, {})[0]["kind"] == "head_mismatch"


def test_empty_chain_requires_genesis_head():
    # 11：链上记录全被删除、链头仍在原位 → 报告；全新的空链合法
    chain = _chain(3)
    emptied = integrity.Chain(rows={}, issues=[], counts={}, head=chain.head)
    assert integrity.check(emptied, {})[0]["kind"] == "head_mismatch"
    fresh = integrity.Chain(rows={}, issues=[], counts={}, head={"seq": 0, "hash": integrity.GENESIS})
    assert integrity.check(fresh, {}) == []


def test_purge_gaps_keep_remaining_chain_verifiable():
    chain = _chain(10)
    first = {2, 3, 4, 8}
    gaps = _new_gaps(chain, first, {})
    assert [(g["from_seq"], g["to_seq"]) for g in gaps] == [(2, 4), (8, 8)]
    registered = {(g["from_seq"], g["to_seq"]): {**g, "anchor_id": "a1"} for g in gaps}
    after = integrity.Chain({s: r for s, r in chain.rows.items() if s not in first}, [], {}, chain.head)
    assert integrity.check(after, registered) == []
    second = {1, 5, 7}
    gaps2 = _new_gaps(after, second, registered)
    assert [(g["from_seq"], g["to_seq"]) for g in gaps2] == [(1, 5), (7, 8)]
    assert gaps2[0]["before_hash"] == integrity.GENESIS
    registered.update({(g["from_seq"], g["to_seq"]): {**g, "anchor_id": "a2"} for g in gaps2})
    final = integrity.Chain({s: r for s, r in after.rows.items() if s not in second}, [], {}, chain.head)
    assert integrity.check(final, registered) == []
    del final.rows[9]
    assert integrity.check(final, registered)[0]["kind"] == "gap_unregistered"


def test_csv_cells_cannot_become_formulas():
    # 13：合法的带引号列名 "=1+1"、以 + - @ 或控制字符开头的文本都按文本输出
    for value in ("=1+1", "+cmd", "-2", "@SUM(A1)", "\tx", "\rx"):
        assert _cell(value) == "'" + value
    assert _cell("amount") == "amount" and _cell(12) == 12 and _cell(None) is None
