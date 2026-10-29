"""历史表只追加与防篡改哈希链（ADR 0008）。

数据库层（迁移 0007 安装，见 install_sql）：
- 只追加：BEFORE UPDATE / DELETE / TRUNCATE 触发器。已封链的行只允许表配置里的“存活标记”前移、“状态列”作为一组
  按规则同时改一次；状态列的每次合法变化追加一条 history_mark（也在链上）。DELETE 只在清理专用路径
  （事务内 avelis.history_purge = on）放行；TRUNCATE 一律拒绝。未封链（本事务刚插入）的行可补全业务字段，但主键冻结，
  “本事务插入”以行里的 inserted_xact（插入触发器写入的顶层事务号 pg_current_xact_id()，之后不可改）为准，
  已提交却未封链的行不能改。
  封链找不到行或链头时拒绝提交。
- 封链：提交时的约束触发器（DEFERRABLE INITIALLY DEFERRED）为本事务插入的每一行锁住链头、取下一个序号，
  row_hash = sha256(prev_hash | chain_seq | 表名 | content_digest)，
  content_digest = sha256((to_jsonb(行) − 链列 − 存活标记)::text)。只有一把链锁，只在提交阶段持有。

核验（verify）：按序号重算内容摘要与行哈希、检查前后相接；序号空洞必须由清理锚点登记且两端哈希相符；
状态列必须与最后一条标记一致；未封链的已提交行、链头不符都如实报告。不修复、不重写。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from avelis.db import utcnow

GENESIS = "0" * 64
CHAIN_COLUMNS = ("chain_seq", "prev_hash", "row_hash")
ISSUE_LIMIT = 100


@dataclass(frozen=True)
class ChainedTable:
    name: str
    order_by: str  # 迁移补算哈希时的自然时间顺序（SQL 表达式，别名 x）
    live: dict[str, str] = field(default_factory=dict)  # 存活标记：forward 只能前移；any 可改。不进哈希
    # 状态列（一组，必须同时、按规则只改一次）：{列: {"from": 旧值, "to": 新值（缺省 = 任意非空）}}
    marks: dict[str, dict[str, Any]] = field(default_factory=dict)

    @property
    def digest_excluded(self) -> list[str]:
        return [*CHAIN_COLUMNS, *self.live]

    def guard_config(self) -> str:
        return json.dumps({"live": self.live, "marks": self.marks}, separators=(",", ":"))


_VERSION_TIME = "(SELECT v.observed_at FROM asset_version v WHERE v.id = x.asset_version_id)"

TABLES: tuple[ChainedTable, ...] = (
    ChainedTable("asset_version", "x.observed_at", live={"last_seen_at": "forward", "last_seen_run_id": "any"},
                 marks={"superseded_at": {"from": None}}),
    ChainedTable("column_version", _VERSION_TIME),
    ChainedTable("definition", _VERSION_TIME),
    ChainedTable("lineage_edge", "x.created_at",
                 marks={"status": {"from": "active", "to": "superseded"}, "superseded_at": {"from": None}}),
    ChainedTable("change_set", "x.created_at",
                 marks={"status": {"from": "open", "to": "resolved"}, "resolved_at": {"from": None}}),
    ChainedTable("change_event", "x.created_at"),
    ChainedTable("audit_event", "x.at"),
    ChainedTable("metric_version_transition", "x.at"),
    ChainedTable("history_mark", "x.at"),
    ChainedTable("history_anchor", "x.at"),
)
BY_NAME = {t.name: t for t in TABLES}


def chain_hash(prev_hash: str, seq: int, table: str, digest: str) -> str:
    return hashlib.sha256(f"{prev_hash}|{seq}|{table}|{digest}".encode()).hexdigest()


def digest_sql(table: ChainedTable, alias: str = "x", override: str | None = None) -> str:
    """content_digest 的 SQL 表达式。封链、补算与核验用同一个表达式；override 为封链时状态列取值（jsonb）。"""
    excluded = "ARRAY[" + ", ".join(f"'{c}'" for c in table.digest_excluded) + "]::text[]"
    body = f"(to_jsonb({alias}) - {excluded})"
    if override:
        body = f"({body} || coalesce({override}, '{{}}'::jsonb))"
    return f"encode(sha256(convert_to({body}::text, 'UTF8')), 'hex')"


# --- 数据库对象 -------------------------------------------------------------------

FUNCTIONS_SQL = r"""
CREATE OR REPLACE FUNCTION avelis_history_insert() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  -- 链列只由提交时的封链触发器写入；插入它的顶层事务号写在行里（保存点内也是顶层事务号，64 位不回卷），之后不可改
  NEW.chain_seq := NULL; NEW.prev_hash := NULL; NEW.row_hash := NULL;
  NEW.inserted_xact := pg_current_xact_id();
  RETURN NEW;
