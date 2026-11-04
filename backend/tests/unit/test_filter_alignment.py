"""筛选取值对齐的业务中立规则与查询守卫。"""

from types import SimpleNamespace as NS

import pytest
import sqlglot
from sqlglot import exp

from avelis.ask import alignment, service
from avelis.ask.intent import DimensionFilter
from avelis.connectors.base import QueryError
from avelis.engines import CredentialMissing, UnsupportedEngine
from avelis.lineage.sql import SqlDialect
from avelis.models import Asset, ColumnVersion, Datasource, JoinPath, MetricVersion


@pytest.mark.parametrize("asset_ids", [[], ["dimension_a", "dimension_b"]])
@pytest.mark.parametrize("kind", ["base", "derived"])
def test_confirmed_values_skip_all_member_and_alias_queries(monkeypatch, asset_ids, kind):
    items = [{"dimension": "d", "original": "alias", "options": ["1", "2"]}]
    if asset_ids:
        items[0]["candidate_sources"] = {"1": asset_ids}
    confirmed = alignment.confirmed_values(items, "1")
    monkeypatch.setattr(service, "_dimension_values", lambda *a, **kw: pytest.fail("确认值不应重新取样"))
    monkeypatch.setattr(service, "dependency_drift", lambda *a: pytest.fail("确认值不应重做对齐"))
    result = alignment.align_filters(
        None,
        None,
        NS(kind=kind),
        [DimensionFilter(dimension="d", values=["1"])],
        NS(),
        confirmed=confirmed,
        remaining_seconds=lambda: 0,
    )
    assert result.filters[0].values == ["1"] and not result.unresolved and not result.unknown
    assert result.explanations == [
        {
            "dimension": "d",
            "original": "alias",
            "value": "1",
            "method": "confirmed",
            **({"asset_id": asset_id} if asset_id else {}),
        }
        for asset_id in asset_ids or [None]
    ]


@pytest.mark.parametrize(
    "answer",
    [
        "3",
        [{"dimension": "other", "original": "alias", "value": "1"}],
        [{"dimension": "d", "original": "other", "value": "1"}],
    ],
)
def test_confirmation_requires_offered_dimension_original_and_value(answer):
    assert alignment.confirmed_values([{"dimension": "d", "original": "alias", "options": ["1", "2"]}], answer) == []


