"""导入语句按语法树记录多写入、结构事实与列级依赖。"""

import pytest
from sqlglot.errors import ParseError, TokenError

from avelis.importer.statements import parse_statement
from avelis.lineage.sql import SqlDialect, fold_identifiers


def parse(sql, name="postgres", *, known=True, default_db=None, line_start=1):
    schema = {"c": {"src": {"events": {"id": "INT", "value": "INT", "category": "VARCHAR"}},
                    "dw": {"daily_summary": {"id": "INT", "value": "INT"},
                           "stage": {"id": "INT", "value": "INT"}}}}
    if name == "oracle":
        schema = {catalog.upper(): {db.upper(): {table.upper(): {col.upper(): typ for col, typ in columns.items()}
                                                for table, columns in tables.items()}
                                    for db, tables in databases.items()}
                  for catalog, databases in schema.items()}
    dialect = SqlDialect(name, "C" if name == "oracle" else "c", normalize=fold_identifiers(name))
    return parse_statement(sql, dialect=dialect, schema=schema if known else {},
                           default_db=default_db, line_start=line_start)


def signatures(write):
    return {(e.source_ref.lower(), e.source_column.lower() if e.source_column else None,
             e.target_column.lower() if e.target_column else None, e.relation) for e in write.edges}


@pytest.mark.parametrize("name,sql,kind", [
    ("starrocks", "INSERT INTO dw.daily_summary (id, value) SELECT id, value FROM src.events", "INSERT"),
    ("hive", "INSERT OVERWRITE TABLE dw.daily_summary SELECT id, value FROM src.events", "INSERT OVERWRITE"),
    ("postgres", "WITH rows AS (SELECT id, value FROM src.events) "
                  "INSERT INTO dw.daily_summary SELECT id, value FROM rows", "INSERT"),
    ("mysql", "INSERT INTO dw.daily_summary SELECT id, value FROM src.events", "INSERT"),
    ("tsql", "INSERT INTO dw.daily_summary (id, value) SELECT id, value FROM src.events", "INSERT"),
    ("oracle", "INSERT INTO dw.daily_summary (id, value) SELECT id, value FROM src.events", "INSERT"),
])
def test_insert_dialects_and_front_cte(name, sql, kind):
    result = parse(sql, name)
    assert result.status == "parsed", result.reason
    assert result.statement_type == kind
    assert len(result.writes) == 1
    assert signatures(result.writes[0]) == {("c.src.events", "id", "id", "value_direct"),
                                           ("c.src.events", "value", "value", "value_direct")}
    assert result.target_ref == result.writes[0].target_ref
    assert result.edges is result.writes[0].edges


@pytest.mark.parametrize("sql,kind", [
    ("CREATE TABLE dw.snapshot AS SELECT id, value FROM src.events", "CREATE TABLE AS"),
    ("CREATE VIEW dw.snapshot AS SELECT id, value FROM src.events", "CREATE VIEW AS"),
    ("CREATE MATERIALIZED VIEW dw.snapshot AS SELECT id, value FROM src.events", "CREATE MATERIALIZED VIEW"),
    ("CREATE TEMPORARY TABLE dw.snapshot AS SELECT id, value FROM src.events", "CREATE TABLE AS"),
])
def test_create_query_records_creation_and_materialized_type(sql, kind):
    result = parse(sql)
    assert result.status == "parsed", result.reason
    assert result.statement_type == kind
    assert result.writes[0].created
    assert result.writes[0].temporary == ("TEMPORARY" in sql)
    assert result.target_columns == ["id", "value"]


@pytest.mark.parametrize("name,sql", [
    ("hive", "FROM src.events INSERT INTO dw.first SELECT id INSERT INTO dw.second SELECT value"),
    ("oracle", "INSERT ALL INTO dw.first(id) VALUES(id) INTO dw.second(value) VALUES(value) "
               "SELECT id,value FROM src.events"),
])
def test_multi_insert_has_one_write_per_branch(name, sql):
    result = parse(sql, name)
    assert result.status == "parsed", result.reason
    assert [w.target_ref.lower() for w in result.writes] == ["c.dw.first", "c.dw.second"]
    assert signatures(result.writes[0]) == {("c.src.events", "id", "id", "value_direct")}
    assert signatures(result.writes[1]) == {("c.src.events", "value", "value", "value_direct")}


def test_oracle_conditional_insert_keeps_branch_filters():
    result = parse("INSERT ALL WHEN value>0 THEN INTO dw.first(id) VALUES(id) "
                   "ELSE INTO dw.second(value) VALUES(value) SELECT id,value FROM src.events", "oracle")
    assert result.status == "parsed", result.reason
    assert len(result.writes) == 2
    assert all(("c.src.events", "value", None, "filter") in signatures(w) for w in result.writes)


