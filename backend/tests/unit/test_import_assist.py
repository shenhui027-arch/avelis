"""模型辅助只产生候选，使用确定性假模型，不联网或连接数据库。"""

from __future__ import annotations

import copy
from types import SimpleNamespace

import pytest

from avelis import modelcfg
from avelis.importer.assist import AssistBudget, code_blocks, run_assistance
from avelis.importer.candidates import model_operations
from avelis.lineage.sql import SqlDialect, fold_identifiers

DIALECT = SqlDialect("postgres", "default_catalog")
SCHEMA = {
    "default_catalog": {
        "src": {"events": {"id": "INT", "v": "INT"}},
        "dw": {"daily_summary": {"id": "INT", "v": "INT"}},
    }
}
SQL = "INSERT INTO dw.daily_summary (id) SELECT id FROM src.events"


class FakeImportModel:
    """按顺序返回工具结果；所有调用和绑定参数都可断言。"""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.prompts = []
        self.bindings = []

    def bind_tools(self, tools, **kwargs):
        assert "tool_choice" not in kwargs
        self.bindings.append((tools, kwargs))
        return self

    def invoke(self, prompt):
        self.prompts.append(prompt)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        if isinstance(response, str):
            return SimpleNamespace(tool_calls=[], text=response)
        name = self.bindings[-1][0][0]["title"]
        return SimpleNamespace(tool_calls=[{"name": name, "args": response}])


def operation(sql=SQL, *, start=2, end=2, **values):
    return {
        "line_start": start,
        "line_end": end,
        "type": "sql",
        "sql": sql,
        "read_tables": ["src.events"],
        "write_tables": ["dw.daily_summary"],
        "confidence": 0.8,
        "rationale": "从原文中的读写调用还原",
        **values,
    }


def edge(**values):
    return {
        "source_table": "src.events",
        "source_column": "id",
        "target_table": "dw.daily_summary",
        "target_column": "id",
        "relation": "value_direct",
        "confidence": 0.8,
        "rationale": "原文可见的数据操作",
        "line": 1,
        **values,
    }


def run(content, fake, *, language="python", statements=None, options=None, **kwargs):
    return run_assistance(
        language=language,
        content=content,
        rule_statements=statements or [],
        dialect=DIALECT,
        schema=kwargs.pop("schema", SCHEMA),
        default_db=kwargs.pop("default_db", "dw"),
        options=options or {"model_assist": {"code": True}},
        policy=kwargs.pop("policy", {"send_code": True}),
        resolved=kwargs.pop("resolved", modelcfg.Resolved(fake, "test:fake", "private-api-key")),
        **kwargs,
    )


def test_python_blocks_attach_imports_constants_and_configuration():
    source = 'import client\nCONFIG = {"db": "src"}\n\ndef first():\n    call()\n\nclass Second:\n    pass\n'
    blocks = code_blocks(source, "python")
    assert [(b.line_start, b.line_end) for b in blocks] == [(4, 5), (7, 8)]
    assert all('CONFIG = {"db": "src"}' in b.header and "import client" in b.header for b in blocks)
    assert blocks[0].text.startswith("def first")


def test_shell_blocks_keep_original_lines_and_file_header():
    blocks = code_blocks("DB=src\nfirst() {\n run_sql x\n}\nsecond() {\n run_sql y\n}", "shell")
    assert [(b.line_start, b.line_end) for b in blocks] == [(2, 4), (5, 7)]
    assert all(b.header == "DB=src" for b in blocks)


@pytest.mark.parametrize(
    "prefix",
    [
        "#!/bin/sh\n# $(example) is a comment\nDB=src",
        'export DB=src TARGET=dw\nSQL="SELECT id; SELECT v" # static SQL',
        "SQL='$(literal) `literal`' # $(comment)",
    ],
)
def test_shell_static_preamble_only_supplies_context(prefix):
    source = prefix + "\nload() {\n :\n}"
    blocks = code_blocks(source, "shell")
    assert len(blocks) == 1
    assert blocks[0].line_start == len(prefix.splitlines()) + 1
    assert blocks[0].header == prefix