@pytest.fixture
def world(monkeypatch):
    fact = NS(id="fact", datasource_id="ds", catalog_name="catalog", db_name="schema_a", name="fact_a")
    table = NS(
        id="table",
        datasource_id="ds",
        catalog_name="catalog",
        db_name="schema_a",
        name="dimension_a",
        status="active",
        current_version_id="table_v1",
    )
    upstream = NS(id="upstream")
    source = NS(id="ds")
    version = NS(
        id="metric_v1",
        kind="base",
        status="published",
        dimensions=[{"name": "dimension_a", "column": "b.member_a"}],
        timezone="UTC",
    )
    path = NS(
        id="path_a",
        code="path_a",
        left_asset_id=fact.id,
        left_columns=["member_a"],
        right_asset_id=table.id,
        right_columns=["key_a"],
        cardinality="n1",
        status="approved",
        dependency=[],
    )
    columns = [
        NS(name="key_a", data_type="varchar(64)", ordinal=1),
        NS(name="label_a", data_type="text", ordinal=2),
        NS(name="amount_a", data_type="decimal(10,2)", ordinal=3),
    ]
    state = NS(
        sample={"dimension_a": ["MemberA", "MemberB"]},
        paths=[path],
        columns=columns,
        rows=[[["MemberA"]], [[0, 1]]],
        queries=[],
        events=[],
        denied=set(),
        drift=[],
        live=[],
        table_approved=True,
        exact=set(),
        exact_calls=[],
        error=None,
        truncated=False,
        remaining=60.0,
        query_cost=0.0,
    )
    stored = {(Asset, table.id): table, (Datasource, "ds"): source}

    def scalars(statement):
        entity = statement.column_descriptions[0]["entity"]
        return state.paths if entity is JoinPath else state.columns if entity is ColumnVersion else []

    session = NS(get=lambda model, key: stored.get((model, key)), scalars=scalars)
    monkeypatch.setattr(service, "dependency_drift", lambda *args: state.drift)
    monkeypatch.setattr(service, "_dimension_values", lambda *args, **kwargs: state.sample)
    monkeypatch.setattr(service, "validate_spec", lambda *args: {"aliases": {"b": fact}})
    monkeypatch.setattr(
        service, "upstream_closure", lambda *args: {fact.id: fact, table.id: table, upstream.id: upstream}
    )
    monkeypatch.setattr(service.access, "domain_ids", lambda *args: {"domain_a"})
    monkeypatch.setattr(
        service.access, "can_read_asset", lambda session, user, asset, allowed: asset.id not in state.denied
    )
    monkeypatch.setattr(
        alignment, "effectively_approved", lambda session, p: p.status == "approved" and state.table_approved
    )
    monkeypatch.setattr(service, "engine_of", lambda *args: NS(supports=lambda capability: True))
    monkeypatch.setattr(service, "dialect_of", lambda *args: SqlDialect("postgres", "catalog", catalog_in_sql=False))

    def exact(session, user, metric, dimension, values, settings, **kwargs):
        state.exact_calls.append(values)
        return state.exact

    monkeypatch.setattr(service, "_existing_dimension_values", exact)

    def execute(sql, **limits):
        state.events.append("execute")
        state.queries.append((sql, limits))
        state.remaining -= state.query_cost
        if state.error:
            raise state.error
        return NS(rows=state.rows.pop(0), truncated=state.truncated)

    def live(*args):
        state.events.append("live")
        return state.live

    monkeypatch.setattr(service, "query_connector", lambda *args: NS(execute_select=execute))
    monkeypatch.setattr(service, "_live_check", live)
    validate = service.validate_sql

    def checked(sql, **kwargs):
        state.events.append("validate")
        return validate(sql, **kwargs)

    monkeypatch.setattr(service, "validate_sql", checked)

    def align(values, metric=version, *, remaining_seconds=None):
        return alignment.align_filters(
            session,
            NS(id="user_a"),
            metric,
            [DimensionFilter(dimension="dimension_a", values=values)],
            NS(business_timezone="UTC"),
            remaining_seconds=remaining_seconds,
        )

    return NS(
        state=state, align=align, session=session, version=version, fact=fact, table=table, path=path, stored=stored
    )


def test_exact_preserves_input_and_records_each_value(world):
    result = world.align(["MemberA", "MemberA", "MemberB"])
    assert result.filters[0].values == ["MemberA", "MemberA", "MemberB"]
    assert [item["method"] for item in result.explanations] == ["exact"] * 3
    assert not result.unresolved and not result.unknown and not world.state.queries


@pytest.mark.parametrize("value", ["ＭｅｍｂｅｒＡ", "  Member A\t", "mEmBeRa", "\u3000Ｍｅｍｂｅｒ Ａ\n"])
def test_generic_normalization(world, value):
    result = world.align([value])
    assert result.filters[0].values == ["MemberA"]
    assert result.explanations == [
        {"dimension": "dimension_a", "original": value, "value": "MemberA", "method": "normalized"}
    ]
    assert not world.state.queries


@pytest.mark.parametrize("members", [["Member A", "MemberA"], ["MemberA", "membera"], ["ＭｅｍｂｅｒＡ", "MemberA"]])
def test_normalization_collision_does_not_choose(world, members):
    world.state.sample = {"dimension_a": members}
    world.state.paths = []
    result = world.align(["mEmBeRa"])
    assert result.filters[0].values == ["mEmBeRa"]
    assert result.unresolved[0]["candidates"] == members
    assert not result.explanations and not result.unknown


