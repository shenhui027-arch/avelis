"""资产档案页聚合（ADR 0022）：内存 SQLite，覆盖各段的归并、权限裁剪与 HTTP 契约。"""

from datetime import timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import JSON, MetaData, create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from avelis.api import dossier as dossier_api
from avelis.api import schemas as S
from avelis.api.deps import current_user, get_session
from avelis.db import Base, utcnow
from avelis.governance import dossier, lineage_notes
from avelis.models import (
    AnalysisDataset,
    AnalysisRun,
    Answer,
    Asset,
    AssetVersion,
    ColumnVersion,
    Datasource,
    Domain,
    DomainMember,
    ExtractedStatement,
    ImportJob,
    JoinPath,
    LineageEdge,
    Metric,
    MetricVersion,
    User,
    UserRole,
)


@pytest.fixture
def db(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    metadata = MetaData()
    for source in Base.metadata.tables.values():
        table = source.to_metadata(metadata)
        for column in table.columns:
            if isinstance(column.type, JSONB):
                column.type = JSON()
    metadata.create_all(engine)
    monkeypatch.setattr(lineage_notes, "can_edit", lambda session, user, asset: False)
    monkeypatch.setattr(lineage_notes, "model_available", lambda session: False)
    monkeypatch.setattr(lineage_notes, "notes_for", lambda session, asset, basis: {})
    with Session(engine, expire_on_commit=False) as session:
        me = User(id="me", username="me", display_name="查询者", password_hash="unused")
        other = User(id="other", username="other", display_name="负责人", password_hash="unused")
        admin = User(id="admin", username="admin", display_name="管理员", password_hash="unused")
        session.add_all(
            [
                me,
                other,
                admin,
                UserRole(user_id="me", role="analyst"),
                UserRole(user_id="other", role="analyst"),
                UserRole(user_id="admin", role="admin"),
                Domain(id="vis", code="vis", name="可见域", owner_id="other"),
                Domain(id="hid", code="hid", name="其他域", owner_id="other"),
                DomainMember(domain_id="vis", user_id="me", role="viewer"),
                DomainMember(domain_id="vis", user_id="other", role="owner"),
                Datasource(id="ds", name="离线源", kind="fake", host="offline", port=1),
            ]
        )
        for name, domain, kind in (
            ("t_a", "vis", "table"),
            ("t_b", "vis", "table"),
            ("t_c", "vis", "table"),
            ("t_h", "hid", "table"),
            ("v_x", "vis", "view"),
        ):
            add_asset(session, name, domain, kind)
        session.commit()
        yield session
    engine.dispose()


def add_asset(session, name, domain="vis", kind="table", columns=(), comment=None):
    asset = Asset(id=name, datasource_id="ds", catalog_name="cat", db_name="db", name=name, kind=kind, domain_id=domain)
    session.add(asset)
    if columns or comment:
        set_version(session, asset, columns, comment)
    return asset


def set_version(session, asset, columns, comment=None):
    version = AssetVersion(
        id=f"{asset.id}_v1",
        asset_id=asset.id,
        version_no=1,
        observed_run_id="run",
        last_seen_run_id="run",
        kind=asset.kind,
        catalog_name=asset.catalog_name,
        db_name=asset.db_name,
        name=asset.name,
        comment=comment,
        content_hash="h",
    )
    session.add(version)
    for i, (column, column_comment) in enumerate(columns):
        session.add(
            ColumnVersion(
                asset_version_id=version.id,
                column_id=f"{asset.id}.{column}",
                ordinal=i,
                name=column,
                data_type="INT",
                comment=column_comment,
            )
        )
    asset.current_version_id = version.id


def edge(source_ref, source_column, target_column, *, target="t_a", source=None, relation="value_direct", **kw):
    return LineageEdge(
        origin=kw.pop("origin", "parsed"),
        target_asset_id=target,
        target_column=target_column,
        source_asset_id=source,
        source_ref=source_ref,
        source_column=source_column,
        relation=relation,
        **kw,
    )


def user(session, user_id):
    return session.get(User, user_id)


# --- 字段来历 ---------------------------------------------------------------------


def test_field_lineage_merges_sources_and_prefers_parsed(db):
    t_a = db.get(Asset, "t_a")
    set_version(db, t_a, [("col_w", "无来源"), ("col_x", "计算值"), ("col_v", None)])
    db.add_all(
        [
            edge("cat.db.t_b", "col_y", "col_x", source="t_b", origin="import", expression="col_y"),
            edge("cat.db.t_b", "col_y", "col_x", source="t_b", relation="value_computed", expression="col_y + 1"),
            edge("cat.db.t_h", "col_z", "col_x", source="t_h", origin="import", relation="value_aggregated"),
            edge("cat.db.t_b", "col_y", "col_x", source="t_b", origin="import"),
            edge("cat.raw.q_1", "col_q", "col_v", origin="import"),
            edge("cat.db.t_b", None, None, source="t_b", relation="table"),
            edge("cat.db.t_b", "col_f", None, source="t_b", relation="filter"),
            edge("cat.db.t_c", "col_old", "col_x", source="t_c", status="superseded"),
            edge("cat.db.t_b", "col_gone", "col_missing", source="t_b"),
        ]
    )
    db.commit()

    fields = dossier.field_lineage(db, t_a)
    assert [f["column"] for f in fields] == ["col_x", "col_v"]
    col_x = fields[0]
    assert col_x["comment"] == "计算值"
    assert col_x["relation"] == "value_computed"
    assert col_x["expression"] == "col_y + 1"
    assert col_x["origin"] == "parsed"
    assert col_x["sources"] == [
        {"asset_id": "t_b", "ref": "cat.db.t_b", "column": "col_y"},
        {"asset_id": "t_h", "ref": "cat.db.t_h", "column": "col_z"},
    ]
    assert col_x["basis_hash"] == dossier.basis_hash("value_computed", "col_y + 1", col_x["sources"])
    col_v = fields[1]
    assert col_v["relation"] == "value_direct"
    assert col_v["origin"] == "import"
    assert col_v["sources"] == [{"asset_id": None, "ref": "cat.raw.q_1", "column": "col_q"}]
    for item in fields:
        S.DossierFieldOut.model_validate(item)


def test_field_lineage_without_current_version_orders_by_name(db):
    db.add_all([edge("cat.db.t_b", "col_y", "col_z"), edge("cat.db.t_b", "col_y", "col_m")])
    db.commit()
    assert [f["column"] for f in dossier.field_lineage(db, db.get(Asset, "t_a"))] == ["col_m", "col_z"]


def test_basis_hash_stable_and_sensitive():
    one = [{"ref": "cat.db.t_b", "column": "col_y"}, {"ref": "cat.db.t_c", "column": None}]
    two = [{"asset_id": "x", "ref": "cat.db.t_c", "column": None}, {"ref": "cat.db.t_b", "column": "col_y"}]
    base = dossier.basis_hash("value_computed", "col_y + 1", one)
    assert len(base) == 64
    assert dossier.basis_hash("value_computed", "  col_y + 1\n", two) == base
    assert dossier.basis_hash("value_computed", "col_y + 2", one) != base
    assert dossier.basis_hash("value_direct", "col_y + 1", one) != base
    assert dossier.basis_hash("value_computed", "col_y + 1", one[:1]) != base
    assert dossier.basis_hash("value_computed", "col_y + 1", [{"ref": "cat.db.t_b", "column": "col_k"}]) != (
        dossier.basis_hash("value_computed", "col_y + 1", one[:1])
    )
    assert dossier.basis_hash(None, None, []) == dossier.basis_hash(None, "  ", [])


# --- 上下游 -----------------------------------------------------------------------


def test_neighbors_prune_unreadable_and_count_columns(db):
    set_version(db, db.get(Asset, "t_b"), [("col_y", None)], comment="上游表 B")
    set_version(db, db.get(Asset, "t_h"), [("col_z", None)], comment="不该出现的注释")
    set_version(db, db.get(Asset, "t_c"), [("col_k", None)], comment="下游表 C")
    db.add_all(
        [
            edge("cat.db.t_b", "col_y", "col_x", source="t_b"),
            edge("cat.db.t_b", "col_y", "col_w", source="t_b", relation="value_computed"),
            edge("cat.db.t_b", "col_y", "col_x", source="t_b", origin="import"),
            edge("cat.db.t_b", "col_f", None, source="t_b", relation="filter"),
            edge("cat.db.t_h", "col_z", "col_x", source="t_h"),
            edge("cat.raw.q_1", None, None, relation="table", origin="import"),
            edge("cat.db.t_a", "col_x", "col_x", source="t_a"),
            edge("cat.db.t_c", "col_k", "col_x", source="t_c", status="superseded"),
            # 下游
            edge("cat.db.t_a", "col_x", "col_k", source="t_a", target="t_c"),
            edge("cat.db.t_a", "col_w", "col_k", source="t_a", target="t_c"),
            edge("cat.db.t_a", "col_x", "col_j", source="t_a", target="t_c", relation="group_by"),
            edge("cat.db.t_a", "col_x", "col_z", source="t_a", target="t_h"),
            edge("cat.db.t_a", "col_x", None, source="t_a", target=None, target_ref="cat.out.r_1", origin="import"),
        ]
    )
    db.commit()

    up, down = dossier.neighbors(db, user(db, "me"), db.get(Asset, "t_a"))
    assert up == [
        {"asset_id": "t_b", "ref": "cat.db.t_b", "comment": "上游表 B", "datasource_kind": "fake", "columns": 2},
        {"asset_id": None, "ref": "cat.db.t_h", "comment": None, "datasource_kind": None, "columns": 1},
        {"asset_id": None, "ref": "cat.raw.q_1", "comment": None, "datasource_kind": None, "columns": 0},
    ]
    assert down == [
        {"asset_id": "t_c", "ref": "cat.db.t_c", "comment": "下游表 C", "datasource_kind": "fake", "columns": 2},
        {"asset_id": None, "ref": "cat.db.t_h", "comment": None, "datasource_kind": None, "columns": 1},
        {"asset_id": None, "ref": "cat.out.r_1", "comment": None, "datasource_kind": None, "columns": 0},
    ]
    # 管理员能看到所有表
    up_admin, _ = dossier.neighbors(db, user(db, "admin"), db.get(Asset, "t_a"))
    assert [x["asset_id"] for x in up_admin] == ["t_b", "t_h", None]
    assert up_admin[1]["comment"] == "不该出现的注释"


# --- 口径 -------------------------------------------------------------------------


def metric(session, code, domain="vis", versions=()):
    session.add(Metric(id=code, code=code, name=f"指标 {code}", domain_id=domain, owner_id="other", created_by="other"))
    for no, status, extra in versions:
        session.add(
            MetricVersion(
                id=f"{code}_v{no}",
                metric_id=code,
                version_no=no,
                status=status,
                description=f"{code} 第 {no} 版",
                expression="SUM(col_x)",
                time_column="col_d",
                timezone="UTC",
                created_by="other",
                **extra,
            )
        )


def test_metrics_using_roles_versions_and_visibility(db):
    db.add(
        JoinPath(
            id="p_1",
            code="p_1",
            name="关联",
            left_asset_id="t_b",
            left_columns=["col_k"],
            right_asset_id="t_a",
            right_columns=["col_k"],
            cardinality="n1",
            status="approved",
            created_by="other",
        )
    )
    on_a = {"base_asset_id": "t_a"}
    dep_a = {"base_asset_id": "t_b", "dependency": [{"asset_id": "t_b"}, {"asset_id": "t_a", "column": "col_x"}]}
    join_a = {"base_asset_id": "t_b", "joins": [{"join_path_id": "p_1"}]}
    metric(db, "m_base", versions=[(1, "published", on_a), (2, "draft", {"base_asset_id": "t_b"})])
    metric(db, "m_dep", versions=[(1, "draft", {}), (2, "pending_review", dep_a), (3, "rejected", on_a)])
    metric(db, "m_join", versions=[(1, "affected", join_a), (2, "retired", on_a)])
    metric(db, "m_retired", versions=[(1, "retired", on_a), (2, "archived", on_a)])
    metric(db, "m_hidden", domain="hid", versions=[(1, "published", on_a)])
    metric(db, "m_pub_other", versions=[(1, "published", {"base_asset_id": "t_b"}), (2, "draft", on_a)])
    metric(db, "m_strong", versions=[(1, "published", {**dep_a, **join_a, "base_asset_id": "t_a"})])
    metric(db, "m_two_pub", versions=[(1, "published", on_a), (2, "published", dep_a)])
    db.commit()

    found = {m["code"]: m for m in dossier.metrics_using(db, user(db, "me"), db.get(Asset, "t_a"))}
    assert set(found) == {"m_base", "m_dep", "m_join", "m_strong", "m_two_pub"}

    def pick(code):
        return found[code]["version_no"], found[code]["status"], found[code]["role"]

    assert pick("m_base") == (1, "published", "base")
    assert pick("m_dep") == (2, "pending_review", "dependency")
    assert pick("m_join") == (1, "affected", "join")
    assert pick("m_strong") == (1, "published", "base")
    assert pick("m_two_pub") == (2, "published", "dependency")
    for item in found.values():
        S.DossierMetricOut.model_validate(item)
    assert {m["code"] for m in dossier.metrics_using(db, user(db, "admin"), db.get(Asset, "t_a"))} >= {"m_hidden"}


# --- 最近谁问过 -------------------------------------------------------------------


def answer(session, answer_id, by, ago, asset_id="t_a", question=None):
    session.add(
        Answer(
            id=answer_id,
            question=question or f"问题 {answer_id}",
            asked_by=by,
            asked_at=utcnow() - ago,
            status="answered",
            model_adapter="fake",
            evidence={"sources": [{"asset_id": asset_id, "qualified": f"cat.db.{asset_id}"}]},
        )
    )


def test_asked_groups_people_and_hides_questions(db):
    answer(db, "a_1", "me", timedelta(days=3))
    answer(db, "a_2", "me", timedelta(hours=2), question="我最近的问题")
    answer(db, "a_3", "other", timedelta(days=1))
    answer(db, "a_old", "other", timedelta(days=40))
    answer(db, "a_else", "other", timedelta(hours=1), asset_id="t_b")
    db.add(AnalysisRun(id="r_1", user_id="other", question="分析里的问题", trigger="manual", status="succeeded"))
    db.flush()
    run = db.get(AnalysisRun, "r_1")
    run.created_at = utcnow() - timedelta(minutes=30)
    db.add_all(
        [
            AnalysisDataset(run_id="r_1", seq=1, kind="ungoverned", title="D1", meta={"tables": [{"asset_id": "t_a"}]}),
            AnalysisDataset(run_id="r_1", seq=2, kind="context", title="D2", meta={"asset_ids": ["t_a", "t_b"]}),
            AnalysisDataset(run_id="r_1", seq=3, kind="governed", title="D3", answer_id="a_run", meta={}),
        ]
    )
    # 分析里受治理查询留下的回答归到运行上，不重复计数
    answer(db, "a_run", "other", timedelta(minutes=29))
    db.add(AnalysisRun(id="r_2", user_id="me", question="无关分析", trigger="manual", status="succeeded"))
    db.add(AnalysisDataset(run_id="r_2", seq=1, kind="context", title="D1", meta={"asset_ids": ["t_b"]}))
    db.commit()

    out = dossier.asked(db, user(db, "me"), db.get(Asset, "t_a"))
    S.DossierAskedOut.model_validate(out)
    assert out["days"] == 30
    assert out["total"] == 4
    assert [(p["user"]["id"], p["count"]) for p in out["people"]] == [("other", 2), ("me", 2)]
    by_id = {p["user"]["id"]: p for p in out["people"]}
    assert by_id["me"]["last_question"] == "我最近的问题"
    assert by_id["other"]["last_question"] is None
    assert by_id["other"]["user"]["display_name"] == "负责人"

    as_admin = {p["user"]["id"]: p for p in dossier.asked(db, user(db, "admin"), db.get(Asset, "t_a"))["people"]}
    assert as_admin["other"]["last_question"] == "分析里的问题"


def test_asked_caps_people_at_eight(db):
    for i in range(10):
        db.add(User(id=f"u_{i}", username=f"u_{i}", display_name=f"用户 {i}", password_hash="unused"))
        answer(db, f"q_{i}", f"u_{i}", timedelta(hours=i + 1))
    db.commit()
    out = dossier.asked(db, user(db, "me"), db.get(Asset, "t_a"))
    assert out["total"] == 10
    assert [p["user"]["id"] for p in out["people"]] == [f"u_{i}" for i in range(8)]


# --- 产出方式 ---------------------------------------------------------------------


def script(session, target, statements, *, filename="job.sql", status="done", job_id=None):
    """statements: [(statement_type, sql, writes_target)]；writes_target 为真时登记一条写入目标表的导入边。"""
    job = ImportJob(
        id=job_id or f"{target}_{filename}",
        source_key=job_id or f"{target}_{filename}",
        datasource_id="ds",
        source_type="sql",
        filename=filename,
        language="sql",
        dialect="starrocks",
        content_hash="h",
        status=status,
        created_by="other",
    )
    session.add(job)
    for seq, (kind, sql, writes) in enumerate(statements, 1):
        stmt = ExtractedStatement(
            id=f"{job.id}_{seq}",
            job_id=job.id,
            seq=seq,
            line_start=seq * 10,
            line_end=seq * 10 + 2,
            method="sql",
            raw_text=sql,
            sql_text=sql,
            statement_type=kind,
            status="parsed",
        )
        session.add(stmt)
        if writes:
            session.add(
                edge(
                    "cat.db.t_src",
                    "col_s",
                    "col_x",
                    target=target,
                    origin="import",
                    import_job_id=job.id,
                    target_ref=f"cat.db.{target}",
                    attributes={"statement_id": stmt.id, "job": filename, "design_time": True},
                )
            )


INSERT = ("INSERT", "INSERT INTO db.{t} SELECT col_s FROM db.t_src", True)


def produce(session, name, *jobs, show_sql=False):
    add_asset(session, name)
    for i, statements in enumerate(jobs):
        script(session, name, [(k, sql.format(t=name), w) for k, sql, w in statements], filename=f"f_{i}.sql")
    session.commit()
    return dossier.produced(session, user(session, "me"), session.get(Asset, name), show_sql=show_sql)


@pytest.mark.parametrize(
    ("statements", "mode"),
    [
        ([("INSERT OVERWRITE", "INSERT OVERWRITE db.{t} SELECT col_s FROM db.t_src", True)], "overwrite"),
        ([("TRUNCATE TABLE", "TRUNCATE TABLE db.{t}", False), INSERT], "overwrite"),
        ([("DELETE", "DELETE FROM {t}", False), INSERT], "overwrite"),
        ([("DELETE", "DELETE FROM db.{t} WHERE col_x = 1", False), INSERT], None),
        ([("DELETE", "DELETE FROM db.t_other WHERE col_x = 1", False), INSERT], "append"),
        (
            [
                (
                    "INSERT OVERWRITE",
                    "INSERT OVERWRITE TABLE db.{t} PARTITION (col_p = 1) SELECT col_s FROM db.t_src",
                    True,
                )
            ],
            None,
        ),
        ([("TRUNCATE TABLE", "TRUNCATE TABLE db.t_other", False), INSERT], "append"),
        ([("TRUNCATE TABLE", "TRUNCATE TABLE other_db.{t}", False), INSERT], "append"),
        ([INSERT, ("TRUNCATE TABLE", "TRUNCATE TABLE db.{t}", False)], "append"),
        ([INSERT], "append"),
        ([("CREATE TABLE AS", "CREATE TABLE db.{t} AS SELECT col_s FROM db.t_src", True)], "create"),
        ([("MERGE", "MERGE INTO db.{t} USING db.t_src ON 1 = 1", True)], None),
        ([("UPDATE", "UPDATE db.{t} SET col_x = 1", True)], None),
        ([("TRUNCATE TABLE", "TRUNCATE TABLE ((( db.{t}", False), INSERT], None),
        ([("INSERT OVERWRITE", "INSERT OVERWRITE db.{t} SELECT 1", True), INSERT], None),
    ],
)
def test_produced_write_mode(db, statements, mode):
    out = produce(db, "t_p", statements)
    assert out["kind"] == "script"
    assert out["write_mode"] == mode
    S.DossierProducedOut.model_validate(out)


def test_produced_modes_across_files_must_agree(db):
    overwrite = [("INSERT OVERWRITE", "INSERT OVERWRITE db.{t} SELECT col_s FROM db.t_src", True)]
    create = [("CREATE TABLE AS", "CREATE TABLE db.{t} AS SELECT col_s FROM db.t_src", True)]
    assert produce(db, "t_q", overwrite, create)["write_mode"] is None
    assert produce(db, "t_r", overwrite, overwrite)["write_mode"] == "overwrite"


def test_produced_scripts_order_limit_and_sql_visibility(db):
    add_asset(db, "t_s")
    script(db, "t_s", [("INSERT", "INSERT INTO db.t_s SELECT 3", True)] * 3, filename="b.sql")
    script(db, "t_s", [("INSERT", "INSERT INTO db.t_s SELECT 1", True)] * 3, filename="a.sql")
    script(db, "t_s", [("INSERT", "INSERT INTO db.t_s SELECT 2", True)], filename="old.sql", status="superseded")
    db.commit()
    hidden = dossier.produced(db, user(db, "me"), db.get(Asset, "t_s"), show_sql=False)
    assert [(s["filename"], s["line_start"]) for s in hidden["scripts"]] == [
        ("a.sql", 10),
        ("a.sql", 20),
        ("a.sql", 30),
        ("b.sql", 10),
        ("b.sql", 20),
    ]
    assert all(s["sql"] is None for s in hidden["scripts"])
    shown = dossier.produced(db, user(db, "me"), db.get(Asset, "t_s"), show_sql=True)
    assert shown["scripts"][0]["sql"] == "INSERT INTO db.t_s SELECT 1"
    assert shown["scripts"][0]["statement_type"] == "INSERT"
    S.DossierProducedOut.model_validate(shown)


def test_produced_none_and_view(db):
    assert dossier.produced(db, user(db, "me"), db.get(Asset, "t_a"), show_sql=True) is None
    add_asset(db, "t_old")
    script(db, "t_old", [INSERT[:1] + ("INSERT INTO db.t_old SELECT 1", True)], status="superseded")
    db.commit()
    assert dossier.produced(db, user(db, "me"), db.get(Asset, "t_old"), show_sql=True) is None
    assert dossier.produced(db, user(db, "me"), db.get(Asset, "v_x"), show_sql=True) == {
        "kind": "view",
        "write_mode": None,
        "scripts": [],
    }


# --- 整体 -------------------------------------------------------------------------


def note(column, text, status="confirmed"):
    return {
        "id": f"n_{column or 'table'}",
        "column": column or None,
        "text": text,
        "status": status,
        "origin": "human",
        "confirmed_by": None,
        "confirmed_at": None,
        "updated_at": utcnow().isoformat(),
    }


def test_build_validates_and_prunes(db, monkeypatch):
    t_a = db.get(Asset, "t_a")
    set_version(db, t_a, [("col_x", "计算值"), ("col_y", None)])
    db.add_all(
        [
            edge("cat.db.t_b", "col_y", "col_x", source="t_b", relation="value_computed", expression="col_y * 2"),
            edge("cat.db.t_h", "col_z", "col_x", source="t_h"),
        ]
    )
    answer(db, "a_1", "me", timedelta(hours=1))
    db.commit()
    seen = {}

    def notes_for(session, asset, basis):
        seen["basis"] = basis
        return {"": note("", "整表说明", "draft"), "col_x": note("col_x", "由来源字段翻倍")}

    monkeypatch.setattr(lineage_notes, "notes_for", notes_for)
    monkeypatch.setattr(lineage_notes, "can_edit", lambda session, user, asset: True)
    monkeypatch.setattr(lineage_notes, "model_available", lambda session: True)

    out = dossier.build(db, user(db, "me"), t_a)
    model = S.AssetDossierOut.model_validate(out)
    assert model.asset.id == "t_a"
    assert out["domain"] == {"id": "vis", "name": "可见域"}
    assert out["owner"] == {"id": "other", "display_name": "负责人"}
    assert out["comment"] is None
    assert out["column_count"] == 2
    assert out["summary"]["source"] == "note"
    assert out["summary"]["text"] == "整表说明"
    assert out["can_edit_notes"] is True and out["model_available"] is True
    assert seen["basis"] == {"col_x": out["fields"][0]["basis_hash"]}
    field = out["fields"][0]
    assert field["note"]["text"] == "由来源字段翻倍"
    assert {s["ref"]: s["asset_id"] for s in field["sources"]} == {"cat.db.t_b": "t_b", "cat.db.t_h": None}
    assert out["asked"]["total"] == 1
    assert out["upstream"][1]["asset_id"] is None

    # 有表注释时一句话说明取注释
    db.get(AssetVersion, "t_a_v1").comment = "  表注释  "
    db.commit()
    out = dossier.build(db, user(db, "me"), t_a)
    assert out["comment"] == "表注释"
    assert out["summary"] == {"text": "表注释", "source": "comment", "note": None}


def test_build_empty_asset(db):
    out = dossier.build(db, user(db, "me"), db.get(Asset, "t_c"))
    S.AssetDossierOut.model_validate(out)
    assert out["summary"] == {"text": None, "source": None, "note": None}
    assert out["fields"] == [] and out["produced"] is None and out["asked"]["people"] == []


# --- HTTP -------------------------------------------------------------------------


def test_dossier_endpoint(db):
    app = FastAPI()
    app.include_router(dossier_api.router)
    app.dependency_overrides[current_user] = lambda: user(db, "me")
    app.dependency_overrides[get_session] = lambda: db
    with TestClient(app) as client:
        ok = client.get("/api/assets/t_a/dossier")
        assert ok.status_code == 200, ok.text
        assert ok.json()["asset"]["id"] == "t_a"
        assert ok.json()["can_edit_notes"] is False
        assert client.get("/api/assets/t_h/dossier").status_code == 404
        assert client.get("/api/assets/missing/dossier").status_code == 404
