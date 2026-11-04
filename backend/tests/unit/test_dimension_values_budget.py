"""对齐使用的成员取样与精确核验共同遵守调用方的剩余时间。"""

from types import SimpleNamespace as NS

import pytest

from avelis.ask import service
from avelis.lineage.sql import SqlDialect
from avelis.models import MetricVersion
from tests.unit.test_overfit_batch1 import _batch1_dimension_reader


@pytest.mark.parametrize("exact", [False, True])
def test_dimension_reads_limit_query_to_remaining_time(monkeypatch, exact):
    session, version, state, _ = _batch1_dimension_reader(monkeypatch, SqlDialect("postgres", "catalog_a"))
    state["rows"] = [[1]] if exact else [["MemberA"]]
    monkeypatch.setattr(service.cache, "client", lambda: NS(get=lambda *args: None, set=lambda *args, **kwargs: None))
    if exact:
        result = service._existing_dimension_values(
            session, object(), version, "维度甲", ["MemberA"], object(), remaining_seconds=lambda: 2.25
        )
        assert result == {"MemberA"}
    else:
        result = service._dimension_values(session, object(), version, object(), remaining_seconds=lambda: 2.25)
        assert result == {"维度甲": ["MemberA"]}
    assert state["queries"][0][1]["timeout_seconds"] == 2


@pytest.mark.parametrize("exact", [False, True])
@pytest.mark.parametrize("remaining", [0, 0.9])
def test_exhausted_dimension_reads_skip_guards_and_database(monkeypatch, exact, remaining):
    monkeypatch.setattr(service, "dependency_drift", lambda *args: pytest.fail("预算耗尽后不再读取治理元数据"))
    if exact:
        assert (
            service._existing_dimension_values(
                None, object(), object(), "dimension_a", ["MemberA"], object(), remaining_seconds=lambda: remaining
            )
            is None
        )
    else:
        assert service._dimension_values(None, object(), object(), object(), remaining_seconds=lambda: remaining) == {}


@pytest.mark.parametrize("exact", [False, True])
def test_dimension_read_rechecks_budget_after_live_check(monkeypatch, exact):
    session, version, state, _ = _batch1_dimension_reader(monkeypatch, SqlDialect("postgres", "catalog_a"))
    remaining = NS(seconds=2)
    monkeypatch.setattr(service.cache, "client", lambda: NS(get=lambda *args: None, set=lambda *args, **kwargs: None))

    def live(*args):
        remaining.seconds = 0
        return []

    monkeypatch.setattr(service, "_live_check", live)
    if exact:
        assert (
            service._existing_dimension_values(
                session,
                object(),
                version,
                "维度甲",
                ["MemberA"],
                object(),
                remaining_seconds=lambda: remaining.seconds,
            )
            is None
        )
    else:
        assert (
            service._dimension_values(session, object(), version, object(), remaining_seconds=lambda: remaining.seconds)
            == {}
        )
    assert not state["queries"]


def test_sampling_stops_before_the_next_dimension_when_budget_is_exhausted(monkeypatch):
    session, version, state, _ = _batch1_dimension_reader(monkeypatch, SqlDialect("postgres", "catalog_a"))
    version.dimensions.append({"name": "维度乙", "column": "column_b"})
    state["rows"] = [["MemberA"]]
    remaining = NS(seconds=2)
    connector = service.query_connector(None, None)
    execute = connector.execute_select

    def spend(sql, **limits):
        remaining.seconds = 0
        return execute(sql, **limits)

    connector.execute_select = spend
    monkeypatch.setattr(service.cache, "client", lambda: NS(get=lambda *args: None, set=lambda *args, **kwargs: None))
    assert service._dimension_values(
        session, object(), version, object(), remaining_seconds=lambda: remaining.seconds
    ) == {"维度甲": ["MemberA"]}
    assert len(state["queries"]) == 1


@pytest.mark.parametrize("exact", [False, True])
def test_derived_dimension_reads_share_the_same_budget(monkeypatch, exact):
    session, version, state, _ = _batch1_dimension_reader(monkeypatch, SqlDialect("postgres", "catalog_a"))
    second = NS(**vars(version))
    derived = NS(
        status="published",
        kind="derived",
        dimensions=version.dimensions,
        components=[{"version_id": "first"}, {"version_id": "second"}],
    )
    stored_get = session.get
    session.get = lambda model, key: (
        {"first": version, "second": second}.get(key) if model is MetricVersion else stored_get(model, key)
    )
    state["rows"] = [[1]] if exact else [["MemberA"]]
    remaining = NS(seconds=1)
    connector = service.query_connector(None, None)
    execute = connector.execute_select

    def spend(sql, **limits):
        remaining.seconds = 0
        return execute(sql, **limits)

    connector.execute_select = spend
    monkeypatch.setattr(service.cache, "client", lambda: NS(get=lambda *args: None, set=lambda *args, **kwargs: None))
    if exact:
        service._existing_dimension_values(
            session,
            object(),
            derived,
            "维度甲",
            ["MemberA"],
            object(),
            remaining_seconds=lambda: remaining.seconds,
        )
    else:
        service._dimension_values(session, object(), derived, object(), remaining_seconds=lambda: remaining.seconds)
    assert len(state["queries"]) == 1
    assert state["queries"][0][1]["timeout_seconds"] == 1
