"""目录检索在数据库内筛选、分页，并批量读取最新治理版本。"""

from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from sqlalchemy import event, select
from sqlalchemy.dialects import postgresql

from avelis.ask import service
from avelis.governance.metrics import metric_ai_context
from avelis.models import Domain, DomainMember, Metric, MetricVersion
from tests.unit.test_mcp_tools import toolbox  # noqa: F401


def _add_metric(session, metric_id, code, *, domain_id="visible", name="Scale metric"):
    session.add(
        Metric(
            id=metric_id,
            code=code,
            name=name,
            domain_id=domain_id,
            owner_id="reader",
            created_by="reader",
        )
    )
    _add_version(session, metric_id, 1, "published")


def _add_version(session, metric_id, number, status):
    session.add(
        MetricVersion(
            id=f"{metric_id}-v{number}",
            metric_id=metric_id,
            version_no=number,
            status=status,
            description=f"Version {number}",
            base_asset_id="asset",
            expression="SUM(b.value_a)",
            time_column="b.day_a",
            timezone="UTC",
            created_by="reader",
        )
    )


@contextmanager
def _catalog_reads(session):
    """清空 identity map，分别统计 SQL 往返和实际载入的 ORM 对象。"""
    session.flush()
    session.expunge_all()
    stats = SimpleNamespace(selects=[], metrics=[], versions=[])
    engine = session.get_bind()

    def before_cursor_execute(connection, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            stats.selects.append(statement)

    def loaded_as_persistent(session, instance):
        if isinstance(instance, Metric):
            stats.metrics.append(instance.id)
        elif isinstance(instance, MetricVersion):
            stats.versions.append(instance.id)

    event.listen(engine, "before_cursor_execute", before_cursor_execute)
    event.listen(session, "loaded_as_persistent", loaded_as_persistent)
    try:
        yield stats
    finally:
        event.remove(session, "loaded_as_persistent", loaded_as_persistent)
        event.remove(engine, "before_cursor_execute", before_cursor_execute)


def test_mcp_limit_bounds_queries_and_loaded_metrics_as_catalog_grows(toolbox):
    _add_metric(toolbox.session, "scale-01", "scale_01")
    _add_version(toolbox.session, "scale-01", 2, "published")
    _add_version(toolbox.session, "scale-01", 3, "draft")
    with _catalog_reads(toolbox.session) as small:
        first = toolbox.tool.search_metrics(limit=1)
    for number in range(2, 50):
        metric_id = f"scale-{number:02}"
        _add_metric(toolbox.session, metric_id, f"scale_{number:02}")
        _add_version(toolbox.session, metric_id, 2, "published")
        _add_version(toolbox.session, metric_id, 3, "draft")
    with _catalog_reads(toolbox.session) as large:
        assert toolbox.tool.search_metrics(limit=1) == first
    assert len(small.selects) == len(large.selects) > 0
    assert len(large.selects) <= 3
    assert len(small.metrics) == len(large.metrics) == 1
    assert len(small.versions) == len(large.versions) == 1
    assert small.metrics == large.metrics == ["metric"]
    assert small.versions == large.versions == ["metric-v1"]


def test_visible_catalog_batches_latest_versions_as_catalog_grows(toolbox):
    _add_metric(toolbox.session, "scale-01", "scale_01")
    with _catalog_reads(toolbox.session) as small:
        assert len(service.visible_catalog(toolbox.session, toolbox.user, with_values=False).cards) == 2
    for number in range(2, 50):
        _add_metric(toolbox.session, f"scale-{number:02}", f"scale_{number:02}")
    with _catalog_reads(toolbox.session) as large:
        catalog = service.visible_catalog(toolbox.session, toolbox.user, with_values=False)
    assert len(catalog.cards) == len(catalog.versions) == 50
    assert len(small.selects) == len(large.selects) > 0
    assert len(large.selects) <= 3
    assert len(small.metrics) == len(small.versions) == 2
    assert len(large.metrics) == len(large.versions) == 50


@pytest.mark.parametrize("latest_status", ["published", "affected", "retired"])
def test_latest_governed_version_ignores_drafts_without_falling_back_to_published(toolbox, latest_status):
    _add_version(toolbox.session, "metric", 2, latest_status)
    _add_version(toolbox.session, "metric", 3, "draft")
    _add_version(toolbox.session, "metric", 4, "pending_review")
    toolbox.session.flush()
    rows = service.metric_catalog_rows(toolbox.session, toolbox.user)
    assert [(metric.code, version.id, domain.id) for metric, version, domain in rows] == [
        ("metric_a", "metric-v2", "visible")
    ]
    catalog = service.visible_catalog(toolbox.session, toolbox.user, with_values=False)
    assert catalog.versions["metric_a"][1].id == "metric-v2"
    assert catalog.cards[0].status == latest_status
    result = toolbox.tool.search_metrics(limit=1)
    assert [row["code"] for row in result] == (["metric_a"] if latest_status == "published" else [])


def test_permission_domain_and_keyword_filter_before_stable_pagination(toolbox):
    toolbox.session.add(Domain(id="second", code="second", name="Second domain", owner_id="reader"))
    toolbox.session.add(DomainMember(user_id="reader", domain_id="second", role="viewer"))
    for metric_id, code, domain, name in (
        ("hidden-first", "000_hidden", "hidden", "Page needle"),
        ("other-domain", "001_other", "second", "Page needle"),
        ("nonmatch", "002_nonmatch", "visible", "Unrelated"),
        ("page-z", "page_z", "visible", "Page needle"),
        ("page-a", "page_a", "visible", "Page needle"),
        ("page-m", "page_m", "visible", "Page needle"),
    ):
        _add_metric(toolbox.session, metric_id, code, domain_id=domain, name=name)
    toolbox.session.flush()

    def page(offset, domain_id="visible"):
        return [
            metric.code
            for metric, _, _ in service.metric_catalog_rows(
                toolbox.session, toolbox.user, keyword="needle", domain_id=domain_id, limit=1, offset=offset
            )
        ]

    assert [page(offset) for offset in range(4)] == [["page_a"], ["page_m"], ["page_z"], []]
    assert page(0) == ["page_a"]
    assert page(0, "second") == ["001_other"]
    assert page(0, "hidden") == []
    assert [page(offset, None) for offset in range(5)] == [
        ["001_other"],
        ["page_a"],
        ["page_m"],
        ["page_z"],
        [],
    ]
    assert [row["code"] for row in toolbox.tool.search_metrics("needle", "visible", limit=1)] == ["page_a"]
    assert toolbox.tool.search_metrics("needle", "hidden", limit=1) == []


@pytest.mark.parametrize(
    "keyword",
    [
        "",
        "metric_literal",
        "literal",
        "LITERAL",
        "ReadableName",
        "readablename",
        "CaseSensitive",
        "Alias Needle",
        "alias needle",
        "SynonymNeedle",
        "多语言同义词",
        "ExampleNeedle",
        '"quoted"',
        r"\"quoted\"",
        "percent%_marker",
        "%",
        "Read%",
        "metric_liter_l",
        r"back\slash",
        r"back\\slash",
        "synonyms",
        "examples",
        "instructions",
        "instruction_secret_not_searchable",
        'Needle", "',
    ],
)
def test_keyword_matches_original_literal_substring_semantics(toolbox, keyword):
    metric = toolbox.session.get(Metric, "metric")
    metric.code = "metric_literal"
    metric.name = "ReadableName CaseSensitive"
    metric.aliases = ["Alias Needle", 'json "quoted"', "percent%_marker", r"back\slash"]
    metric.ai_context = {
        "instructions": "instruction_secret_not_searchable",
        "synonyms": ["SynonymNeedle", "多语言同义词"],
        "examples": ["ExampleNeedle"],
    }
    toolbox.session.flush()
    context = metric_ai_context(metric)
    terms = [metric.name, metric.code, *metric.aliases, *context["synonyms"], *context["examples"]]
    expected = [metric.code] if any(keyword in term for term in terms) else []
    assert [row["code"] for row in toolbox.tool.search_metrics(keyword, limit=1)] == expected


def test_postgresql_keyword_query_searches_decoded_json_array_elements_with_bound_literals():
    dialect = postgresql.dialect()
    session = SimpleNamespace(get_bind=lambda: SimpleNamespace(dialect=dialect))
    keyword = 'Needle%_"\\'
    query = select(Metric.id).where(service._metric_keyword_clause(session, keyword))
    compiled = query.compile(dialect=dialect)
    sql = str(compiled)
    assert sql.count("jsonb_array_elements_text(") == 3
    assert sql.count("strpos(") == 5
    assert " LIKE " not in sql and " ILIKE " not in sql
    assert keyword not in sql
    assert keyword in compiled.params.values()
    assert {"synonyms", "examples"} <= set(compiled.params.values())
