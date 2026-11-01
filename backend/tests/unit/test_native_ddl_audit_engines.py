"""SQL Server / Oracle 原生审计的能力与三语客户前提说明。"""

import pytest

from avelis.connectors.ddl import mentions_column
from avelis.engines import get_engine
from avelis.engines.base import DDL_AUDIT
from avelis.i18n import locale_context


@pytest.mark.parametrize(
    "kind,source",
    [
        ("sqlserver", "default_trace"),
        ("oracle", "UNIFIED_AUDIT_TRAIL"),
    ],
)
def test_native_audit_requires_no_table_binding(kind, source):
    engine = get_engine(kind)
    assert engine.supports(DDL_AUDIT)
    assert callable(engine.collector.ddl_events)
    assert source in engine.collector.ddl_audit_source
    assert "ddl_audit_table" not in {option.key for option in engine.options}


@pytest.mark.parametrize(
    "kind,requirements",
    [
        ("sqlserver", ("ALTER TRACE", "VIEW SERVER STATE", "StartTime", "UTC")),
        ("oracle", ("AUDIT_VIEWER", "RETURN_CODE", "EVENT_TIMESTAMP_UTC", "EVENT_TIMESTAMP")),
    ],
)
@pytest.mark.parametrize("locale", ["zh-CN", "zh-TW", "en"])
def test_native_audit_requirements_are_translated(kind, requirements, locale):
    engine = get_engine(kind)
    with locale_context(locale):
        description = engine.describe()
    assert DDL_AUDIT in description["capabilities"]
    assert all(term in description["note"] for term in requirements)
    if locale != "zh-CN":
        assert description["note"] != engine.note


def test_oracle_column_evidence_folds_only_unquoted_identifiers():
    statement = 'ALTER TABLE store.sales ADD (amount NUMBER, "Mixed" NUMBER)'
    assert mentions_column(statement, "AMOUNT", "SALES", "upper", "STORE")
    assert not mentions_column(statement, "amount", "SALES", "upper", "STORE")
    assert mentions_column(statement, "Mixed", "SALES", "upper", "STORE")
    assert not mentions_column(statement, "MIXED", "SALES", "upper", "STORE")
    assert not mentions_column("ALTER TABLE store.sales ADD mixed NUMBER", "Mixed", "SALES", "upper", "STORE")