END $$;

CREATE OR REPLACE FUNCTION avelis_history_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
  cfg jsonb := TG_ARGV[0]::jsonb;
  o jsonb;
  n jsonb;
  k text;
  rule jsonb;
  group_changed int := 0;
  group_size int := 0;
BEGIN
  IF TG_OP = 'TRUNCATE' THEN
    -- 清理只走逐行 DELETE 的专用路径；TRUNCATE 一律拒绝
    RAISE EXCEPTION 'avelis_history_append_only: % 只允许追加，不能 TRUNCATE', TG_TABLE_NAME;
  END IF;
  IF TG_OP = 'DELETE' THEN
    IF current_setting('avelis.history_purge', true) = 'on' THEN
      RETURN OLD;
    END IF;
    RAISE EXCEPTION 'avelis_history_append_only: % 只允许追加，不能 DELETE（超期历史只能由保留期清理删除）',
      TG_TABLE_NAME;
  END IF;
  o := to_jsonb(OLD);
  n := to_jsonb(NEW);
  IF NEW.inserted_xact IS DISTINCT FROM OLD.inserted_xact THEN
    RAISE EXCEPTION 'avelis_history_append_only: %.inserted_xact 不能修改（行 %）', TG_TABLE_NAME, OLD.id;
  END IF;
  IF OLD.chain_seq IS NULL THEN
    -- 尚未封链：只允许插入它的事务补全业务字段，以行里的插入事务号为准（不可伪造、不随 ON CONFLICT 变化）。
    -- 已提交却未封链的行（绕过触发器写入的）事务号对不上，不能借此改写
    IF OLD.inserted_xact IS DISTINCT FROM pg_current_xact_id() THEN
      RAISE EXCEPTION 'avelis_history_append_only: %（行 %）不是本事务插入的未封链行（已提交但未封链），不能修改',
        TG_TABLE_NAME, OLD.id;
    END IF;
    IF NEW.id IS DISTINCT FROM OLD.id THEN
      RAISE EXCEPTION 'avelis_history_append_only: %.id 不能修改', TG_TABLE_NAME;
    END IF;
    IF (o->'chain_seq', o->'prev_hash', o->'row_hash') IS DISTINCT FROM (n->'chain_seq', n->'prev_hash', n->'row_hash')
       AND coalesce(current_setting('avelis.history_sealing', true), '') <> 'on' THEN
      RAISE EXCEPTION 'avelis_history_append_only: %.chain_* 只能由封链触发器写入', TG_TABLE_NAME;
    END IF;
    RETURN NEW;
  END IF;
  -- 已封链：状态列是一组，必须按规则同时、只改一次（如 active/空 → superseded/非空）
  FOR k IN SELECT jsonb_object_keys(coalesce(cfg->'marks', '{}'::jsonb)) LOOP
    group_size := group_size + 1;
    IF (o->k) IS DISTINCT FROM (n->k) THEN
      group_changed := group_changed + 1;
    END IF;
  END LOOP;
  IF group_changed > 0 THEN
    IF group_changed <> group_size THEN
      RAISE EXCEPTION 'avelis_history_append_only: % 的状态列 % 必须同时改（行 %）', TG_TABLE_NAME,
        (SELECT string_agg(x, '、') FROM jsonb_object_keys(cfg->'marks') x), OLD.id;
    END IF;
    FOR k IN SELECT jsonb_object_keys(cfg->'marks') LOOP
      rule := cfg->'marks'->k;
      IF (o->k) IS DISTINCT FROM (rule->'from') OR jsonb_typeof(n->k) = 'null'
         OR (rule ? 'to' AND (n->k) IS DISTINCT FROM (rule->'to')) THEN
        RAISE EXCEPTION 'avelis_history_append_only: %.% 只能从 % 改为 %，且只改一次（行 %）', TG_TABLE_NAME, k,
          rule->'from', coalesce(rule->'to', '"非空"'::jsonb), OLD.id;
      END IF;
      INSERT INTO history_mark (id, table_name, row_id, column_name, old_value, new_value, at)
      VALUES (replace(gen_random_uuid()::text, '-', ''), TG_TABLE_NAME, OLD.id, k, o->k, n->k,
              now() AT TIME ZONE 'UTC');
    END LOOP;
  END IF;
  FOR k IN SELECT jsonb_object_keys(n) LOOP
    CONTINUE WHEN (o->k) IS NOT DISTINCT FROM (n->k) OR coalesce(cfg->'marks', '{}'::jsonb) ? k;
    IF cfg->'live' ? k THEN
      IF cfg->'live'->>k = 'forward' AND ((n->>k) IS NULL OR (n->>k)::timestamp < (o->>k)::timestamp) THEN
        RAISE EXCEPTION 'avelis_history_append_only: %.% 只能前移', TG_TABLE_NAME, k;
      END IF;
      CONTINUE;
    END IF;
    RAISE EXCEPTION 'avelis_history_append_only: %.% 是历史内容，不能修改（行 %）', TG_TABLE_NAME, k, OLD.id;
  END LOOP;
  RETURN NEW;
