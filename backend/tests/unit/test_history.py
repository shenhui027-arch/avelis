"""变更留底（ADR 0008）的纯逻辑：DDL 对象解析与摘要遮蔽、归因规则（不猜测）、哈希链核验与清理锚点空洞。"""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

from avelis.collect.attribution import attribute, unavailable_cause
from avelis.connectors.ddl import AuditUnavailable, DdlEvent, mentions_column, objects_in, redefines, summarize
from avelis.engines import get_engine
from avelis.history import integrity
from avelis.history.retention import _new_gaps

T0 = datetime(2026, 10, 30, 8, 0, 0)


def test_objects_in_ddl_statements():
    assert objects_in("ALTER TABLE store.sales ADD COLUMN channel VARCHAR(16)") == [("store", "sales")]
    assert objects_in("ALTER TABLE `store`.`returns` RENAME refunds") == [("store", "returns"), ("store", "refunds")]
    assert objects_in("CREATE OR REPLACE VIEW store.v AS SELECT 1") == [("store", "v")]
    assert objects_in("DROP TABLE IF EXISTS store.refunds FORCE") == [("store", "refunds")]
    assert objects_in("RENAME TABLE store.a TO store.b") == [("store", "a"), ("store", "b")]
    assert objects_in("ALTER TABLE sales MODIFY COLUMN city COMMENT 'x'", "store") == [("store", "sales")]
    assert objects_in("ALTER TABLE sales ADD COLUMN c INT") == []  # 没有库名也没有当前库：不猜
    assert objects_in("CREATE USER 'u'@'%' IDENTIFIED BY 'p'") == []
    assert objects_in("SELECT 1") == []


def test_summary_masks_secrets_and_truncates():
    s = summarize("CREATE USER  bob\n IDENTIFIED BY 'hunter2'; ALTER USER x PASSWORD 'p@ss'")
    assert "hunter2" not in s and "p@ss" not in s and "\n" not in s and "'***'" in s
    assert len(summarize("x" * 1000)) == 300


def test_column_mention_and_redefinition():
    assert mentions_column("ALTER TABLE t ADD COLUMN channel text", "channel")
    assert not mentions_column("ALTER TABLE t ADD COLUMN channel_no text", "channel")
    assert mentions_column('COMMENT ON COLUMN store.shops."City" IS \'x\'', "city")
    assert redefines("CREATE OR REPLACE VIEW v AS SELECT *")
    assert redefines("ALTER VIEW v AS SELECT 1")
    assert not redefines("ALTER TABLE t COMMENT COLUMN city 'x'")


class _Session:
    def __init__(self, assets):
        self.assets = assets

    def get(self, _model, key):
        return self.assets.get(key)


class _Connector:
    ddl_audit_source = "test:audit"

    def __init__(self, events=None, error=None):
        self.events, self.error = events or [], error

    def ddl_events(self, since, until):
        if self.error:
            raise self.error
        return [e for e in self.events if since <= e.at <= until]


def _asset(db="store", name="sales"):
    return SimpleNamespace(db_name=db, name=name)


def _event(kind, column=None, asset_id="a1", **extra):
    return {"kind": kind, "asset_id": asset_id, "column_name": column, "before": {}, "after": {}, **extra}


def _ddl(user, stmt, at=T0, objects=(("store", "sales"),)):
    return DdlEvent(at=at, db_user=user, objects=list(objects), statement_type="ALTER", statement=stmt)


def _attribute(events, connector, since=T0 - timedelta(minutes=5), engine="postgres", assets=None):
    session = _Session(assets or {"a1": _asset(), "a2": _asset(name="shops")})
    return attribute(session, get_engine(engine), connector, events, since, T0 + timedelta(minutes=1))


def test_attribution_matched_ambiguous_unmatched():
    conn = _Connector([_ddl("alice", "ALTER TABLE store.sales ADD COLUMN channel text"),
                       _ddl("bob", "ALTER TABLE store.sales ALTER COLUMN amount TYPE numeric(20,2)"),
                       _ddl("carol", "ALTER TABLE store.sales ALTER COLUMN amount SET NOT NULL")])
    added, typed, other, table = (_event("column_added", "channel"), _event("column_type_changed", "amount"),
                                  _event("column_comment_changed", "status"), _event("comment_changed"))
    _attribute([added, typed, other, table], conn)
    assert (added["attribution_status"], added["actor_db_user"]) == ("matched", "alice")
    assert typed["attribution_status"] == "ambiguous" and typed["actor_db_user"] is None
    assert typed["attribution"]["candidates"] == ["bob", "carol"]
    # 同一对象上只改了别的列：不归给他们
    assert other["attribution_status"] == "unmatched" and "status" in other["attribution"]["reason"]
    # 表级变化：三个用户都改过这张表 → 多人，不猜
    assert table["attribution_status"] == "ambiguous"