def test_containment_is_candidate_only(world):
    world.state.sample = {"dimension_a": ["Member", "Unrelated"]}
    world.state.paths = []
    result = world.align(["MemberAlias"])
    assert result.filters[0].values == ["MemberAlias"]
    assert result.unresolved == [
        {"dimension": "dimension_a", "original": "MemberAlias", "candidates": ["Member"], "has_dimension_table": False}
    ]
    assert not result.explanations


def test_unmatched_candidates_fall_back_to_ten_samples(world):
    world.state.sample = {"dimension_a": [f"Sample{i}" for i in range(15)]}
    world.state.paths = []
    assert world.align(["Different"]).unresolved[0]["candidates"] == [f"Sample{i}" for i in range(10)]


@pytest.mark.parametrize("reverse,cardinality", [(False, "n1"), (False, "11"), (True, "1n"), (True, "11")])
def test_approved_single_key_dimension_table_aligns_in_both_directions(world, reverse, cardinality):
    if reverse:
        world.path.left_asset_id, world.path.right_asset_id = world.path.right_asset_id, world.path.left_asset_id
        world.path.left_columns, world.path.right_columns = world.path.right_columns, world.path.left_columns
    world.path.cardinality = cardinality
    result = world.align(["AliasA"])
    assert result.filters[0].values == ["MemberA"]
    assert result.explanations == [
        {
            "dimension": "dimension_a",
            "original": "AliasA",
            "value": "MemberA",
            "method": "dimension_table",
            "asset_id": "table",
            "table": "catalog.schema_a.dimension_a",
            "column": "label_a",
        }
    ]
    assert world.state.events == ["validate", "live", "execute"] * 2
    assert all(
        limits == {"timeout_seconds": 10, "mem_limit_bytes": 512 * 1024**2, "max_rows": 3, "timezone": "UTC"}
        for _, limits in world.state.queries
    )
    tree = sqlglot.parse_one(world.state.queries[0][0], read="postgres")
    assert len(tree.expressions) == 1 and tree.expressions[0].name == "key_a"
    assert {column.name for column in tree.find(exp.Where).find_all(exp.Column)} == {"key_a", "label_a"}


def test_sql_literal_is_escaped_and_unicode_keys_use_national_strings(world, monkeypatch):
    monkeypatch.setattr(
        service, "dialect_of", lambda *args: SqlDialect("tsql", "catalog", catalog_in_sql=False, national_strings=True)
    )
    original = "Alias' OR 1=1 --"
    world.state.rows = [[["成员甲"]], [[0, 1]]]
    result = world.align([original])
    assert result.filters[0].values == ["成员甲"]
    for sql, _ in world.state.queries:
        assert "N'" in sql
        tree = sqlglot.parse_one(sql, read="tsql")
        assert original in [literal.this for literal in tree.find_all(exp.National)]
    assert "成员甲" in [
        literal.this for literal in sqlglot.parse_one(world.state.queries[1][0], read="tsql").find_all(exp.National)
    ]


@pytest.mark.parametrize(
    "data_type",
    [
        "VARCHAR2(64)",
        "NVARCHAR2(64)",
        "CHARACTER VARYING(32)",
        "STRING",
        "CLOB",
        "citext",
        "LowCardinality(String)",
        "LowCardinality(Nullable(String))",
        "Nullable(String)",
        "FixedString(32)",
        "Nullable(LowCardinality(FixedString(32)))",
    ],
)
def test_collected_text_types_are_used(world, data_type):
    world.state.columns[1].data_type = data_type
    assert world.align(["AliasA"]).filters[0].values == ["MemberA"]


def test_text_key_can_itself_match(world):
    world.state.rows = [[["KeyOutsideSample"]], [[1, 0]]]
    result = world.align(["KeyOutsideSample"])
    assert result.filters[0].values == ["KeyOutsideSample"]
    assert result.explanations[0]["column"] == "key_a"


