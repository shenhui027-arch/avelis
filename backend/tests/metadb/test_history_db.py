"""留底在真实 PostgreSQL 元数据库上的行为（ADR 0008；# 编号对应 PR #9 审计报告）。只用开发者自己的 *_dev 库。"""

from __future__ import annotations

import csv
import dataclasses
import hashlib
import io
import threading
import time
from datetime import timedelta

import pytest
from sqlalchemy import select, text

from avelis import audit
from avelis.db import new_id, session_factory, session_scope, utcnow
from avelis.history import integrity, records, retention
from avelis.models import (
    Asset,
    AssetVersion,
    ChangeEvent,
    ChangeSet,
    CollectionRun,
    Datasource,
    Domain,
    Metric,
    MetricVersion,
    User,
    UserRole,
)

pytestmark = pytest.mark.db


def _rejected(engine, sql: str, match: str = "avelis_history", **params) -> str:
    with engine.connect() as conn:
        with pytest.raises(Exception) as info:
            conn.execute(text(sql), params)
            conn.commit()
        conn.rollback()
    assert match in str(info.value), str(info.value)
    return str(info.value)


def _replica(engine, sql: str, **params) -> None:
    with engine.begin() as conn:
        conn.execute(text("SET LOCAL session_replication_role = replica"))
        conn.execute(text(sql), params)


def _verify() -> dict:
    s = session_factory()()
    try:
        s.connection(execution_options={"isolation_level": "REPEATABLE READ"})
        return integrity.verify(s)
    finally:
        s.rollback()
        s.close()


@pytest.fixture(scope="module")
def world(metadb):
    """一个数据源、一张表的三个版本（AV0 无人引用、AV1 被已归档的组成口径引用、AV2 当前）与口径：
    基础口径 M1（已归档，绑定 AV1）、M2（已发布，绑定 AV2），派生口径 D（已发布，components 固定引用 M1）。"""
    now = utcnow()
    with session_scope() as s:
        admin = User(username="admin", display_name="管理员", password_hash="x")
        s.add(admin)
        s.flush()
        s.add(UserRole(user_id=admin.id, role="admin"))
        domain = Domain(code="trade", name="交易", owner_id=admin.id)
        ds = Datasource(name="测试源", kind="fake", host="fake", port=0, is_test_adapter=True)
        s.add_all([domain, ds])
        s.flush()
        run = CollectionRun(datasource_id=ds.id, trigger="manual", status="succeeded", finished_at=now)
        s.add(run)
        s.flush()
        asset = Asset(datasource_id=ds.id, kind="table", catalog_name="c", db_name="store", name="sales",
                      status="active", domain_id=domain.id)
        s.add(asset)
        s.flush()
        versions = []
        for no in range(3):
            v = AssetVersion(asset_id=asset.id, version_no=no + 1, observed_run_id=run.id, last_seen_run_id=run.id,
                             kind="table", catalog_name="c", db_name="store", name="sales", content_hash=f"h{no}",
                             superseded_at=now if no < 2 else None)
            s.add(v)
            s.flush()
            versions.append(v)
        asset.current_version_id = versions[2].id
        base = Metric(code="sales", name="销售额", domain_id=domain.id, owner_id=admin.id, created_by=admin.id)
        derived = Metric(code="ratio", name="比率", domain_id=domain.id, owner_id=admin.id, created_by=admin.id)
        s.add_all([base, derived])
        s.flush()

        def mv(metric, no, status, version, **kw):
            row = MetricVersion(metric_id=metric.id, version_no=no, status=status, description="d", expression="x",
                                time_column="t", timezone="UTC", created_by=admin.id,
                                asset_versions={asset.id: {"version_id": version.id}} if version else {}, **kw)
            s.add(row)
            s.flush()
            return row

        m1 = mv(base, 1, "archived", versions[1])
        mv(base, 2, "published", versions[2])
        mv(derived, 1, "published", None, kind="derived", components=[{"metric_code": "sales", "version_id": m1.id}])
        cs = ChangeSet(run_id=run.id, datasource_id=ds.id, status="resolved", window_end=now, resolved_at=now)
        s.add(cs)
        s.flush()
        for column in ("=1+1", "amount"):
            s.add(ChangeEvent(change_set_id=cs.id, asset_id=asset.id, kind="column_added", column_name=column,
                              after={"data_type": "int"}, attribution_status="matched", actor_db_user="alice"))
        return {"admin_id": admin.id, "versions": [v.id for v in versions], "asset": asset.id, "cs": cs.id,
                "run": run.id, "ds": ds.id}


def test_rows_are_sealed_and_chain_verifies(world):
    report = _verify()
    assert report["ok"] is True and report["checked_rows"] > 0, report["issues"]