@pytest.mark.parametrize(
    "command",
    [
        f'hive -e "{SQL}"',
        f'SQL="{SQL}"; hive -e "$SQL"',
        f'OUTPUT=$(hive -e "{SQL}")',
        f"OUTPUT=\"$(hive -e '{SQL}')\"",
    ],
)
def test_shell_executable_preamble_has_own_block_and_retains_candidate(command):
    source = "#!/bin/sh\n# load data\nDB=src\n" + command + "\nload() {\n :\n}"
    blocks = code_blocks(source, "shell")
    assert [(b.line_start, b.line_end) for b in blocks] == [(4, 4), (5, 7)]
    assert blocks[0].text == command
    assert all(b.header == "#!/bin/sh\n# load data\nDB=src" for b in blocks)
    fake = FakeImportModel({"operations": [operation(start=4, end=4)]}, {"operations": []})
    result = run(source, fake, language="shell")
    assert len(fake.prompts) == 2 and not result.notes
    assert len(result.candidates) == 1
    assert result.candidates[0]["evidence"]["lines"] == [4, 4]
    assert result.candidates[0]["evidence"]["original_code"] == command
    assert [(p["lines"]) for p in result.payloads] == [[4, 4], [5, 7]]


def test_python_module_operations_between_definitions_keep_original_lines():
    source = (
        'import client\nCONFIG = "src"\n'
        f'run_sql("{SQL}")\n'
        "def first():\n    pass\n"
        'frame = spark.table("src.events")\nframe.write.saveAsTable("dw.daily_summary")\n'
        "def second():\n    pass\n"
        f'run_sql("{SQL}")\nTAIL = "context"\n'
    )
    blocks = code_blocks(source, "python")
    assert [(b.line_start, b.line_end) for b in blocks] == [(1, 3), (4, 5), (6, 7), (8, 9), (10, 11)]
    assert all('CONFIG = "src"' in b.header and 'TAIL = "context"' in b.header for b in blocks)
    assert all("run_sql" not in b.header and "spark.table" not in b.header for b in blocks)
    fake = FakeImportModel(
        {"operations": [operation(start=3, end=3)]},
        {"operations": []},
        {"operations": [operation(start=6, end=7)]},
        {"operations": []},
        {"operations": [operation(start=10, end=10)]},
    )
    result = run(source, fake)
    assert len(fake.prompts) == 5 and not result.notes
    assert [c["evidence"]["lines"] for c in result.candidates] == [[3, 3], [6, 7], [10, 10]]
    assert len(result.statements) == 3


@pytest.mark.parametrize(
    "source,expected",
    [
        ("frame = source_frame\nframe.write = target_frame", [(1, 2)]),
        ("def load():\n    pass\nframe = source_frame\nframe.write = target_frame", [(1, 2), (3, 4)]),
    ],
)
def test_python_assignment_only_module_and_tail_keep_existing_coverage(source, expected):
    assert [(b.line_start, b.line_end) for b in code_blocks(source, "python")] == expected


@pytest.mark.parametrize(
    "language,source",
    [
        ("shell", f'hive -e "{SQL}"\nload() {{\n :\n}}'),
        ("python", f'run_sql("{SQL}")\ndef load():\n    pass'),
    ],
)
def test_top_level_model_operation_still_deduplicates_rule_sql(language, source):
    stmt = {
        "id": "s",
        "line_start": 1,
        "line_end": 1,
        "method": "sql",
        "status": "parsed",
        "raw_text": SQL,
        "sql_text": SQL,
    }
    fake = FakeImportModel({"operations": [operation(start=1, end=1)]}, {"operations": []})
    result = run(source, fake, language=language, statements=[stmt])
    assert len(fake.prompts) == 2 and not result.notes
    assert not result.candidates and not result.statements
    assert result.payloads[0]["comparisons"][0]["status"] == "consistent"


def test_reconstructed_sql_uses_parser_and_stays_pending():
    source = f'def load():\n    run_sql("{SQL}")'
    fake = FakeImportModel(
        {"operations": [operation(sql_function="run_sql")], "suggested_sql_functions": ["run_sql", "invented"]}
    )
    result = run(source, fake)
    assert len(result.candidates) == 1
    candidate = result.candidates[0]
    assert candidate["origin"] == "model_sql" and candidate["status"] == "pending"
    assert candidate["source_ref"] == "default_catalog.src.events"
    assert candidate["target_ref"] == "default_catalog.dw.daily_summary"
    assert candidate["relation"] == "value_direct"
    assert candidate["statement_id"] == result.statements[0]["id"] == "assist:0"
    assert candidate["evidence"]["restored_sql"] == SQL
    assert candidate["evidence"]["original_code"] == source.splitlines()[1]
    assert result.suggested_sql_functions == ["run_sql"]
    assert "${名字}" in fake.prompts[0]