def test_multiple_dimension_keys_are_candidates(world):
    world.state.rows = [[["KeyA"], ["KeyB"]]]
    result = world.align(["SharedAlias"])
    assert result.filters[0].values == ["SharedAlias"]
    assert result.unresolved[0]["candidates"] == ["KeyA", "KeyB"]
    assert result.unresolved[0]["has_dimension_table"] is True
    assert not result.explanations and len(world.state.queries) == 1


@pytest.mark.parametrize(
    "guard",
    [
        "table_permission",
        "source_permission",
        "closure_permission",
        "pending",
        "missing_approval",
        "composite_left",
        "composite_right",
        "nonunique",
        "reverse_nonunique",
        "wrong_column",
        "deleted",
        "nontext",
    ],
)
def test_unusable_paths_are_skipped_without_query(world, guard):
    if guard.endswith("permission"):
        world.state.denied.add(
            {"table_permission": "table", "source_permission": "fact", "closure_permission": "upstream"}[guard]
        )
    elif guard == "pending":
        world.path.status = "pending"
    elif guard == "missing_approval":
        world.state.table_approved = False
    elif guard == "composite_left":
        world.path.left_columns.append("other_a")
    elif guard == "composite_right":
        world.path.right_columns.append("other_a")
    elif guard == "nonunique":
        world.path.cardinality = "1n"
    elif guard == "reverse_nonunique":
        world.path.left_asset_id, world.path.right_asset_id = world.path.right_asset_id, world.path.left_asset_id
        world.path.left_columns, world.path.right_columns = world.path.right_columns, world.path.left_columns
    elif guard == "wrong_column":
        world.path.left_columns = ["other_a"]
    elif guard == "deleted":
        world.table.status = "deleted"
    else:
        world.state.columns = [
            NS(name="key_a", data_type="INTEGER", ordinal=1),
            NS(name="blob_a", data_type="JSONB", ordinal=2),
        ]
    result = world.align(["AliasA"])
    assert result.unresolved[0]["has_dimension_table"] is False
    assert result.filters[0].values == ["AliasA"]
    assert not world.state.queries


@pytest.mark.parametrize("exact,expected_unknown", [({"OutsideSample"}, False), (None, True)])
def test_truncated_sample_uses_exact_existence_without_claiming_absence(world, exact, expected_unknown):
    world.state.sample = {"dimension_a": [f"Sample{i}" for i in range(service.DIMENSION_VALUES_LIMIT)]}
    world.state.exact = exact
    result = world.align(["OutsideSample"])
    assert result.filters[0].values == ["OutsideSample"] and not result.unresolved
    assert bool(result.unknown) is expected_unknown
    assert world.state.exact_calls == [["OutsideSample"]]
    assert not world.state.queries


def test_truncated_sample_missing_exact_still_uses_dimension_table(world):
    world.state.sample = {"dimension_a": [f"Sample{i}" for i in range(service.DIMENSION_VALUES_LIMIT)]}
    result = world.align(["AliasA"])
    assert result.filters[0].values == ["MemberA"]
    assert world.state.exact_calls == [["AliasA"]]
    assert result.explanations[0]["method"] == "dimension_table"


@pytest.mark.parametrize("members", [("ACME", "acme"), ("ACME", "ＡＣＭＥ"), ("ACME", "AC ME")])
@pytest.mark.parametrize("omitted", [0, 1])
def test_truncated_normalization_collision_requires_confirmation(world, members, omitted):
    # 201 个实际成员，只取 200 个；两个同形成员轮流落在样本外。
    all_members = [f"AcmeAlias{i}" for i in range(service.DIMENSION_VALUES_LIMIT - 1)] + list(members)
    world.state.sample = {"dimension_a": [value for value in all_members if value != members[omitted]]}
    world.state.paths = []

    result = world.align(["Acme"])

    assert len(all_members) == 201 and len(world.state.sample["dimension_a"]) == service.DIMENSION_VALUES_LIMIT
    assert result.filters[0].values == ["Acme"]
    assert not result.explanations and not result.unknown
    assert result.unresolved == [
        {
            "dimension": "dimension_a",
            "original": "Acme",
            "candidates": [members[1 - omitted], *all_members[:9]],
            "has_dimension_table": False,
        }
    ]
    assert world.state.exact_calls == [["Acme"]]
    assert not world.state.queries