END $$;

CREATE OR REPLACE FUNCTION avelis_history_seal() RETURNS trigger LANGUAGE plpgsql SET timezone = 'UTC' AS $$
DECLARE
  excluded text[] := ARRAY(SELECT jsonb_array_elements_text(TG_ARGV[0]::jsonb));
  r jsonb;
  v_seq bigint;
  v_hash text;
  v_digest text;
  v_row_hash text;
  v_count int;
BEGIN
  EXECUTE format('SELECT to_jsonb(t) FROM %I t WHERE t.id = $1', TG_TABLE_NAME) INTO r USING NEW.id;
  -- 找不到本事务插入的行（主键被改、被删）或已有链字段：拒绝提交，不能留下未封链的历史
  IF r IS NULL THEN
    RAISE EXCEPTION 'avelis_history_seal: 封链时找不到 %（行 %），拒绝提交', TG_TABLE_NAME, NEW.id;
  END IF;
  IF jsonb_typeof(r->'chain_seq') <> 'null' THEN
    RAISE EXCEPTION 'avelis_history_seal: %（行 %）封链前已有链字段，拒绝提交', TG_TABLE_NAME, NEW.id;
  END IF;
  SELECT h.last_seq, h.last_hash INTO v_seq, v_hash FROM history_chain_head h WHERE h.id = 1 FOR UPDATE;
  IF v_seq IS NULL OR v_hash IS NULL THEN
    RAISE EXCEPTION 'avelis_history_seal: 链头 history_chain_head 不存在，拒绝提交';
  END IF;
  v_digest := encode(sha256(convert_to((r - excluded)::text, 'UTF8')), 'hex');
  v_row_hash := encode(sha256(convert_to(v_hash || '|' || (v_seq + 1)::text || '|' || TG_TABLE_NAME || '|' || v_digest,
                                         'UTF8')), 'hex');
  PERFORM set_config('avelis.history_sealing', 'on', true);
  EXECUTE format('UPDATE %I SET chain_seq = $1, prev_hash = $2, row_hash = $3 WHERE id = $4', TG_TABLE_NAME)
    USING v_seq + 1, v_hash, v_row_hash, NEW.id;
  GET DIAGNOSTICS v_count = ROW_COUNT;
  PERFORM set_config('avelis.history_sealing', 'off', true);
  IF v_count <> 1 THEN
    RAISE EXCEPTION 'avelis_history_seal: 写入 %（行 %）的链字段失败，拒绝提交', TG_TABLE_NAME, NEW.id;
  END IF;
  UPDATE history_chain_head SET last_seq = v_seq + 1, last_hash = v_row_hash WHERE id = 1;
  RETURN NULL;
END $$;
"""


def trigger_sql(table: ChainedTable) -> str:
    t = table.name
    excluded = json.dumps(table.digest_excluded)
    return f"""
DROP TRIGGER IF EXISTS avelis_chain_insert ON {t};
CREATE TRIGGER avelis_chain_insert BEFORE INSERT ON {t} FOR EACH ROW EXECUTE FUNCTION avelis_history_insert();
DROP TRIGGER IF EXISTS avelis_append_only ON {t};
CREATE TRIGGER avelis_append_only BEFORE UPDATE OR DELETE ON {t}
  FOR EACH ROW EXECUTE FUNCTION avelis_history_guard('{table.guard_config()}');
DROP TRIGGER IF EXISTS avelis_no_truncate ON {t};
CREATE TRIGGER avelis_no_truncate BEFORE TRUNCATE ON {t}
  FOR EACH STATEMENT EXECUTE FUNCTION avelis_history_guard('{{}}');
