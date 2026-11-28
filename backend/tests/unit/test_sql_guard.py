"""共用禁止清单：问数校验、探索查询与已核验查询对同一批危险写法一致拒绝。"""

from __future__ import annotations

import pytest

from avelis.ask.compiler import ValidationFailed, validate_sql
from avelis.engines.builtin import HIVE, MYSQL
from avelis.governance.errors import Invalid
from avelis.governance.vqr import validate_vqr_sql
from avelis.sql_guard import BANNED_FUNCTIONS, banned_constructs
from tests.unit.test_analysis_explore import ENGINES  # noqa: F401 - 确认探索测试的解析器可用
from tests.unit.test_analysis_explore import ok as explore_ok

HIVE_DANGEROUS = [
    "SELECT TRANSFORM(id) USING 'rm -rf /' AS x FROM store.sales LIMIT 2",
    "SELECT reflect('java.lang.System', 'exit', 0) FROM store.sales LIMIT 2",
    "SELECT java_method('java.lang.Runtime', 'getRuntime') FROM store.sales LIMIT 2",
    "SELECT reflect2(id, 'toString') FROM store.sales LIMIT 2",
]


@pytest.mark.parametrize("sql", HIVE_DANGEROUS)
def test_ask_validation_rejects_hive_code_execution(sql):
    with pytest.raises(ValidationFailed, match="禁止的写法"):
        validate_sql(
            sql,
            dialect=HIVE.sql,
            allowed_tables={"hive.store.sales"},
            schema={"hive.store.sales": {"id"}},
            max_rows=2,
        )


@pytest.mark.parametrize("sql", HIVE_DANGEROUS)
def test_verified_queries_reject_hive_code_execution(sql):
    with pytest.raises(Invalid, match="禁止的写法"):
        validate_vqr_sql(sql, "hive")


def test_ask_validation_rejects_file_and_sleep_functions_on_mysql():
    for sql in (
        "SELECT LOAD_FILE('/etc/passwd') FROM sample.source LIMIT 2",
        "SELECT SLEEP(10) FROM sample.source LIMIT 2",
    ):
        with pytest.raises(ValidationFailed, match="禁止的写法"):
            validate_sql(
                sql,
                dialect=MYSQL.sql,
                allowed_tables={"def.sample.source"},
                schema={"def.sample.source": {"id"}},
                max_rows=2,
            )


def test_plain_select_passes_and_explore_still_bans_by_name():
    validate_sql(
        "SELECT id FROM store.sales LIMIT 2",
        dialect=HIVE.sql,
        allowed_tables={"hive.store.sales"},
        schema={"hive.store.sales": {"id"}},
        max_rows=2,
    )
    assert banned_constructs(__import__("sqlglot").parse_one("SELECT id FROM t", read="hive")) == []
    assert {"reflect", "reflect2", "java_method", "sleep", "load_file"} <= BANNED_FUNCTIONS
    from avelis.analysis.explore import ExploreRejected

    with pytest.raises(ExploreRejected, match="不允许使用"):
        explore_ok("hive", "SELECT reflect('a', 'b') AS x FROM d1.facts")