def test_complete_sample_with_unique_normalized_match_still_aligns(world):
    world.state.sample = {"dimension_a": ["ACME", *[f"Sample{i}" for i in range(service.DIMENSION_VALUES_LIMIT - 2)]]}

    result = world.align(["Acme"])

    assert result.filters[0].values == ["ACME"]
    assert result.explanations == [
        {"dimension": "dimension_a", "original": "Acme", "value": "ACME", "method": "normalized"}
    ]
    assert not result.unresolved and not result.unknown
    assert not world.state.exact_calls and not world.state.queries


@pytest.mark.parametrize("in_sample", [False, True])
def test_truncated_sample_preserves_exact_before_normalized_match(world, in_sample):
    sample = ["ACME", *[f"Sample{i}" for i in range(service.DIMENSION_VALUES_LIMIT - 1)]]
    if in_sample:
        sample[-1] = "Acme"
    world.state.sample = {"dimension_a": sample}
    world.state.exact = {"Acme"}

    result = world.align(["Acme"])

    assert result.filters[0].values == ["Acme"]
    assert result.explanations == [{"dimension": "dimension_a", "original": "Acme", "value": "Acme", "method": "exact"}]
    assert not result.unresolved and not result.unknown and not world.state.queries
    assert world.state.exact_calls == ([] if in_sample else [["Acme"]])


def test_truncated_normalized_match_still_uses_dimension_table(world):
    world.state.sample = {"dimension_a": ["ALIASA", *[f"Sample{i}" for i in range(service.DIMENSION_VALUES_LIMIT - 1)]]}

    result = world.align(["AliasA"])

    assert result.filters[0].values == ["MemberA"]
    assert result.explanations[0]["method"] == "dimension_table"
    assert not result.unresolved and not result.unknown
    assert world.state.exact_calls == [["AliasA"]]
    assert len(world.state.queries) == 2


def test_truncated_normalized_match_is_kept_with_ambiguous_table_candidates(world):
    world.state.sample = {"dimension_a": ["ALIASA", *[f"Sample{i}" for i in range(service.DIMENSION_VALUES_LIMIT - 1)]]}
    world.state.rows = [[["MemberA"], ["MemberB"]]]

    result = world.align(["AliasA"])

    assert result.filters[0].values == ["AliasA"]
    assert not result.explanations and not result.unknown
    assert result.unresolved[0]["candidates"] == ["MemberA", "MemberB", "ALIASA"]
    assert result.unresolved[0]["has_dimension_table"] is True


@pytest.mark.parametrize(
    "guard",
    ["version", "drift", "sample", "query", "live", "validation", "truncated", "evidence_query", "evidence_shape"],
)
def test_failed_checks_preserve_original_as_unknown(world, monkeypatch, guard):
    if guard == "version":
        world.version.status = "affected"
    elif guard == "drift":
        world.state.drift = [{"column": "member_a"}]
    elif guard == "sample":
        world.state.sample = {}
    elif guard == "query":
        world.state.error = QueryError("timeout", None, "timeout")
    elif guard == "live":
        world.state.live = [{"asset_id": "table"}]
    elif guard == "validation":

        def fail(*args, **kwargs):
            raise service.ValidationFailed(["denied"])

        monkeypatch.setattr(service, "validate_sql", fail)
    elif guard == "truncated":
        world.state.truncated = True
    elif guard == "evidence_query":
        world.state.rows = [[["MemberA"]], []]
    else:
        world.state.rows = [[["MemberA"]], [[2, 0]]]
    result = world.align(["AliasA"])
    assert result.filters[0].values == ["AliasA"]
    assert result.unknown and not result.unresolved and not result.explanations