def test_oracle_reconstructed_unquoted_names_follow_engine_identifier_normalization():
    dialect = SqlDialect("oracle", "FREEPDB1", normalize=fold_identifiers("oracle"), catalog_in_sql=False)
    schema = {"FREEPDB1": {"SRC": {"EVENTS": {"ID": "NUMBER"}}, "DW": {"DAILY_SUMMARY": {"ID": "NUMBER"}}}}
    sql = "insert into dw.daily_summary (id) select id from src.events"
    content = f'def load():\n    run_sql("{sql}")'
    fake = FakeImportModel({"operations": [operation(sql)]})
    result = run_assistance(
        language="python",
        content=content,
        rule_statements=[],
        dialect=dialect,
        schema=schema,
        default_db="DW",
        options={"model_assist": {"code": True}},
        policy={"send_code": True},
        resolved=modelcfg.Resolved(fake, "test:fake", "test-key"),
    )
    assert not result.notes
    assert len(result.candidates) == 1
    candidate = result.candidates[0]
    assert candidate["source_ref"] == "FREEPDB1.SRC.EVENTS"
    assert candidate["target_ref"] == "FREEPDB1.DW.DAILY_SUMMARY"
    assert candidate["source_column"] == candidate["target_column"] == "ID"
    assert candidate["origin"] == "model_sql" and candidate["status"] == "pending"


def test_wrapper_function_suggestion_does_not_require_reconstructed_sql():
    source = "def load():\n    run_sql(build_query())"
    result = run(source, FakeImportModel({"operations": [operation(sql=None, sql_function="run_sql")]}))
    assert result.suggested_sql_functions == ["run_sql"]
    assert not result.candidates


def test_failed_sql_candidates_keep_statement_link_and_reason():
    source = "INSERT INTO dw.daily_summary SELECT id FROM src.events BROKEN"
    stmt = {
        "id": "statement-1",
        "seq": 1,
        "line_start": 1,
        "line_end": 1,
        "method": "sql",
        "status": "failed",
        "raw_text": source,
        "reason": "SQL 无法解析",
    }
    fake = FakeImportModel({"edges": [edge()]})
    result = run(source, fake, language="sql", statements=[stmt], options={"model_assist": {"failed_sql": True}})
    assert len(result.candidates) == 1
    assert result.candidates[0]["statement_id"] == "statement-1"
    assert result.candidates[0]["origin"] == "model"
    assert "SQL 无法解析" in fake.prompts[0]
    assert result.payloads[0]["kind"] == "failed_sql"


def test_assistance_options_default_off_but_non_sql_keeps_legacy_path():
    source = 'df = spark.table("src.events")\ndf.write.saveAsTable("dw.daily_summary")'
    stmt = {"id": "s", "line_start": 1, "line_end": 2, "method": "unrecognized", "raw_text": source}
    fake = FakeImportModel({"edges": [edge(line=2)]})
    result = run(source, fake, statements=[stmt], options={"model_assist": {}})
    assert len(result.payloads) == 1 and result.payloads[0]["kind"] == "non_sql"
    assert result.candidates[0]["origin"] == "model"
    assert not run(source, FakeImportModel(), options={"model_assist": {}}).payloads


def test_send_policy_prevents_real_model_and_marks_demo():
    source = 'df = spark.table("src.events")\ndf.write.saveAsTable("dw.daily_summary")'
    stmt = {"id": "s", "line_start": 1, "line_end": 2, "method": "unrecognized", "raw_text": source}
    fake = FakeImportModel()
    result = run(source, fake, statements=[stmt], options={"model_assist": {}}, policy={"send_code": False})
    assert not fake.prompts and result.candidates
    assert all(c["evidence"]["is_demo"] for c in result.candidates)
    assert result.payloads[0]["sent_to"] == "未发送（演示规则）"


@pytest.mark.parametrize(
    "values,reason",
    [
        ({"line": 9}, "行号"),
        ({"source_table": "src.missing"}, "未采集"),
        ({"source_column": "missing"}, "没有列"),
        ({"target_table": "dw.missing"}, "未出现在代码"),
        ({"relation": "invented"}, "不合法"),
    ],
)
def test_model_candidates_reject_unknown_sources_columns_and_invalid_lines(values, reason):
    stmt = {"id": "s", "line_start": 1, "line_end": 1, "method": "sql", "status": "failed", "raw_text": SQL}
    result = run(
        SQL,
        FakeImportModel({"edges": [edge(**values)]}),
        language="sql",
        statements=[stmt],
        options={"model_assist": {"failed_sql": True}},
    )
    assert not result.candidates and reason in " ".join(result.notes)