DROP TRIGGER IF EXISTS avelis_chain_seal ON {t};
CREATE CONSTRAINT TRIGGER avelis_chain_seal AFTER INSERT ON {t} DEFERRABLE INITIALLY DEFERRED
  FOR EACH ROW EXECUTE FUNCTION avelis_history_seal('{excluded}');
"""


def drop_sql(table: ChainedTable) -> str:
    return "".join(f"DROP TRIGGER IF EXISTS {name} ON {table.name};\n"
                   for name in ("avelis_chain_insert", "avelis_append_only", "avelis_no_truncate", "avelis_chain_seal"))


def run_raw(conn, sql: str) -> None:
    """按原样执行：函数体里有 %、触发器参数里有 JSON 冒号，不能经过绑定参数解析。"""
    with conn.connection.cursor() as cursor:
        cursor.execute(sql)


def install(conn) -> None:
    run_raw(conn, FUNCTIONS_SQL + "".join(trigger_sql(t) for t in TABLES))


def backfill(conn) -> int:
    """为已有数据按自然时间顺序补算哈希（迁移在安装触发器之前调用）。返回补算行数。"""
    conn.execute(text("SET LOCAL TIME ZONE 'UTC'"))
    parts = [f"SELECT '{t.name}' AS tbl, {i} AS rank, x.id, {t.order_by} AS ts, {digest_sql(t)} AS digest "
             f"FROM {t.name} x" for i, t in enumerate(TABLES)]
    rows = conn.execute(text(" UNION ALL ".join(parts) + " ORDER BY ts NULLS FIRST, rank, id")).all()
    prev, seq = GENESIS, 0
    updates: dict[str, list[dict[str, Any]]] = {}
    for tbl, _, row_id, _, digest in rows:
        seq += 1
        h = chain_hash(prev, seq, tbl, digest)
        updates.setdefault(tbl, []).append({"id": row_id, "seq": seq, "prev": prev, "h": h})
        prev = h
    for tbl, items in updates.items():
        conn.execute(text(f"UPDATE {tbl} SET chain_seq = :seq, prev_hash = :prev, row_hash = :h WHERE id = :id"),
                     items)
    conn.execute(text("INSERT INTO history_chain_head (id, last_seq, last_hash) VALUES (1, :seq, :h)"),
                 {"seq": seq, "h": prev})
    return seq


# --- 核验 -------------------------------------------------------------------------

@dataclass
class ChainRow:
    seq: int
    table: str
    row_id: str
    prev_hash: str
    row_hash: str
    digest: str


@dataclass
class Chain:
    rows: dict[int, ChainRow]
    issues: list[dict[str, Any]]
    counts: dict[str, int]
    head: dict[str, Any] | None


def _row_query(table: ChainedTable) -> str:
    return f"""
SELECT x.id, x.chain_seq, x.prev_hash, x.row_hash, {digest_sql(table, override='m.sealed')} AS digest,
       coalesce(m.n, 0) AS marks, coalesce(m.cols, 0) AS mark_cols,
       NOT EXISTS (SELECT 1 FROM jsonb_each(coalesce(m.latest, '{{}}'::jsonb)) e
                   WHERE to_jsonb(x) -> e.key IS DISTINCT FROM e.value) AS marks_ok
FROM {table.name} x
LEFT JOIN (SELECT row_id, count(*) AS n, count(DISTINCT column_name) AS cols,
                  jsonb_object_agg(column_name, old_value) AS sealed, jsonb_object_agg(column_name, new_value) AS latest
           FROM history_mark WHERE table_name = '{table.name}' GROUP BY row_id) m ON m.row_id = x.id