@pytest.mark.parametrize("name,sql", [
    ("postgres", "UPDATE dw.daily_summary AS t SET value=s.value FROM src.events AS s WHERE t.id=s.id"),
    ("mysql", "UPDATE dw.daily_summary AS t JOIN src.events AS s ON t.id=s.id SET t.value=s.value"),
    ("tsql", "UPDATE t SET value=s.value FROM dw.daily_summary AS t JOIN src.events AS s ON t.id=s.id"),
])
def test_update_uses_query_lineage_and_resolves_target_alias(name, sql):
    result = parse(sql, name)
    assert result.status == "parsed", result.reason
    assert result.target_ref == "c.dw.daily_summary"
    assert result.target_columns == ["value"]
    assert ("c.src.events", "value", "value", "value_direct") in signatures(result.writes[0])
    if name == "postgres":
        assert ("c.src.events", "id", None, "filter") in signatures(result.writes[0])


def test_update_drops_only_same_column_self_dependency():
    result = parse("UPDATE dw.daily_summary AS t SET value=t.value+s.value "
                   "FROM src.events AS s WHERE t.id=s.id")
    edges = signatures(result.writes[0])
    assert result.status == "parsed", result.reason
    assert ("c.src.events", "value", "value", "value_computed") in edges
    assert ("c.dw.daily_summary", "value", "value", "value_computed") not in edges
    assert ("c.dw.daily_summary", "id", None, "filter") in edges


def test_update_scalar_subquery_preserves_parser_limit_without_inventing_column_edges():
    result = parse("UPDATE dw.daily_summary AS t SET value="
                   "(SELECT max(s.value) FROM src.events AS s WHERE s.id=t.id)")
    assert result.status == "partial"
    assert "标量子查询" in result.reason
    assert not any(e.target_column == "value" for e in result.edges)
    assert any(e.source_ref == "c.src.events" for e in result.edges)


@pytest.mark.parametrize("name", ["postgres", "tsql", "oracle", "starrocks"])
def test_merge_writes_each_branch_and_keeps_join_condition(name):
    result = parse("MERGE INTO dw.daily_summary t USING src.events s ON t.id=s.id "
                   "WHEN MATCHED AND s.value>0 THEN UPDATE SET value=t.value+s.value "
                   "WHEN NOT MATCHED THEN INSERT(id,value) VALUES(s.id,s.value)", name)
    assert result.status == "parsed", result.reason
    assert len(result.writes) == 2
    for write in result.writes:
        assert ("c.src.events", "id", None, "join_key") in signatures(write)
        assert not any(e.source_ref == write.target_ref and e.source_column == e.target_column
                       for e in write.edges if e.target_column)
    assert ("c.src.events", "value", None, "filter") in signatures(result.writes[0])
    assert signatures(result.writes[1]) >= {("c.src.events", "id", "id", "value_direct"),
                                           ("c.src.events", "value", "value", "value_direct")}


@pytest.mark.parametrize("name,sql", [
    ("mysql", "CREATE TABLE dw.clone LIKE src.events"),
    ("postgres", "CREATE TABLE dw.clone (LIKE src.events)"),
])
def test_create_like_copies_structure_without_data_dependency(name, sql):
    result = parse(sql, name)
    assert result.status == "parsed"
    assert result.statement_type == "CREATE TABLE LIKE"
    assert result.target_columns == ["id", "value", "category"]
    assert result.writes[0].created and not result.edges


def test_create_table_without_query_registers_declared_columns():
    result = parse("CREATE TEMP TABLE dw.stage (id INTEGER, value INTEGER)")
    assert result.status == "parsed"
    assert result.target_columns == ["id", "value"]
    assert result.writes[0].created and result.temporary and not result.edges


def test_procedure_keyword_in_column_name_does_not_turn_table_into_code_block():
    result = parse('CREATE TABLE stage ("function" TEXT)', default_db="dw")
    assert result.status == "parsed"
    assert result.target_columns == ["function"]


def test_rename_records_old_name_and_column_mapping():
    result = parse("ALTER TABLE dw.stage RENAME TO final_stage")
    assert result.status == "parsed"
    assert result.removed_refs == ["c.dw.stage"]
    assert result.target_ref == "c.dw.final_stage"
    assert signatures(result.writes[0]) == {("c.dw.stage", "id", "id", "value_direct"),
                                           ("c.dw.stage", "value", "value", "value_direct")}
    assert result.writes[0].attributes == {"op": "rename"}
    assert all(e.attributes == {"op": "rename"} for e in result.edges)


def test_rename_unknown_columns_is_table_level():
    result = parse("ALTER TABLE dw.unknown RENAME TO final_stage")
    assert result.status == "parsed"
    assert signatures(result.writes[0]) == {("c.dw.unknown", None, None, "table")}


