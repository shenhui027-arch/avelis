"""探索结果保留执行时的上游来源，历史与当前血缘都按阅读者权限检查。"""

import json
from dataclasses import replace

import pytest

from avelis.analysis import exploration, service
from avelis.analysis.dataset_access import can_read_dataset
from avelis.models import Asset, LineageEdge
from tests.unit.test_analysis_dataset_access import dataset_db  # noqa: F401  夹具按参数名注入
from tests.unit.test_analysis_explore import resolver as fake_resolver
from tests.unit.test_analysis_explore_tool import GOOD, explore_box


def test_explore_saves_bound_upstream_asset_ids(monkeypatch):
    box, _ = explore_box(monkeypatch)

    def resolver(session, user, source):
        resolve = fake_resolver(source.default_catalog, None)

        def bound(catalog, db, name):
            return replace(resolve(catalog, db, name), upstream=(("root-z", "vz"), ("root-a", "va")))

        return bound

    monkeypatch.setattr(exploration, "resolver", resolver)
    out = json.loads(box.dispatch("query_sql", {"sql": GOOD, "purpose": "检查变化"}))
    assert box.datasets()[out["dataset"]].meta["upstream_asset_ids"] == ["root-a", "root-z"]


@pytest.mark.parametrize("revoked", ["historical", "current"])
def test_changed_view_checks_historical_and_current_upstream(dataset_db, revoked):
    session, user, run, dataset, view, _, _ = dataset_db
    historical = Asset(
        id="historical",
        datasource_id="source",
        kind="table",
        catalog_name="local",
        db_name="data",
        name="historical",
        domain_id="visible",
    )
    current = Asset(
        id="current",
        datasource_id="source",
        kind="table",
        catalog_name="local",
        db_name="data",
        name="current",
        domain_id="visible",
    )
    view.kind = "view"
    dataset.kind = "explore"
    dataset.meta = {"tables": [{"asset_id": view.id}], "upstream_asset_ids": [historical.id]}
    edge = LineageEdge(
        origin="manual",
        target_asset_id=view.id,
        source_asset_id=historical.id,
        source_ref="local.data.historical",
        relation="value",
    )
    session.add_all([historical, current, edge])
    session.commit()
    assert can_read_dataset(session, user, dataset)

    edge.source_asset_id, edge.source_ref = current.id, "local.data.current"
    session.commit()
    assert can_read_dataset(session, user, dataset)
    (historical if revoked == "historical" else current).domain_id = "hidden"
    session.commit()

    assert not can_read_dataset(session, user, dataset)
    saved = service.run_view(session, run, user=user)["datasets"][0]
    assert saved["restricted"] and saved["rows"] == []


def test_legacy_explore_checks_current_upstream_without_snapshot(dataset_db):
    session, user, _, dataset, view, _, _ = dataset_db
    current = Asset(
        id="current",
        datasource_id="source",
        kind="table",
        catalog_name="local",
        db_name="data",
        name="current",
        domain_id="visible",
    )
    view.kind = "view"
    dataset.kind = "explore"
    dataset.meta = {"tables": [{"asset_id": view.id}]}
    session.add_all(
        [
            current,
            LineageEdge(
                origin="manual",
                target_asset_id=view.id,
                source_asset_id=current.id,
                source_ref="local.data.current",
                relation="value",
            ),
        ]
    )
    session.commit()
    assert can_read_dataset(session, user, dataset)
    current.domain_id = "hidden"
    session.commit()
    assert not can_read_dataset(session, user, dataset)