def test_derived_metric_resolves_governed_tables_of_pinned_components(world):
    second = NS(**{**vars(world.version), "id": "metric_v2"})
    derived = NS(
        id="derived_v1",
        kind="derived",
        status="published",
        dimensions=[{"name": "dimension_a"}],
        components=[{"version_id": world.version.id}, {"version_id": second.id}],
    )
    world.stored[(MetricVersion, world.version.id)] = world.version
    world.stored[(MetricVersion, second.id)] = second
    world.state.rows = [[["MemberA"]], [[0, 1]], [["MemberA"]], [[0, 1]]]
    result = world.align(["AliasA"], derived)
    assert result.filters[0].values == ["MemberA"]
    assert result.explanations[0]["method"] == "dimension_table" and not result.unresolved


def test_derived_metric_conflicting_component_keys_require_confirmation(world):
    second = NS(**{**vars(world.version), "id": "metric_v2"})
    derived = NS(
        kind="derived",
        status="published",
        dimensions=[{"name": "dimension_a"}],
        components=[{"version_id": world.version.id}, {"version_id": second.id}],
    )
    world.stored[(MetricVersion, world.version.id)] = world.version
    world.stored[(MetricVersion, second.id)] = second
    world.state.rows = [[["MemberA"]], [[0, 1]], [["MemberB"]], [[0, 1]]]
    result = world.align(["AliasA"], derived)
    assert result.filters[0].values == ["AliasA"]
    assert result.unresolved[0]["candidates"] == ["MemberA", "MemberB"]
    assert result.unresolved[0]["candidate_sources"] == {"MemberA": ["table"], "MemberB": ["table"]}
    assert not result.explanations


def test_input_filters_are_not_mutated(world):
    filters = [DimensionFilter(dimension="dimension_a", values=["membera"])]
    result = alignment.align_filters(world.session, NS(id="u"), world.version, filters, NS())
    assert filters[0].values == ["membera"] and result.filters[0].values == ["MemberA"]


@pytest.mark.parametrize("stage", ["sample", "connector", "exact", "version"])
@pytest.mark.parametrize(
    "failure", [QueryError("timeout", None, "timeout"), CredentialMissing("missing"), UnsupportedEngine("missing")]
)
def test_query_setup_and_guard_failures_are_unknown(world, monkeypatch, stage, failure):
    def fail(*args, **kwargs):
        raise failure

    target = {
        "sample": "_dimension_values",
        "connector": "query_connector",
        "exact": "_existing_dimension_values",
        "version": "dependency_drift",
    }[stage]
    monkeypatch.setattr(service, target, fail)
    if stage == "exact":
        world.state.sample = {"dimension_a": [f"Sample{i}" for i in range(service.DIMENSION_VALUES_LIMIT)]}
    result = world.align(["AliasA"])
    assert result.filters[0].values == ["AliasA"]
    assert result.unknown and not result.unresolved and not result.explanations


def test_empty_dimension_table_match_preserves_member_candidates(world):
    world.state.rows = [[]]
    result = world.align(["AliasA"])
    assert result.unresolved == [
        {
            "dimension": "dimension_a",
            "original": "AliasA",
            "candidates": ["MemberA", "MemberB"],
            "has_dimension_table": True,
        }
    ]


def test_paths_with_conflicting_unique_keys_are_not_automatically_chosen(world):
    world.state.paths.append(NS(**{**vars(world.path), "id": "path_b", "code": "path_b"}))
    world.state.rows = [[["MemberA"]], [[0, 1]], [["MemberB"]], [[0, 1]]]
    result = world.align(["AliasA"])
    assert result.filters[0].values == ["AliasA"]
    assert result.unresolved[0]["candidates"] == ["MemberA", "MemberB"]
    assert not result.explanations