def test_unsealed_rows_keep_primary_key_and_committed_unsealed_rows_are_frozen(metadb, world):
    # 2：同一事务里先插入再改主键 → 拒绝（封链会按插入时的主键找行）
    with metadb.connect() as conn:
        conn.execute(text("INSERT INTO audit_event (id, at, actor_name, action, object_type, detail) "
                          "VALUES ('a0000000000000000000000000000001', now(), 'x', 't', 't', '{}')"))
        with pytest.raises(Exception, match="id 不能修改"):
            conn.execute(text("UPDATE audit_event SET id = 'b0000000000000000000000000000001' "
                              "WHERE id = 'a0000000000000000000000000000001'"))
        conn.rollback()
    # 绕过触发器提交了一条未封链的行：之后普通 UPDATE 不能借“未封链”分支改写它
    _replica(metadb, "INSERT INTO audit_event (id, at, actor_name, action, object_type, detail) "
                     "VALUES ('c0000000000000000000000000000001', now(), 'x', 't', 't', '{}')")
    _rejected(metadb, "UPDATE audit_event SET action = 'rewritten' WHERE id = 'c0000000000000000000000000000001'",
              match="已提交但未封链")
    assert _verify()["first_break"]["kind"] == "unsealed"
    _replica(metadb, "DELETE FROM audit_event WHERE id = 'c0000000000000000000000000000001'")
    # 封链时找不到本事务插入的行（这里借清理开关删掉）→ 拒绝提交
    with metadb.connect() as conn:
        conn.execute(text("INSERT INTO audit_event (id, at, actor_name, action, object_type, detail) "
                          "VALUES ('d0000000000000000000000000000001', now(), 'x', 't', 't', '{}')"))
        conn.execute(text("SET LOCAL avelis.history_purge = 'on'"))
        conn.execute(text("DELETE FROM audit_event WHERE id = 'd0000000000000000000000000000001'"))
        with pytest.raises(Exception, match="封链时找不到"):
            conn.commit()
        conn.rollback()
    assert _verify()["ok"] is True


def test_state_columns_must_change_together(metadb, world):
    # 9：血缘边的 status 与 superseded_at、变更集的 status 与 resolved_at 只能成对、只改一次
    edge = new_id()
    with metadb.begin() as conn:
        conn.execute(text("INSERT INTO lineage_edge (id, origin, source_ref, relation, attributes, status, created_at) "
                          "VALUES (:id, 'manual', 'store.a', 'value_direct', '{}', 'active', now())"), {"id": edge})
    _rejected(metadb, "UPDATE lineage_edge SET superseded_at = '2020-01-01' WHERE id = :id", match="必须同时改",
              id=edge)
    _rejected(metadb, "UPDATE lineage_edge SET status = 'superseded' WHERE id = :id", match="必须同时改", id=edge)
    with metadb.begin() as conn:
        conn.execute(text("UPDATE lineage_edge SET status = 'superseded', superseded_at = now() WHERE id = :id"),
                     {"id": edge})
        marks = conn.execute(text("SELECT count(*) FROM history_mark WHERE row_id = :id"), {"id": edge}).scalar()
    assert marks == 2
    _rejected(metadb, "UPDATE lineage_edge SET status = 'active', superseded_at = NULL WHERE id = :id", id=edge)
    with metadb.begin() as conn:
        open_set = new_id()
        conn.execute(text("INSERT INTO change_set (id, run_id, datasource_id, status, summary, window_end, created_at) "
                          "VALUES (:id, :run, :ds, 'open', '{}', now(), now())"),
                     {"id": open_set, "run": world["run"], "ds": world["ds"]})
    _rejected(metadb, "UPDATE change_set SET status = 'resolved' WHERE id = :id", match="必须同时改", id=open_set)
    _rejected(metadb, "UPDATE change_set SET resolved_at = now() WHERE id = :id", match="必须同时改", id=open_set)
    assert _verify()["ok"] is True


def test_truncate_is_rejected_even_on_the_purge_path(metadb, world):
    # 10：清理开关只放行逐行 DELETE
    with metadb.connect() as conn:
        conn.execute(text("SET LOCAL avelis.history_purge = 'on'"))
        with pytest.raises(Exception, match="不能 TRUNCATE"):
            conn.execute(text("TRUNCATE column_version CASCADE"))
        conn.rollback()


def _admin() -> User:
    with session_scope() as s:
        return s.scalar(select(User).where(User.username == "admin"))


