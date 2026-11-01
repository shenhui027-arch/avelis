"""客户端分隔符、过程块与代码抽取共用的 SQL 切分边界。"""

from textwrap import dedent

import pytest
from sqlglot.errors import TokenError

from avelis.engines.builtin import MYSQL, ORACLE, SQLSERVER, STARROCKS
from avelis.importer.extract import Segment, extract_python, extract_shell
from avelis.importer.split import split_sql


@pytest.mark.parametrize("dialect", ["starrocks", "hive", "postgres", "mysql", "tsql", "oracle"])
def test_split_each_dialect_preserves_quotes_comments_and_positions(dialect):
    source = "-- 说明中的 ;\nSELECT 'first;part' /* ; */ FROM src.events;\n\nSELECT 2;"
    notes = []
    segments = split_sql(source, dialect, notes=notes)
    assert not notes
    assert [(segment.line_start, segment.line_end) for segment in segments] == [(2, 2), (4, 4)]
    assert [segment.text for segment in segments] == ["SELECT 'first;part' /* ; */ FROM src.events", "SELECT 2"]
    assert all(isinstance(segment, Segment) for segment in segments)


def test_mysql_delimiter_keeps_procedure_whole_and_removes_only_client_markers():
    source = dedent("""\
        DELIMITER $$
        CREATE PROCEDURE refresh_rows()
        BEGIN
          SELECT 'literal$$;';
          SELECT CASE WHEN 1 = 1 THEN 2 ELSE 3 END;
          BEGIN SELECT 4; END;
        END$$
        DELIMITER ;
        SELECT 5;
    """)
    segments = split_sql(source, MYSQL.sql.name, MYSQL.script_separators)
    assert len(segments) == 2
    assert [(segment.line_start, segment.line_end) for segment in segments] == [(2, 7), (9, 9)]
    assert "'literal$$;'" in segments[0].text
    assert "DELIMITER" not in segments[0].text
    assert segments[0].text.endswith("END")
    assert segments[1].text == "SELECT 5"


@pytest.mark.parametrize("delimiter", ["//", "#", ";;"])
def test_custom_delimiter_text_is_not_confused_with_comments(delimiter):
    source = f"DELIMITER {delimiter}\nSELECT 1{delimiter}\nDELIMITER ;\nSELECT 2;"
    assert [segment.text for segment in split_sql(source, "mysql", ("DELIMITER",))] == ["SELECT 1", "SELECT 2"]


@pytest.mark.parametrize("marker,separators,dialect", [
    ("GO", SQLSERVER.script_separators, SQLSERVER.sql.name),
    ("/", ORACLE.script_separators, ORACLE.sql.name),
])
def test_standalone_client_separator_with_or_without_semicolon(marker, separators, dialect):
    segments = split_sql(f"SELECT 1\n{marker}\nSELECT 2;\n{marker}\n", dialect, separators)
    assert [segment.text for segment in segments] == ["SELECT 1", "SELECT 2"]
    assert [(segment.line_start, segment.line_end) for segment in segments] == [(1, 1), (3, 3)]


def test_separators_are_engine_declarations_rather_than_dialect_branches():
    assert STARROCKS.script_separators == ("DELIMITER",)
    segments = split_sql("SELECT 1\nGO\nSELECT 2;", "mysql", ("GO",))
    assert [segment.text for segment in segments] == ["SELECT 1", "SELECT 2"]
    assert len(split_sql("SELECT 1\nGO\nSELECT 2;", "tsql", ())) == 1


def test_separator_lines_inside_multiline_strings_and_comments_are_data():
    source = "SELECT 'first\nGO\nlast';\n/*\nGO\n*/\nSELECT 2;\nGO\n"
    segments = split_sql(source, "tsql", ("GO",))
    assert [segment.text for segment in segments] == ["SELECT 'first\nGO\nlast'", "SELECT 2"]
    assert [(segment.line_start, segment.line_end) for segment in segments] == [(1, 3), (7, 7)]


def test_separator_inside_multiline_quoted_identifier_is_data():
    segments = split_sql("SELECT [first\nGO\nlast];\nGO\nSELECT 2;", "tsql", ("GO",))
    assert [segment.text for segment in segments] == ["SELECT [first\nGO\nlast]", "SELECT 2"]


def test_client_batch_keeps_a_procedure_without_begin_whole():
    source = "CREATE PROCEDURE refresh_rows AS\nSELECT 1;\nSELECT 2;\nGO\nSELECT 3;"
    segments = split_sql(source, "tsql", ("GO",))
    assert [segment.text for segment in segments] == [
        "CREATE PROCEDURE refresh_rows AS\nSELECT 1;\nSELECT 2", "SELECT 3",
    ]


@pytest.mark.parametrize("quote", ["$$", "$body$"])
def test_postgres_dollar_quoted_body_does_not_split(quote):
    source = (f"CREATE FUNCTION f() RETURNS void AS {quote}\nBEGIN\n SELECT 1;\nEND;\n"
              f"{quote} LANGUAGE plpgsql;\nSELECT 2;")
    segments = split_sql(source, "postgres")
    assert len(segments) == 2
    assert segments[0].line_start == 1 and segments[0].line_end == 5
    assert segments[1].text == "SELECT 2"


@pytest.mark.parametrize("begin", ["BEGIN;", "BEGIN TRANSACTION;", "BEGIN TRAN;", "BEGIN WORK;"])
def test_transaction_begin_does_not_open_procedure_depth(begin):
    segments = split_sql(begin + "\nSELECT 1;\nCOMMIT;", "tsql")
    assert [segment.text for segment in segments] == [begin[:-1], "SELECT 1", "COMMIT"]