def test_model_and_rule_same_normalized_sql_do_not_duplicate_candidates():
    source = f'def load():\n    run_sql("{SQL}")'
    stmt = {
        "id": "s",
        "line_start": 2,
        "line_end": 2,
        "method": "python_ast",
        "status": "parsed",
        "raw_text": SQL,
        "sql_text": "insert into dw.daily_summary (id) select id from src.events",
    }
    result = run(source, FakeImportModel({"operations": [operation()]}), statements=[stmt])
    assert not result.candidates and not result.statements
    assert result.payloads[0]["comparisons"][0]["status"] == "consistent"


def test_different_sql_on_same_lines_marks_conflict():
    source = f'def load():\n    run_sql("{SQL}")'
    stmt = {
        "id": "s",
        "line_start": 2,
        "line_end": 2,
        "method": "python_ast",
        "status": "parsed",
        "raw_text": SQL,
        "sql_text": "INSERT INTO dw.daily_summary (id) SELECT v FROM src.events",
    }
    result = run(source, FakeImportModel({"operations": [operation()]}), statements=[stmt])
    assert result.candidates[0]["evidence"]["comparison"] == "conflict"
    assert "不一致" in result.candidates[0]["evidence"]["note"]


def test_reconstructed_table_must_appear_in_original_block():
    source = 'def load():\n    run_sql("SELECT id FROM src.events")'
    result = run(source, FakeImportModel({"operations": [operation()]}))
    assert not result.candidates and "不能由块内常量拼出" in " ".join(result.notes)


def test_table_from_static_configuration_and_concatenation_is_grounded():
    source = (
        'CONFIG = {"db": "src", "name": "events"}\n'
        'def load():\n    source = CONFIG["db"] + "." + CONFIG["name"]\n'
        '    run_sql(source, "dw.daily_summary")'
    )
    result = run(source, FakeImportModel({"operations": [operation(start=4, end=4)]}))
    assert len(result.candidates) == 1


def test_shell_table_from_constant_is_grounded_without_executing_commands():
    source = (
        'SOURCE_DB=src\nload() {\n run_sql "INSERT INTO dw.daily_summary (id) SELECT id FROM ${SOURCE_DB}.events"\n}'
    )
    result = run(source, FakeImportModel({"operations": [operation(start=3, end=3)]}), language="shell")
    assert len(result.candidates) == 1


def test_reconstructed_source_must_be_collected_or_created_in_batch():
    source = f'def load():\n    run_sql("{SQL}")'
    schema = {"default_catalog": {"dw": {"daily_summary": {"id": "INT"}}}}
    result = run(source, FakeImportModel({"operations": [operation()]}), schema=schema)
    assert not result.candidates and "未采集" in " ".join(result.notes)


@pytest.mark.parametrize(
    "first_sql,source_column",
    [
        ("CREATE TABLE dw.stage AS SELECT id AS recovered FROM src.events", "recovered"),
        ("CREATE TABLE dw.stage AS SELECT 1 AS recovered", "recovered"),
        ("CREATE TABLE dw.stage (recovered INT)", "recovered"),
        ("INSERT INTO dw.stage (recovered) SELECT id FROM src.events", "recovered"),
    ],
)
def test_reconstructed_operations_share_inferred_structure_without_mutating_rule_schema(first_sql, source_column):
    second_sql = "INSERT INTO dw.daily_summary (id) SELECT * FROM dw.stage"
    source = f'def load():\n    run_sql("{first_sql}")\n    run_sql("{second_sql}")'
    schema = copy.deepcopy(SCHEMA)
    fake = FakeImportModel({"operations": [operation(first_sql), operation(second_sql, start=3, end=3)]})

    result = run(source, fake, schema=schema)

    assert not result.notes
    assert [statement["status"] for statement in result.statements] == ["parsed", "parsed"]
    downstream = [
        candidate for candidate in result.candidates if candidate["target_ref"] == "default_catalog.dw.daily_summary"
    ]
    assert len(downstream) == 1
    assert downstream[0]["source_ref"] == "default_catalog.dw.stage"
    assert downstream[0]["source_column"] == source_column
    assert downstream[0]["target_column"] == "id"
    assert all(
        candidate["origin"] == "model_sql" and candidate["status"] == "pending" for candidate in result.candidates
    )
    assert schema == SCHEMA