@pytest.mark.parametrize("sample_a,sample_b", [(["MemberA"], ["membera"]), (["Member A"], ["membera"])])
def test_derived_canonical_conflict_reports_available_table_without_table_explanations(
    world, monkeypatch, sample_a, sample_b
):
    second = NS(**{**vars(world.version), "id": "metric_v2"})
    derived = NS(
        id="derived_v1",
        kind="derived",
        status="published",
        dimensions=[{"name": "dimension_a"}],
        components=[{"version_id": world.version.id}, {"version_id": second.id}],
    )
    world.stored[(MetricVersion, world.version.id)] = world.version
    world.stored[(MetricVersion, second.id)] = second
    monkeypatch.setattr(
        service,
        "_dimension_values",
        lambda session, user, version, settings: {
            "dimension_a": sample_a if version.id == world.version.id else sample_b
        },
    )
    result = world.align(["MemberA"], derived)
    assert result.filters[0].values == ["MemberA"]
    assert result.unresolved[0]["has_dimension_table"] is True
    assert not result.explanations and not world.state.queries


@pytest.mark.parametrize("guard", [None, "hidden", "unapproved", "composite", "nonunique", "version", "dimension"])
def test_public_table_availability_uses_the_same_governed_rules(world, guard):
    if guard == "hidden":
        world.state.denied.add("upstream")
    elif guard == "unapproved":
        world.state.table_approved = False
    elif guard == "composite":
        world.path.right_columns.append("other")
    elif guard == "nonunique":
        world.path.cardinality = "nn"
    elif guard == "version":
        world.version.status = "affected"
    dimension = "other_dimension" if guard == "dimension" else "dimension_a"
    assert alignment.has_dimension_table(world.session, NS(id="u"), world.version, dimension) is (guard is None)
    assert not world.state.queries


def test_public_table_availability_handles_guard_failure(world, monkeypatch):
    def fail(*args):
        raise CredentialMissing("missing")

    monkeypatch.setattr(service, "dependency_drift", fail)
    assert alignment.has_dimension_table(world.session, NS(id="u"), world.version, "dimension_a") is False


def test_exhausted_budget_keeps_all_values_unknown_without_sampling(world, monkeypatch):
    monkeypatch.setattr(service, "_dimension_values", lambda *args, **kwargs: pytest.fail("预算耗尽后不再取样"))
    result = world.align(["membera", "AliasA"], remaining_seconds=lambda: 0)
    assert result.filters[0].values == ["membera", "AliasA"]
    assert result.unknown == [
        {"dimension": "dimension_a", "original": value, "reason": "budget_exhausted"} for value in ["membera", "AliasA"]
    ]
    assert not result.unresolved and not result.explanations and not world.state.queries


def test_each_table_query_uses_remaining_budget_and_stops_between_values(world):
    world.state.remaining = 3.0
    world.state.query_cost = 1.0
    world.state.rows = [[["MemberA"]], [[0, 1]], [["MemberB"]]]
    result = world.align(["AliasA", "AliasB", "memberb"], remaining_seconds=lambda: world.state.remaining)
    assert result.filters[0].values == ["MemberA", "AliasB", "memberb"]
    assert [limits["timeout_seconds"] for _, limits in world.state.queries] == [3.0, 2.0, 1.0]
    assert [item["original"] for item in result.explanations] == ["AliasA"]
    assert result.unknown == [
        {"dimension": "dimension_a", "original": value, "reason": "budget_exhausted"} for value in ["AliasB", "memberb"]
    ]
    assert not result.unresolved


def test_alignment_keeps_existing_query_limit_when_budget_is_larger(world):
    world.align(["AliasA"], remaining_seconds=lambda: 20)
    assert [limits["timeout_seconds"] for _, limits in world.state.queries] == [10, 10]


def test_budget_is_rechecked_after_table_live_check(world, monkeypatch):
    def live(*args):
        world.state.remaining = 0
        return []

    monkeypatch.setattr(service, "_live_check", live)
    result = world.align(["AliasA"], remaining_seconds=lambda: world.state.remaining)
    assert result.filters[0].values == ["AliasA"]
    assert result.unknown[0]["reason"] == "budget_exhausted"
    assert not world.state.queries and not result.unresolved