def test_nested_begin_case_and_end_if_remain_one_code_block():
    source = ("BEGIN\nIF flag THEN\nSELECT CASE WHEN flag THEN 1 ELSE 2 END;\nEND IF;\n"
              "BEGIN SELECT 3; END;\nEND;\nSELECT 4;")
    segments = split_sql(source, "oracle")
    assert len(segments) == 2
    assert segments[0].line_start == 1 and segments[0].line_end == 6
    assert segments[1].text == "SELECT 4"


def test_case_expression_closes_before_statement_semicolon():
    segments = split_sql("SELECT CASE WHEN 1=1 THEN CASE WHEN 2=2 THEN 3 END ELSE 4 END; SELECT 5;", "postgres")
    assert len(segments) == 2 and segments[1].text == "SELECT 5"


def test_end_case_does_not_open_another_case_block():
    source = "BEGIN CASE flag WHEN 1 THEN SELECT 1; END CASE; SELECT 2; END; SELECT 3;"
    segments = split_sql(source, "mysql")
    assert len(segments) == 2 and segments[1].text == "SELECT 3"


def test_tokenization_failure_falls_back_and_reports_the_file_note():
    notes = []
    segments = split_sql("SELECT 1;\nSELECT 'unfinished;", "mysql", notes=notes)
    assert [segment.text for segment in segments] == ["SELECT 1", "SELECT 'unfinished;"]
    assert [(segment.line_start, segment.line_end) for segment in segments] == [(1, 1), (2, 2)]
    assert len(notes) == 1 and "按分号回退切分" in notes[0]


def test_fallback_still_uses_the_requested_dialect_and_line_offset(monkeypatch):
    def fail(text, *, read):
        assert read == "oracle"
        raise TokenError("测试分词失败")

    monkeypatch.setattr("avelis.importer.split.sqlglot.tokenize", fail)
    notes = []
    segments = split_sql("-- ;\nSELECT ';';\nSELECT 2;", "oracle", line_offset=4, notes=notes)
    assert [segment.text for segment in segments] == ["SELECT ';'", "SELECT 2"]
    assert [segment.line_start for segment in segments] == [6, 7]
    assert "第 5 行起" in notes[0]


def test_original_multiline_text_and_placeholders_are_retained():
    source = "\n/* 开头 */\nINSERT INTO dw.daily_summary\nSELECT id /* 原样保留 */\nFROM ${database}.events\n;\n"
    [segment] = split_sql(source, "starrocks", line_offset=10)
    assert (segment.line_start, segment.line_end) == (13, 15)
    assert "/* 原样保留 */" in segment.text
    assert segment.placeholders == ["database"]


def test_python_name_and_attribute_custom_functions_share_the_new_splitter():
    source = dedent('''\
        run_sql("SELECT $$first;second$$; SELECT 2;")
        client.run_sql("BEGIN; SELECT 3; COMMIT;")
        text("SELECT 4;")
    ''')
    segments, notes = extract_python(source, "postgres", sql_functions=["run_sql"])
    assert not notes
    assert [segment.text for segment in segments] == [
        "SELECT $$first;second$$", "SELECT 2", "BEGIN", "SELECT 3", "COMMIT", "SELECT 4",
    ]
    assert [segment.line_start for segment in segments] == [1, 1, 2, 2, 2, 3]
    assert all(segment.method == "python_ast" for segment in segments)


def test_custom_qualified_function_name_does_not_match_other_objects():
    source = 'client.run_sql("SELECT 1"); other.run_sql("SELECT 2")'
    segments, notes = extract_python(source, sql_functions=["client.run_sql"])
    assert not notes and [segment.text for segment in segments] == ["SELECT 1"]


def test_custom_plain_function_with_unknown_argument_goes_to_candidates():
    segments, notes = extract_python("run_sql(make_query())", sql_functions=["run_sql"])
    assert len(segments) == 1 and segments[0].method == "unrecognized"
    assert len(notes) == 1 and "run_sql()" in notes[0]


def test_python_and_shell_forward_tokenization_failure_notes():
    python_segments, python_notes = extract_python('cursor.execute("SELECT \'unfinished")', "mysql")
    shell_segments, shell_notes = extract_shell('mysql -e "SELECT \'unfinished"', "mysql")
    assert len(python_segments) == len(shell_segments) == 1
    assert "按分号回退切分" in python_notes[0]
    assert "按分号回退切分" in shell_notes[0]


def test_shell_heredoc_uses_client_separators_and_preserves_source_lines():
    source = "psql <<'SQL'\nSELECT $$first;second$$;\nSELECT 2;\nSQL\n"
    segments, notes = extract_shell(source, "postgres")
    assert not notes
    assert [segment.text for segment in segments] == ["SELECT $$first;second$$", "SELECT 2"]
    assert [(segment.line_start, segment.line_end, segment.method) for segment in segments] == [
        (2, 2, "shell"), (3, 3, "shell"),
    ]


def test_shell_custom_execution_functions_are_recognized():
    source = 'run_sql -e "SELECT 1;"\nrun_sql <<SQL\nSELECT 2;\nSQL\n'
    segments, notes = extract_shell(source, "mysql", sql_functions=["run_sql"])
    assert not notes
    assert [segment.text for segment in segments] == ["SELECT 1", "SELECT 2"]
    assert [segment.line_start for segment in segments] == [1, 3]


def test_comment_only_or_empty_scripts_do_not_make_statements():
    assert split_sql(" \n -- 说明;\n/* 更多说明; */\n", "mysql") == []
    assert split_sql("\nGO\n", "tsql", ("GO",)) == []