def test_export_reads_one_snapshot_with_hard_cap(metadb, world, monkeypatch):
    # 12：计数、链头与事件取自同一快照；新提交的事件不进入同一次导出；超过上限（cap + 1 硬限制）拒绝
    admin = _admin()
    snap = session_factory()()
    try:
        snap.connection(execution_options={"isolation_level": "REPEATABLE READ"})
        snap.execute(text("SET TRANSACTION READ ONLY"))
        body, manifest = records.export(snap, admin, records.EventFilter(), "csv")
        with session_scope() as s:
            s.add(ChangeEvent(change_set_id=world["cs"], asset_id=world["asset"], kind="column_added",
                              column_name="late", attribution_status="unsupported"))
        again, manifest2 = records.export(snap, admin, records.EventFilter(), "csv")
    finally:
        snap.rollback()
        snap.close()
    assert again == body and manifest2["chain_head"] == manifest["chain_head"]
    data = b"".join(line for line in body.splitlines(keepends=True) if not line.startswith(b"#"))
    rows = list(csv.DictReader(io.StringIO(data.decode("utf-8-sig"))))
    assert max(int(r["chain_seq"]) for r in rows) <= manifest["chain_head"]["seq"]
    # 13：危险前缀的标识符按文本输出；哈希按实际写出的字节
    assert "'=1+1" in {r["column"] for r in rows}
    assert hashlib.sha256(data).hexdigest() == manifest["content_sha256"]
    settings = dataclasses.replace(records.get_settings(), history_export_max_rows=2)
    monkeypatch.setattr(records, "get_settings", lambda: settings)
    s = session_factory()()
    try:
        with pytest.raises(records.ExportTooLarge):
            records.export(s, admin, records.EventFilter(), "json")
    finally:
        s.close()


def test_purge_keeps_versions_pinned_by_derived_metrics(metadb, world):
    # 3：派生口径 D（已发布）→ 组成版本 M1（已归档）→ AV1：AV1 必须保留；无人引用的 AV0 被清理
    av0, av1, av2 = world["versions"]
    result = retention.purge(retention_days=1, now=utcnow() + timedelta(days=2))
    assert result["status"] == "purged", result
    with metadb.connect() as conn:
        left = set(conn.execute(text("SELECT id FROM asset_version")).scalars())
    assert av0 not in left and {av1, av2} <= left
    assert _verify()["ok"] is True


def test_purge_succeeds_while_others_keep_writing(metadb, world):
    # 14：清理期间其他会话持续写审计（推进链头）：两段式清理不因快照冲突失败，链仍完整
    stop = threading.Event()
    written = []

    def writer():
        while not stop.is_set():
            with session_scope() as s:
                audit.record(s, None, "test.concurrent", "test", None)
            written.append(1)
            time.sleep(0.01)

    with session_scope() as s:
        for _ in range(50):
            audit.record(s, None, "test.old", "test", None)
    thread = threading.Thread(target=writer)
    thread.start()
    try:
        time.sleep(0.2)
        result = retention.purge(retention_days=1, now=utcnow() + timedelta(days=2))
    finally:
        stop.set()
        thread.join()
    assert result["status"] == "purged" and result["counts"].get("audit_event", 0) >= 50
    assert written, "并发写入应在清理期间持续进行"
    report = _verify()
    assert report["ok"] is True, report["issues"]


def test_empty_chain_with_advanced_head_is_reported(metadb, world):
    # 11：绕过触发器删光所有链上记录，链头仍在原位 → 不能判为完整
    tables = [t.name for t in integrity.TABLES]
    with metadb.begin() as conn:
        conn.execute(text("SET LOCAL session_replication_role = replica"))
        for t in ("metric_version_transition", "change_event", "column_version", "definition"):
            conn.execute(text(f"DELETE FROM {t}"))
        conn.execute(text("UPDATE asset SET current_version_id = NULL"))
        conn.execute(text("DELETE FROM metric_version"))
        for t in tables:
            conn.execute(text(f"DELETE FROM {t}"))
    report = _verify()
    assert report["checked_rows"] == 0 and report["first_break"]["kind"] == "head_mismatch"


def test_migration_upgrade_downgrade_upgrade_with_data(metadb):
    """已有数据的库：0005 上写入历史 → 升级（补算哈希）→ 核验 → 降级（触发器与链列移除，原数据保留）→ 再升级。"""
    from alembic import command

    from avelis.devtools import alembic_config, reset_metadata_db

    reset_metadata_db()
    cfg = alembic_config()
    command.downgrade(cfg, "0005")
    with metadb.begin() as conn:
        for i in range(5):
            conn.execute(text("INSERT INTO audit_event (id, at, actor_name, action, object_type, detail) "
                              "VALUES (:id, now() - make_interval(mins => :i), 'old', 'legacy', 't', "
                              "'{\"n\": 1}')"), {"id": new_id(), "i": i})
    command.upgrade(cfg, "head")
    report = _verify()
    assert report["ok"] is True and report["tables"]["audit_event"] == 5
    _rejected(metadb, "UPDATE audit_event SET action = 'x'")
    command.downgrade(cfg, "0005")
    with metadb.begin() as conn:
        assert conn.execute(text("SELECT count(*) FROM audit_event WHERE action = 'legacy'")).scalar() == 5
        conn.execute(text("UPDATE audit_event SET detail = '{\"n\": 2}'"))  # 降级后不再有只追加约束
        cols = set(conn.execute(text("SELECT column_name FROM information_schema.columns "
                                     "WHERE table_name = 'audit_event'")).scalars())
    assert not cols & {"chain_seq", "prev_hash", "row_hash"}
    command.upgrade(cfg, "head")
    report = _verify()
    assert report["ok"] is True and report["tables"]["audit_event"] == 5