"""


def load_chain(session: Session) -> Chain:
    """读出全部链上行并重算内容摘要（同一快照）。调用方负责事务隔离级别。"""
    session.execute(text("SET LOCAL TIME ZONE 'UTC'"))
    rows: dict[int, ChainRow] = {}
    issues: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    for table in TABLES:
        n = 0
        for row_id, seq, prev, row_hash, digest, marks, mark_cols, marks_ok in session.execute(text(_row_query(table))):
            n += 1
            if seq is None:
                issues.append({"kind": "unsealed", "seq": None, "table": table.name, "row_id": row_id,
                               "detail": "已提交但未封链（可能绕过了触发器插入）"})
                continue
            if seq in rows:
                issues.append({"kind": "duplicate_seq", "seq": seq, "table": table.name, "row_id": row_id,
                               "detail": f"序号与 {rows[seq].table}/{rows[seq].row_id} 重复"})
                continue
            rows[seq] = ChainRow(seq, table.name, row_id, prev or "", row_hash or "", digest)
            if marks != mark_cols:
                issues.append({"kind": "duplicate_marks", "seq": seq, "table": table.name, "row_id": row_id,
                               "detail": "同一状态列有多条标记"})
            elif not marks_ok:
                issues.append({"kind": "mark_mismatch", "seq": seq, "table": table.name, "row_id": row_id,
                               "detail": "状态列的当前值与最后一条标记不一致（绕过触发器改过）"})
        counts[table.name] = n
    head = session.execute(text("SELECT last_seq, last_hash FROM history_chain_head WHERE id = 1")).first()
    return Chain(rows, issues, counts, {"seq": head[0], "hash": head[1]} if head else None)


def registered_gaps(session: Session) -> dict[tuple[int, int], dict[str, Any]]:
    gaps: dict[tuple[int, int], dict[str, Any]] = {}
    for anchor_id, anchor_gaps in session.execute(text("SELECT id, gaps FROM history_anchor")):
        for g in anchor_gaps or []:
            gaps[(int(g["from_seq"]), int(g["to_seq"]))] = {**g, "anchor_id": anchor_id}
    return gaps


def check(chain: Chain, gaps: dict[tuple[int, int], dict[str, Any]]) -> list[dict[str, Any]]:
    issues = list(chain.issues)
    prev_seq, prev_hash = 0, GENESIS
    for seq in sorted(chain.rows):
        r = chain.rows[seq]
        where = {"seq": seq, "table": r.table, "row_id": r.row_id}
        if seq == prev_seq + 1:
            if r.prev_hash != prev_hash:
                issues.append({"kind": "link_broken", **where,
                               "detail": f"prev_hash 与序号 {prev_seq} 的行哈希不相接"})
        else:
            gap = gaps.get((prev_seq + 1, seq - 1))
            if gap is None:
                issues.append({"kind": "gap_unregistered", **where,
                               "detail": f"序号 {prev_seq + 1}–{seq - 1} 缺失，且没有清理锚点登记"})
            elif gap["before_hash"] != prev_hash or gap["last_hash"] != r.prev_hash:
                issues.append({"kind": "gap_mismatch", **where,
                               "detail": f"序号 {prev_seq + 1}–{seq - 1} 的清理锚点 {gap['anchor_id']} 与两侧哈希不符"})
        if chain_hash(r.prev_hash, seq, r.table, r.digest) != r.row_hash:
            issues.append({"kind": "content_mismatch", **where, "detail": "内容与封链时不一致（行被改过）"})
        prev_seq, prev_hash = seq, r.row_hash
    head = chain.head
    if head is None:
        issues.append({"kind": "head_missing", "seq": None, "table": "history_chain_head", "row_id": None,
                       "detail": "链头不存在"})
    elif not chain.rows and (head["seq"] != 0 or head["hash"] != GENESIS):
        # 空链只有一种合法状态：链头还在起点。链头已前进而行全不见了，说明链上记录被整体删除
        issues.append({"kind": "head_mismatch", "seq": head["seq"], "table": "history_chain_head", "row_id": None,
                       "detail": f"链头已到序号 {head['seq']}，但链上没有任何记录（记录被整体删除）"})
    elif chain.rows and (head["seq"] != prev_seq or head["hash"] != prev_hash):
        issues.append({"kind": "head_mismatch", "seq": head["seq"], "table": "history_chain_head", "row_id": None,
                       "detail": f"链头（{head['seq']}）与最后一行（{prev_seq}）不一致"})
    return sorted(issues, key=lambda i: (i["seq"] is None, i["seq"] or 0))


def verify(session: Session) -> dict[str, Any]:
    """核验整条链。只读；调用方应在 REPEATABLE READ 事务中调用，保证各表读到同一快照。"""
    chain = load_chain(session)
    issues = check(chain, registered_gaps(session))
    last = chain.rows[max(chain.rows)] if chain.rows else None
    return {
        "ok": not issues,
        "verified_at": utcnow().isoformat(),
        "checked_rows": len(chain.rows),
        "tables": chain.counts,
        "head": {"seq": last.seq, "hash": last.row_hash} if last else chain.head,
        "first_break": issues[0] if issues else None,
        "issue_count": len(issues),
        "issues": issues[:ISSUE_LIMIT],
        "anchors": session.execute(text("SELECT count(*) FROM history_anchor")).scalar_one(),
    }


def chain_head(session: Session) -> dict[str, Any] | None:
    row = session.execute(text("SELECT last_seq, last_hash FROM history_chain_head WHERE id = 1")).first()
    return {"seq": row[0], "hash": row[1]} if row else None
