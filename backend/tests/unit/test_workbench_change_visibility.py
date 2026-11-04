"""最近变更复用详情的对象可见性，过滤后取六条并按可见对象计算摘要。"""

from datetime import timedelta
from unittest.mock import Mock

import pytest

from avelis.api import workbench
from avelis.db import utcnow
from avelis.models import Asset, ChangeEvent, ChangeSet, Impact, UserRole
from tests.unit.test_analysis_conversation_dataset_access import copied_box  # noqa: F401


@pytest.mark.parametrize("admin", [False, True])
def test_recent_changes_filter_before_limit_and_summarize_only_visible_objects(copied_box, monkeypatch, admin):
    box, visible_asset = copied_box
    session, user = box.session, box.user
    hidden_asset = Asset(
        id="hidden-asset",
        datasource_id="source",
        kind="table",
        catalog_name="local",
        db_name="secret",
        name="private_facts",
        domain_id="hidden",
    )
    session.add(hidden_asset)
    if admin:
        session.add(UserRole(user_id=user.id, role="admin"))
    now = utcnow()

    def change(identifier, age, assets):
        cs = ChangeSet(
            id=identifier,
            run_id="collection",
            datasource_id="source",
            status="open",
            summary={"impacts": len(assets)},
            window_end=now,
            created_at=now - timedelta(minutes=age),
        )
        session.add(cs)
        for n, asset in enumerate(assets):
            session.add(
                ChangeEvent(
                    id=f"{identifier}-event-{n}",
                    change_set_id=cs.id,
                    asset_id=asset.id if asset else None,
                    kind="column_added" if asset else "server_identity_changed",
                )
            )
            if asset:
                session.add(
                    Impact(
                        change_set_id=cs.id,
                        object_type="asset",
                        object_id=asset.id,
                        level="affected",
                        reason="test",
                    )
                )
        return cs

    # 最新六条全部不可见：过滤不能发生在 limit 之后。
    for n in range(7):
        change(f"hidden-{n}", n, [hidden_asset])
    change("source-only", 7, [None])
    change("mixed", 8, [hidden_asset, visible_asset])
    for n in range(7):
        change(f"visible-{n}", n + 9, [visible_asset])
    session.commit()
    detail = Mock(wraps=workbench.change_set_view)
    monkeypatch.setattr(workbench, "change_set_view", detail)
    recent = workbench.workbench(user, session)["recent_changes"]
    assert len(recent) == 6
    assert detail.called
    if admin:
        assert [r["id"] for r in recent] == [f"hidden-{n}" for n in range(6)]
        assert all(r["asset"] == "secret.private_facts" for r in recent)
        # 管理员继续看到混合事件和来源级事件，摘要保持原语义。
        session.query(ChangeSet).filter(ChangeSet.id.like("hidden-%")).delete(synchronize_session=False)
        recent = workbench.workbench(user, session)["recent_changes"]
        assert recent[0]["id"] == "source-only" and recent[0]["asset"] == "（数据源）"
        mixed = next(r for r in recent if r["id"] == "mixed")
        assert mixed["events"] == 2 and mixed["impacts"] == 2
    else:
        assert [r["id"] for r in recent] == ["mixed", *(f"visible-{n}" for n in range(5))]
        assert all(r["asset"] == "data.facts" and r["events"] == r["impacts"] == 1 for r in recent)


def test_no_visible_events_means_no_recent_changes(copied_box):
    box, asset = copied_box
    asset.domain_id = "hidden"
    box.session.add(
        ChangeSet(
            id="cs",
            run_id="collection",
            datasource_id="source",
            status="open",
            window_end=utcnow(),
        )
    )
    box.session.add(ChangeEvent(change_set_id="cs", asset_id=asset.id, kind="column_removed"))
    box.session.commit()
    assert workbench.workbench(box.user, box.session)["recent_changes"] == []