def test_reconstructed_structure_reaches_later_blocks_and_is_rebuilt_from_cache():
    first_sql = "CREATE TABLE dw.stage AS SELECT id AS recovered FROM src.events"
    second_sql = "INSERT INTO dw.daily_summary (id) SELECT * FROM dw.stage"
    source = f'def prepare():\n    run_sql("{first_sql}")\ndef publish():\n    run_sql("{second_sql}")'
    fake = FakeImportModel(
        {"operations": [operation(first_sql)]}, {"operations": [operation(second_sql, start=4, end=4)]}
    )
    first = run(source, fake)
    replay_model = FakeImportModel()

    replay = run(source, replay_model, cached=first.cache)

    assert not first.notes and not replay.notes
    assert len(first.candidates) == 2
    assert replay.candidates == first.candidates
    assert not replay_model.prompts and all(payload["reused"] for payload in replay.payloads)
    assert "stage" not in SCHEMA["default_catalog"]["dw"]


@pytest.mark.parametrize(
    "first_sql,status",
    [
        ("CREATE TABLE dw.stage AS SELECT id FROM src.events WHERE id > ${threshold}", "needs_params"),
        ("CREATE TABLE dw.stage AS SELECT ( FROM src.events", "failed"),
    ],
)
def test_unparsed_reconstructed_operation_does_not_register_a_table(first_sql, status):
    second_sql = "INSERT INTO dw.daily_summary (id) SELECT id FROM dw.stage"
    source = f'def load():\n    run_sql("{first_sql}")\n    run_sql("{second_sql}")'
    fake = FakeImportModel({"operations": [operation(first_sql), operation(second_sql, start=3, end=3)]})

    result = run(source, fake)

    assert not result.candidates
    assert [statement["status"] for statement in result.statements] == [status]
    assert "来源表 dw.stage 未采集" in " ".join(result.notes)


def test_reconstructed_operation_before_table_creation_cannot_use_future_structure():
    first_sql = "INSERT INTO dw.daily_summary (id) SELECT id FROM dw.stage"
    second_sql = "CREATE TABLE dw.stage AS SELECT id FROM src.events"
    source = f'def load():\n    run_sql("{first_sql}")\n    run_sql("{second_sql}")'
    # 模型返回的列表顺序不能把原文后建的表提前登记。
    fake = FakeImportModel({"operations": [operation(second_sql, start=3, end=3), operation(first_sql)]})

    result = run(source, fake)

    assert [candidate["target_ref"] for candidate in result.candidates] == ["default_catalog.dw.stage"]
    assert len(result.statements) == 1 and result.statements[0]["line_start"] == 3
    assert "来源表 dw.stage 未采集" in " ".join(result.notes)


def test_unknown_sql_parts_keep_placeholders_for_whole_batch_reparse():
    sql = SQL + " WHERE id > ${threshold}"
    source = f'def load():\n    run_sql("{sql}")'
    result = run(source, FakeImportModel({"operations": [operation(sql)]}))
    assert not result.candidates
    assert result.statements[0]["status"] == "needs_params"
    assert result.statements[0]["placeholders"] == ["threshold"]
    assert "${threshold}" in result.statements[0]["sql_text"]


@pytest.mark.parametrize("start,end", [(0, 2), (2, 3), (3, 2)])
def test_operation_line_validation(start, end):
    source = f'def load():\n    run_sql("{SQL}")'
    result = run(source, FakeImportModel({"operations": [operation(start=start, end=end)]}))
    assert not result.candidates
    assert result.notes or result.payloads[0].get("error")


def test_invalid_operation_does_not_discard_other_valid_operations():
    source = f'def load():\n    run_sql("{SQL}")'
    fake = FakeImportModel({"operations": [operation(start=0), operation()]})
    result = run(source, fake)
    assert len(result.candidates) == 1
    assert "已丢弃" in " ".join(result.notes)