def test_derived_components_share_budget_and_exhaustion_overrides_pending(world):
    second = NS(**{**vars(world.version), "id": "metric_v2"})
    derived = NS(
        id="derived_v1",
        kind="derived",
        status="published",
        dimensions=[{"name": "dimension_a"}],
        components=[{"version_id": world.version.id}, {"version_id": second.id}],
    )
    world.stored[(MetricVersion, world.version.id)] = world.version
    world.stored[(MetricVersion, second.id)] = second
    world.state.remaining = 1
    world.state.query_cost = 1
    world.state.rows = [[["MemberA"], ["MemberB"]]]
    result = world.align(["AliasA"], derived, remaining_seconds=lambda: world.state.remaining)
    assert result.filters[0].values == ["AliasA"]
    assert result.unknown == [{"dimension": "dimension_a", "original": "AliasA", "reason": "budget_exhausted"}]
    assert not result.unresolved and not result.explanations
    assert len(world.state.queries) == 1


@pytest.mark.parametrize(
    "data_type",
    [
        "text[]",
        "character varying(32)[]",
        "varchar(20)[][]",
        "_text",
        "_varchar",
        "ARRAY(text)",
        "Array(String)",
        "ARRAY<VARCHAR(32)>",
        "Nullable(Array(String))",
        "LowCardinality(Nullable(text[]))",
    ],
)
def test_array_columns_are_excluded_before_matching_text(world, data_type):
    world.state.columns.append(NS(name="tags", data_type=data_type, ordinal=4))
    result = world.align(["AliasA"])
    assert result.filters[0].values == ["MemberA"] and not result.unknown
    assert all('"tags"' not in sql for sql, _ in world.state.queries)


def test_multiple_outside_sample_values_use_one_batched_existence_query_per_dimension(world):
    world.state.sample = {"dimension_a": [f"Sample{i}" for i in range(service.DIMENSION_VALUES_LIMIT)]}
    world.state.paths = []
    world.state.exact = {"OutsideA", "OutsideB"}
    filters = [
        DimensionFilter(dimension="dimension_a", values=["OutsideA", "OutsideB", "OutsideA"]),
        DimensionFilter(dimension="dimension_a", values=["OutsideC", "OutsideB"]),
    ]
    result = alignment.align_filters(world.session, NS(id="u"), world.version, filters, NS())
    assert world.state.exact_calls == [["OutsideA", "OutsideB", "OutsideC"]]
    assert [e["original"] for e in result.explanations] == ["OutsideA", "OutsideB", "OutsideA", "OutsideB"]
    assert [u["original"] for u in result.unresolved] == ["OutsideC"]
    assert not result.unknown


def test_batched_existence_query_uses_budget_and_stops_after_exhaustion(world, monkeypatch):
    world.state.sample = {"dimension_a": [f"Sample{i}" for i in range(service.DIMENSION_VALUES_LIMIT)]}
    calls = []

    def exact(*args, remaining_seconds):
        calls.append((args[4], remaining_seconds()))
        world.state.remaining = 0
        return {"OutsideA"}

    monkeypatch.setattr(service, "_existing_dimension_values", exact)
    result = world.align(["OutsideA", "OutsideB"], remaining_seconds=lambda: world.state.remaining)
    assert calls == [(["OutsideA", "OutsideB"], 60)]
    assert [u["reason"] for u in result.unknown] == ["budget_exhausted"] * 2
    assert not world.state.queries


def test_multikey_candidates_keep_sources(world):
    world.state.rows = [[["KeyA"], ["KeyB"]]]
    result = world.align(["SharedAlias"])
    assert result.unresolved[0]["candidate_sources"] == {"KeyA": ["table"], "KeyB": ["table"]}