def test_swap_records_two_directions_from_original_structures():
    result = parse("ALTER TABLE dw.stage SWAP WITH dw.daily_summary", "starrocks")
    assert result.status == "parsed"
    assert result.removed_refs == ["c.dw.stage", "c.dw.daily_summary"]
    assert [w.target_ref for w in result.writes] == ["c.dw.daily_summary", "c.dw.stage"]
    assert all(w.attributes == {"op": "swap"} for w in result.writes)
    assert {e.source_ref for e in result.writes[0].edges} == {"c.dw.stage"}
    assert {e.source_ref for e in result.writes[1].edges} == {"c.dw.daily_summary"}


def test_mysql_rename_command_is_reported_unsupported_without_preprocessing():
    result = parse("RENAME TABLE dw.stage TO dw.final_stage", "mysql")
    assert result.status == "unsupported" and not result.writes


def test_drop_is_script_fact_without_edges():
    result = parse("DROP TABLE dw.stage")
    assert result.status == "skipped"
    assert result.dropped_refs == ["c.dw.stage"]
    assert not result.writes and not result.edges


@pytest.mark.parametrize("sql", [
    "INSERT INTO dw.daily_summary(id,value) VALUES(1,2)",
    "DELETE FROM dw.daily_summary WHERE id=1", "TRUNCATE TABLE dw.daily_summary", "SET work_mem='1MB'",
    "SELECT id FROM src.events", "BEGIN;", "BEGIN TRANSACTION", "COMMIT", "ROLLBACK",
])
def test_non_flow_statements_are_skipped(sql):
    result = parse(sql)
    assert result.status == "skipped", result.reason
    assert result.reason and not result.edges and not result.writes


@pytest.mark.parametrize("name,sql", [
    ("oracle", "BEGIN INSERT INTO dw.stage SELECT id,value FROM src.events; END;"),
    ("postgres", "CREATE FUNCTION f() RETURNS void AS $$ BEGIN RETURN; END; $$ LANGUAGE plpgsql"),
    ("mysql", "CREATE PROCEDURE p() BEGIN SELECT id FROM src.events; END"),
    ("postgres", "DO $$ BEGIN RETURN; END; $$"),
])
def test_procedural_body_stays_one_unsupported_unit(name, sql):
    result = parse(sql, name)
    assert result.status == "unsupported"
    assert result.reason == "存储过程 / 代码块，本期不展开内部语句"
    assert not result.writes


def test_unknown_schema_falls_back_to_table_dependencies():
    result = parse("INSERT INTO dw.daily_summary SELECT missing FROM src.events", known=False)
    assert result.status in {"failed", "partial"}
    assert any(e.source_ref == "c.src.events" for e in result.edges)


def test_wrong_output_width_is_partial_and_preserves_table_sources():
    result = parse("INSERT INTO dw.daily_summary(id) SELECT id,value FROM src.events")
    assert result.status == "partial"
    assert signatures(result.writes[0]) == {("c.src.events", None, None, "table")}


def test_use_and_default_database_are_preserved():
    assert parse("USE src", "mysql").use_db == "src"
    result = parse("INSERT INTO stage(id,value) SELECT id,value FROM src.events", default_db="dw")
    assert result.status == "parsed" and result.target_ref == "c.dw.stage"


@pytest.mark.parametrize("sql,line_start,line,col", [
    ("INSERT INTO dw.daily_summary SELECT id FROM WHERE id=1", 1, 1, 49),
    ("INSERT INTO dw.daily_summary\nSELECT id\nFROM WHERE id=1", 17, 19, 10),
])
def test_parse_error_reason_has_readable_file_location(sql, line_start, line, col):
    result = parse(sql, line_start=line_start)
    assert result.status == "failed"
    assert result.reason == f"SQL 无法解析：第 {line} 行第 {col} 列附近（WHERE）：Expected table name"
    assert "TokenType" not in result.reason


def test_parse_error_reason_uses_first_structured_error(monkeypatch):
    def fail(*args, **kwargs):
        raise ParseError("raw token representation", errors=[
            {"description": "Expected expression", "line": 2, "col": 6, "highlight": ""},
            {"description": "second error", "line": 3, "col": 1, "highlight": "SELECT"},
        ])

    monkeypatch.setattr("avelis.importer.statements.sqlglot.parse", fail)
    result = parse("SELECT 1", line_start=10)
    assert result.status == "failed"
    assert result.reason == "SQL 无法解析：第 11 行第 6 列附近（）：Expected expression"


@pytest.mark.parametrize("error,expected", [
    (ParseError("x" * 201 + "\nsecond line"), "x" * 200),
    (ParseError("x" * 201 + "\nsecond line", errors=[{"description": "incomplete"}]), "x" * 200),
    (TokenError("x" * 201 + "\nsecond line"), "x" * 200),
    (ParseError(""), ""),
])
def test_parse_error_without_structure_falls_back_to_bounded_first_line(monkeypatch, error, expected):
    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr("avelis.importer.statements.sqlglot.parse", fail)
    result = parse("SELECT 1")
    assert result.status == "failed"
    assert result.reason == "SQL 无法解析：" + expected