def test_fragment_cache_reuses_model_output_and_revalidates_schema():
    source = f'def load():\n    run_sql("{SQL}")'
    first_model = FakeImportModel({"operations": [operation()]})
    first = run(source, first_model)
    second_model = FakeImportModel()
    second = run(source, second_model, cached=first.cache)
    assert len(first_model.prompts) == 1 and not second_model.prompts
    assert second.payloads[0]["reused"] and len(second.candidates) == 1
    third = run(source, FakeImportModel(), cached=first.cache, schema={})
    assert not third.candidates and third.payloads[0]["reused"]


def test_demo_fragment_cache_is_invalidated_when_real_model_becomes_available():
    source = f'def load():\n    run_sql("{SQL}")'
    demo = run(source, FakeImportModel(), policy={"send_code": False})
    assert demo.payloads[0]["is_demo"] and not demo.candidates

    fake = FakeImportModel({"operations": [operation()]})
    real = run(source, fake, cached=demo.cache)
    assert len(fake.prompts) == 1 and len(real.candidates) == 1
    assert not real.payloads[0]["is_demo"] and not real.payloads[0].get("reused")
    assert real.payloads[0]["fragment_hash"] != demo.payloads[0]["fragment_hash"]


@pytest.mark.parametrize("adapter,model_id", [("other:fake", "model-v1"), ("test:fake", "model-v2")])
def test_fragment_cache_is_invalidated_when_adapter_or_model_identity_changes(adapter, model_id):
    source = f'def load():\n    run_sql("{SQL}")'
    first_model = FakeImportModel({"operations": [operation()]})
    first_model.model_name = "model-v1"
    first = run(source, first_model)
    second_model = FakeImportModel({"operations": [operation()]})
    second_model.model_name = model_id
    second = run(source, second_model, cached=first.cache, resolved=modelcfg.Resolved(second_model, adapter))
    assert len(second_model.prompts) == 1 and len(second.candidates) == 1
    assert not second.payloads[0].get("reused")
    assert second.payloads[0]["fragment_hash"] != first.payloads[0]["fragment_hash"]


def test_explicit_missing_statement_database_does_not_fall_back_to_file_database():
    source = 'spark.table("src.events").write.saveAsTable("daily_summary")'
    stmt = {"id": "s", "seq": 1, "line_start": 1, "line_end": 1, "method": "unrecognized", "raw_text": source}
    context = {"seq": 1, "line_start": 1, "line_end": 1, "default_db": None, "default_db_after": None}
    fake = FakeImportModel({"edges": [edge(target_table="daily_summary")]})
    result = run(source, fake, statements=[stmt], options={"model_assist": {}}, statement_contexts=[context])
    assert result.payloads[0]["default_db"] is None and not result.candidates
    assert "默认数据库为 （未设置）" in fake.prompts[0]


def test_fragment_cache_is_invalidated_when_effective_database_changes():
    source = 'spark.table("events").write.saveAsTable("summary")'
    stmt = {"id": "s", "seq": 1, "line_start": 1, "line_end": 1, "method": "unrecognized", "raw_text": source}
    schema = {"default_catalog": {db: {"events": {"id": "INT"}} for db in ("a", "b")}}
    fake = FakeImportModel(
        {"edges": [edge(source_table="events", target_table="summary")]},
        {"edges": [edge(source_table="events", target_table="summary")]},
    )
    first = run(source, fake, statements=[stmt], schema=schema, default_db="a", options={"model_assist": {}})
    context = {"seq": 1, "line_start": 1, "line_end": 1, "default_db": "b", "default_db_after": "b"}
    second = run(
        source,
        fake,
        statements=[stmt],
        schema=schema,
        cached=first.cache,
        statement_contexts=[context],
        options={"model_assist": {}},
    )
    assert len(fake.prompts) == 2 and not second.payloads[0].get("reused")
    assert first.candidates[0]["source_ref"] == "default_catalog.a.events"
    assert second.candidates[0]["source_ref"] == "default_catalog.b.events"


def test_restored_operation_crossing_use_is_not_parsed_with_a_single_database():
    sql = "INSERT INTO summary (id) SELECT id FROM events"
    source = f'run_sql("USE b")\nrun_sql("{sql}")'
    context = {"seq": 1, "line_start": 1, "line_end": 1, "default_db": "a", "default_db_after": "b"}
    fake = FakeImportModel({"operations": [operation(sql=sql, start=1, end=2)]})
    result = run(
        source,
        fake,
        default_db="a",
        schema={"default_catalog": {"b": {"events": {"id": "INT"}}}},
        statement_contexts=[context],
    )
    assert not result.candidates and not result.statements
    assert "操作行范围包含默认数据库切换" in " ".join(result.notes)