def test_attribution_redefinition_counts_for_columns_and_rename_matches_old_name():
    conn = _Connector([_ddl("dave", "CREATE OR REPLACE VIEW store.v AS SELECT *", objects=[("store", "v")]),
                       _ddl("erin", "ALTER TABLE store.old RENAME TO new", objects=[("store", "old")])])
    col = _event("column_added", "x", asset_id="v")
    renamed = _event("renamed", asset_id="n", before={"name": "pg.store.old"}, after={"name": "pg.store.new"})
    _attribute([col, renamed], conn, assets={"v": _asset(name="v"), "n": _asset(name="new")})
    assert col["actor_db_user"] == "dave"
    assert renamed["actor_db_user"] == "erin"


def test_attribution_unknown_reasons():
    events = [_event("column_added", "c"), _event("derivation_changed", "c")]
    _attribute(events, _Connector(error=AuditUnavailable("permission denied for table ddl_event")))
    assert events[0]["attribution_status"] == "unavailable"
    assert "permission denied" in events[0]["attribution"]["reason"]
    assert events[1]["attribution_status"] == "not_applicable"
    assert events[0]["attribution"]["cause"] == "no_permission" and "无权读取" in events[0]["attribution"]["reason"]
    assert unavailable_cause('relation "avelis_audit.ddl_event" does not exist')[0] == "not_enabled"
    assert unavailable_cause("Code: 60. DB::Exception: Unknown table expression identifier")[0] == "not_enabled"
    assert unavailable_cause("Code: 497. DB::Exception: avelis_collector: Not enough privileges")[0] == "no_permission"
    assert unavailable_cause("errCode = 2, detailMessage = Access denied for user")[0] == "no_permission"

    events = [_event("column_added", "c")]
    _attribute(events, _Connector(error=ConnectionError("refused")))
    assert events[0]["attribution_status"] == "unavailable" and "ConnectionError" in events[0]["attribution"]["reason"]

    events = [_event("column_added", "c")]
    _attribute(events, _Connector(), engine="starrocks")
    assert events[0]["attribution_status"] == "unsupported" and events[0]["actor_db_user"] is None

    events = [_event("added")]
    _attribute(events, _Connector([_ddl("x", "CREATE TABLE store.sales (a int)")]), since=None)
    assert events[0]["attribution_status"] == "not_applicable"

    # 窗口外的审计记录不参与
    events = [_event("column_added", "channel")]
    _attribute(events, _Connector([_ddl("alice", "ALTER TABLE store.sales ADD COLUMN channel text",
                                        at=T0 - timedelta(hours=1))]))
    assert events[0]["attribution_status"] == "unmatched"


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


def test_purge_gaps_keep_remaining_chain_verifiable():
    chain = _chain(10)
    first = {2, 3, 4, 8}
    gaps = _new_gaps(chain, first, {})
    assert [(g["from_seq"], g["to_seq"]) for g in gaps] == [(2, 4), (8, 8)]
    registered = {(g["from_seq"], g["to_seq"]): {**g, "anchor_id": "a1"} for g in gaps}
    after = integrity.Chain({s: r for s, r in chain.rows.items() if s not in first}, [], {}, chain.head)
    assert integrity.check(after, registered) == []
    # 第二次清理把相邻的行也删掉：空洞合并成更大的区间，并能从链首开始
    second = {1, 5, 7}
    gaps2 = _new_gaps(after, second, registered)
    assert [(g["from_seq"], g["to_seq"]) for g in gaps2] == [(1, 5), (7, 8)]
    assert gaps2[0]["before_hash"] == integrity.GENESIS
    registered.update({(g["from_seq"], g["to_seq"]): {**g, "anchor_id": "a2"} for g in gaps2})
    final = integrity.Chain({s: r for s, r in after.rows.items() if s not in second}, [], {}, chain.head)
    assert integrity.check(final, registered) == []
    # 未经清理路径再删一行：仍被发现
    del final.rows[9]
    assert integrity.check(final, registered)[0]["kind"] == "gap_unregistered"