def test_batch_budget_is_shared_across_files_and_skips_excess_without_call():
    source = f'def load():\n    run_sql("{SQL}")'
    budget = AssistBudget(fragments=199)
    fake = FakeImportModel({"operations": [operation()]})
    first = run(source, fake, budget=budget)
    second = run(source, fake, budget=budget)
    assert len(first.candidates) == 1 and not second.candidates and len(fake.prompts) == 1
    assert budget.fragments == 200 and second.payloads[0]["skipped"]
    assert "批次上限" in " ".join(second.notes)


def test_character_budget_counts_attached_header():
    source = f'CONFIG = "context"\ndef load():\n    run_sql("{SQL}")'
    fake = FakeImportModel()
    result = run(source, fake, budget=AssistBudget(characters=1_999_999))
    assert not fake.prompts and result.payloads[0]["skipped"]


@pytest.mark.parametrize("key", ["password", "api-key"])
@pytest.mark.parametrize("separator", ["=", " "])
@pytest.mark.parametrize("quote", ["", "'", '"'])
def test_cli_credentials_are_redacted_in_model_prompt_and_persisted_payload(key, separator, quote):
    secret = "synthetic-only-secret"
    command = f'client --{key}{separator}{quote}{secret}{quote} -e "{SQL}"'
    source = command + "\nload() {\n :\n}"
    fake = FakeImportModel({"operations": []}, {"operations": []})
    result = run(source, fake, language="shell")

    assert len(fake.prompts) == len(result.payloads) == 2
    assert not result.notes
    # SQL 参数中的残余引号触发整行兜底，连同凭据值的引号一起覆盖。
    expected = f"client --{key}{separator}<redacted>"
    assert expected in fake.prompts[0]
    assert result.payloads[0]["prompt"] == fake.prompts[0]
    assert result.payloads[0]["text"] == expected
    assert result.payloads[0]["redactions"] == 2
    assert all(secret not in prompt for prompt in fake.prompts)
    assert secret not in str(result.payloads)


@pytest.mark.parametrize(
    "source,language,expected,count,secret",
    [
        (
            'cur.execute("CREATE ROLE r PASSWORD " "\'SYNTH1\';")',
            "python",
            'cur.execute("CREATE ROLE r PASSWORD<redacted>',
            2,
            "SYNTH1",
        ),
        (
            "# user's account\ncur.execute(\"CREATE ROLE r PASSWORD ')SYNTH2';\")",
            "python",
            "# user's account\ncur.execute(\"CREATE ROLE r PASSWORD<redacted>",
            1,
            "SYNTH2",
        ),
        ("password: :SYNTH3", "shell", "password: <redacted>", 1, "SYNTH3"),
        ('connect("password::SYNTH4@db")', "python", 'connect("password:<redacted>', 1, "SYNTH4"),
    ],
    ids=["adjacent-host-literals", "comment-apostrophe", "yaml-colon-value", "connection-colon-password"],
)
def test_line_fallback_protects_model_prompt_and_persisted_payload(source, language, expected, count, secret):
    fake = FakeImportModel({"operations": []})
    result = run(source, fake, language=language)

    assert len(fake.prompts) == len(result.payloads) == 1
    assert not result.notes
    payload = result.payloads[0]
    assert payload["text"] == expected
    assert payload["redactions"] == count
    assert expected in fake.prompts[0]
    assert payload["prompt"] == fake.prompts[0]
    assert secret not in fake.prompts[0]
    assert secret not in str(result.payloads)


def test_payload_and_errors_remove_credentials_before_persistence():
    source = f'password = "private-code-secret"\ndef load():\n    run_sql("{SQL}")'
    fake = FakeImportModel(RuntimeError("private-api-key Authorization: Bearer private-bearer"))
    result = run(source, fake)
    assert not result.candidates and "模型推断失败" in result.payloads[0]["error"]
    for secret in ("private-api-key", "private-bearer", "private-code-secret"):
        assert secret not in str(result)
        assert secret not in " ".join(fake.prompts)
    assert result.payloads[0]["redactions"] == 1


def test_text_json_fallback_uses_same_operation_validation_and_no_forced_tool():
    fake = FakeImportModel('{"operations": [], "suggested_sql_functions": []}')
    result = model_operations(fake, "pass", 1, 1)
    assert not result.operations
    assert fake.bindings[0][1] == {}
