"""批次内折叠的关系复合与脚本生命周期。"""

from copy import deepcopy
from types import SimpleNamespace

import pytest

from avelis.importer.fold import FoldStatement, fold_writes
from avelis.importer.statements import parse_statement
from avelis.lineage.sql import SqlDialect, fold_identifiers


def edge(source, source_column="id", target_column="id", relation="value_direct"):
    return SimpleNamespace(source_ref=source, source_column=source_column, target_column=target_column,
                           relation=relation, expression=None, attributes={})


def write(target, edges, *, temporary=False, created=False, op=None):
    return SimpleNamespace(target_ref=target, target_columns=["id"], edges=edges, temporary=temporary,
                           created=created, attributes={"op": op} if op else {})


def statement(seq, writes=(), *, drops=(), removes=(), resets=(), job="file-a"):
    return FoldStatement(job, f"statement-{seq}", list(writes), list(drops), list(removes), list(resets))


def signature(result):
    return {(e.source_ref, e.source_column, e.target_ref, e.target_column, e.relation) for e in result.active}


def test_multilevel_cross_file_fold_preserves_all_original_hops_without_mutation():
    statements = [
        statement(1, [write("cat.stage.first", [edge("cat.src.events")], temporary=True, created=True)]),
        statement(2, [write("cat.stage.second", [edge("cat.stage.first", relation="value_computed")],
                            temporary=True, created=True)], job="file-b"),
        statement(3, [write("cat.dw.daily_summary", [edge("cat.stage.second", relation="value_aggregated")])],
                  job="file-c"),
    ]
    before = deepcopy(statements)
    result = fold_writes(statements)
    assert signature(result) == {
        ("cat.src.events", "id", "cat.dw.daily_summary", "id", "value_aggregated")}
    assert len(result.intermediate) == 3
    assert result.active[0].attributes["via"] == ["cat.stage.first", "cat.stage.second"]
    assert result.active[0].attributes["hops"] == [edge.id for edge in result.intermediate]
    assert result.active[0].job_id == "file-c"
    assert all(edge.status == "intermediate" for edge in result.intermediate)
    assert statements == before
    assert [edge.id for edge in fold_writes(statements).edges] == [edge.id for edge in result.edges]


@pytest.mark.parametrize("relation", ["filter", "join_key", "group_by"])
def test_downstream_influence_uses_upstream_value_source(relation):
    result = fold_writes([
        statement(1, [write("cat.stage.rows", [edge("cat.src.events")], temporary=True)]),
        statement(2, [write("cat.dw.daily_summary", [edge("cat.stage.rows", target_column=None,
                                                        relation=relation)])]),
    ])
    assert signature(result) == {("cat.src.events", "id", "cat.dw.daily_summary", None, relation)}


def test_upstream_influence_reaches_final_table_separately_from_value():
    result = fold_writes([
        statement(1, [write("cat.stage.rows", [edge("cat.src.events"),
                                              edge("cat.src.flags", "flag", None, "filter")], temporary=True)]),
        statement(2, [write("cat.dw.daily_summary", [edge("cat.stage.rows")])]),
    ])
    assert signature(result) == {
        ("cat.src.events", "id", "cat.dw.daily_summary", "id", "value_direct"),
        ("cat.src.flags", "flag", "cat.dw.daily_summary", None, "filter"),
    }


@pytest.mark.parametrize("table_upstream", [False, True])
def test_either_table_level_segment_makes_result_table_level(table_upstream):
    first = edge("cat.src.events", None, None, "table") if table_upstream else edge("cat.src.events")
    second = edge("cat.stage.rows") if table_upstream else edge("cat.stage.rows", None, None, "table")
    result = fold_writes([
        statement(1, [write("cat.stage.rows", [first], temporary=True)]),
        statement(2, [write("cat.dw.daily_summary", [second])]),
    ])
    assert signature(result) == {("cat.src.events", None, "cat.dw.daily_summary", None, "table")}


def test_temporary_write_not_read_is_only_intermediate():
    result = fold_writes([
        statement(1, [write("cat.stage.rows", [edge("cat.src.events")], temporary=True)]),
    ])
    assert not result.active
    assert len(result.intermediate) == 1


def test_created_then_dropped_table_folds_but_drop_of_existing_asset_does_not():
    result = fold_writes([
        statement(1, [write("cat.stage.rows", [edge("cat.src.events")], created=True)]),
        statement(2, [write("cat.dw.daily_summary", [edge("cat.stage.rows")])]),
        statement(3, drops=["cat.stage.rows"]),
        statement(4, drops=["cat.src.events"]),
    ])
    assert result.intermediate_tables == ["cat.stage.rows"]
    assert signature(result) == {("cat.src.events", "id", "cat.dw.daily_summary", "id", "value_direct")}


def test_permanent_write_is_not_folded_and_names_do_not_imply_temporary():
    result = fold_writes([
        statement(1, [write("cat.dw.tmp_name", [edge("cat.src.events")], created=True)]),
        statement(2, [write("cat.dw.daily_summary", [edge("cat.dw.tmp_name")])]),
    ])
    assert len(result.active) == 2
    assert not result.intermediate


def test_rename_collapses_prior_writes_and_new_name_remains_final():
    result = fold_writes([
        statement(1, [write("cat.stage.rows", [edge("cat.src.events")], created=True)]),
        statement(2, [write("cat.dw.daily_summary", [edge("cat.stage.rows")], op="rename")],
                  removes=["cat.stage.rows"]),
    ])
    assert signature(result) == {("cat.src.events", "id", "cat.dw.daily_summary", "id", "value_direct")}
    assert result.active[0].attributes["op"] == "rename"


def test_renamed_external_table_dropped_later_is_intermediate():
    # 批次外已有的 live 先改名为 old 再被删除：指向 old 的边随表消失，只留中间段
    result = fold_writes([
        statement(1, [write("cat.dw.new", [edge("cat.src.events")], created=True)]),
        statement(2, [write("cat.dw.old", [edge("cat.dw.live")], op="rename"),
                      write("cat.dw.live", [edge("cat.dw.new")], op="rename")],
                  removes=["cat.dw.live", "cat.dw.new"]),
        statement(3, drops=["cat.dw.old"]),
    ])
    assert signature(result) == {("cat.src.events", "id", "cat.dw.live", "id", "value_direct")}
    assert "cat.dw.old" in result.intermediate_tables


def test_swap_uses_both_pre_swap_states_without_folding_new_names():
    result = fold_writes([
        statement(1, [write("cat.stage.left", [edge("cat.src.first")], created=True)]),
        statement(2, [write("cat.stage.right", [edge("cat.src.second")], created=True)]),
        statement(3, [write("cat.stage.left", [edge("cat.stage.right")], op="swap"),
                      write("cat.stage.right", [edge("cat.stage.left")], op="swap")],
                  removes=["cat.stage.left", "cat.stage.right"]),
    ])
    assert signature(result) == {
        ("cat.src.second", "id", "cat.stage.left", "id", "value_direct"),
        ("cat.src.first", "id", "cat.stage.right", "id", "value_direct"),
    }
    assert len(result.intermediate) == 4


def test_recreated_name_does_not_inherit_old_lifecycle():
    result = fold_writes([
        statement(1, [write("cat.stage.rows", [edge("cat.src.first")], created=True)]),
        statement(2, drops=["cat.stage.rows"]),
        statement(3, [write("cat.stage.rows", [edge("cat.src.second")], created=True)]),
        statement(4, [write("cat.dw.daily_summary", [edge("cat.stage.rows")])]),
    ])
    assert len(result.intermediate) == 1
    assert signature(result) == {
        ("cat.src.second", "id", "cat.stage.rows", "id", "value_direct"),
        ("cat.stage.rows", "id", "cat.dw.daily_summary", "id", "value_direct"),
    }


@pytest.mark.parametrize("reset", ["TRUNCATE TABLE dw.tmp", "DELETE FROM dw.tmp"])
@pytest.mark.parametrize("create", ["CREATE TABLE dw.tmp(id INT)", "CREATE TEMPORARY TABLE dw.tmp(id INT)"])
def test_data_reset_cuts_prior_sources_and_preserves_created_then_dropped_lifecycle(reset, create):
    dialect = SqlDialect("postgres", "cat", normalize=fold_identifiers("postgres"))
    schema = {"cat": {"src": {"a": {"id": "INT"}, "b": {"id": "INT"}},
                      "dw": {"tmp": {"id": "INT"}, "out": {"id": "INT"}}}}
    sql = [create, "INSERT INTO dw.tmp SELECT id FROM src.a", reset,
           "INSERT INTO dw.tmp SELECT id FROM src.b", "INSERT INTO dw.out SELECT id FROM dw.tmp",
           "DROP TABLE dw.tmp"]
    statements = []
    for seq, text in enumerate(sql):
        parsed = parse_statement(text, dialect=dialect, schema=schema)
        assert parsed.status in {"parsed", "skipped"}, parsed.reason
        statements.append(statement(seq, parsed.writes, drops=parsed.dropped_refs,
                                    removes=parsed.removed_refs, resets=parsed.reset_refs))
    before = deepcopy(statements)
    result = fold_writes(statements)
    assert signature(result) == {("cat.src.b", "id", "cat.dw.out", "id", "value_direct")}
    assert result.intermediate_tables == ["cat.dw.tmp"]
    assert len(result.intermediate) == 3
    assert statements == before


@pytest.mark.parametrize("created", [False, True])
def test_data_reset_alone_preserves_old_segment_intermediate_rules(created):
    result = fold_writes([
        statement(1, [write("cat.stage.rows", [edge("cat.src.first")], created=created)]),
        statement(2, resets=["cat.stage.rows"]),
        statement(3, [write("cat.stage.rows", [edge("cat.src.second")])]),
        statement(4, [write("cat.dw.daily_summary", [edge("cat.stage.rows")])]),
    ])
    assert signature(result) == {
        ("cat.src.first", "id", "cat.stage.rows", "id", "value_direct"),
        ("cat.src.second", "id", "cat.stage.rows", "id", "value_direct"),
        ("cat.stage.rows", "id", "cat.dw.daily_summary", "id", "value_direct"),
    }
    assert not result.intermediate


def test_reset_then_drop_existing_table_does_not_make_old_or_new_writes_intermediate():
    result = fold_writes([
        statement(1, [write("cat.stage.rows", [edge("cat.src.first")])]),
        statement(2, resets=["cat.stage.rows"]),
        statement(3, [write("cat.stage.rows", [edge("cat.src.second")])]),
        statement(4, [write("cat.dw.daily_summary", [edge("cat.stage.rows")])]),
        statement(5, drops=["cat.stage.rows"]),
    ])
    assert len(result.active) == 3
    assert not result.intermediate
    assert not result.intermediate_tables


def test_reset_before_rename_preserves_structure_lifecycle_and_only_composes_current_data():
    result = fold_writes([
        statement(1, [write("cat.stage.rows", [edge("cat.src.first")], created=True)]),
        statement(2, resets=["cat.stage.rows"]),
        statement(3, [write("cat.stage.rows", [edge("cat.src.second")])]),
        statement(4, [write("cat.dw.daily_summary", [edge("cat.stage.rows")], op="rename")],
                  removes=["cat.stage.rows"]),
    ])
    assert signature(result) == {("cat.src.second", "id", "cat.dw.daily_summary", "id", "value_direct")}
    assert len(result.intermediate) == 3


def test_filtered_delete_keeps_prior_sources_available_to_fold():
    parsed = parse_statement("DELETE FROM stage.rows WHERE id=1",
                             dialect=SqlDialect("postgres", "cat"), schema={})
    result = fold_writes([
        statement(1, [write("cat.stage.rows", [edge("cat.src.events")], temporary=True)]),
        statement(2, parsed.writes, resets=parsed.reset_refs),
        statement(3, [write("cat.dw.daily_summary", [edge("cat.stage.rows")])]),
    ])
    assert signature(result) == {("cat.src.events", "id", "cat.dw.daily_summary", "id", "value_direct")}


def test_column_matching_does_not_cross_unrelated_columns_or_future_writes():
    result = fold_writes([
        statement(1, [write("cat.stage.rows", [edge("cat.src.first", "id", "id"),
                                              edge("cat.src.other", "value", "value")], temporary=True)]),
        statement(2, [write("cat.dw.daily_summary", [edge("cat.stage.rows")])]),
        statement(3, [write("cat.stage.rows", [edge("cat.src.future")])]),
    ])
    assert signature(result) == {("cat.src.first", "id", "cat.dw.daily_summary", "id", "value_direct")}


def test_rename_of_external_table_keeps_it_as_source_endpoint():
    result = fold_writes([
        statement(1, [write("cat.dw.daily_summary", [edge("cat.src.events")], op="rename")],
                  removes=["cat.src.events"]),
    ])
    assert signature(result) == {("cat.src.events", "id", "cat.dw.daily_summary", "id", "value_direct")}
    assert len(result.intermediate) == 1
    assert result.active[0].attributes["via"] == ["cat.src.events"]
    assert result.active[0].attributes["hops"] == [result.intermediate[0].id]


def test_renamed_temporary_table_remains_intermediate_until_read():
    result = fold_writes([
        statement(1, [write("cat.stage.first", [edge("cat.src.events")], temporary=True, created=True)]),
        statement(2, [write("cat.stage.second", [edge("cat.stage.first")], op="rename")],
                  removes=["cat.stage.first"]),
        statement(3, [write("cat.dw.daily_summary", [edge("cat.stage.second")])]),
    ])
    assert signature(result) == {("cat.src.events", "id", "cat.dw.daily_summary", "id", "value_direct")}
    assert result.intermediate_tables == ["cat.stage.first", "cat.stage.second"]


def test_long_intermediate_chain_does_not_depend_on_python_recursion_limit():
    statements = [statement(0, [write("cat.stage.step_0", [edge("cat.src.events")], temporary=True)])]
    statements.extend(statement(index, [write(f"cat.stage.step_{index}", [edge(f"cat.stage.step_{index - 1}")],
                                              temporary=True)]) for index in range(1, 1100))
    statements.append(statement(1100, [write("cat.dw.daily_summary", [edge("cat.stage.step_1099")])]))
    result = fold_writes(statements)
    assert signature(result) == {("cat.src.events", "id", "cat.dw.daily_summary", "id", "value_direct")}
    assert len(result.active[0].attributes["hops"]) == 1101


@pytest.mark.parametrize("relation", ["filter", "join_key", "group_by"])
def test_upstream_influence_is_deduplicated_across_downstream_reads_and_keeps_all_paths(relation):
    statements = [
        statement(1, [write("cat.stage.rows", [
            edge("cat.src.events"),
            edge("cat.src.events", "amount", "amount"),
            edge("cat.src.flags", "flag", None, relation),
        ], temporary=True)]),
        statement(2, [write("cat.dw.daily_summary", [
            edge("cat.stage.rows"), edge("cat.stage.rows", "amount", "total"),
        ])]),
    ]
    before = deepcopy(statements)
    result = fold_writes(statements)
    assert len(result.active) == 3
    assert signature(result) == {
        ("cat.src.events", "id", "cat.dw.daily_summary", "id", "value_direct"),
        ("cat.src.events", "amount", "cat.dw.daily_summary", "total", "value_direct"),
        ("cat.src.flags", "flag", "cat.dw.daily_summary", None, relation),
    }
    influence = next(edge for edge in result.active if edge.relation == relation)
    raw = result.intermediate
    assert influence.attributes["paths"] == [[raw[2].id, raw[3].id], [raw[2].id, raw[4].id]]
    assert influence.attributes["hops"] == influence.attributes["paths"][0]
    stored_ids = [edge.id for edge in result.edges]
    assert len(stored_ids) == len(set(stored_ids))
    assert all(hop in stored_ids for edge in result.active for path in edge.attributes["paths"] for hop in path)
    first_read = fold_writes([statements[0], statement(2, [write("cat.dw.daily_summary", [edge("cat.stage.rows")])])])
    assert influence.id == next(edge.id for edge in first_read.active if edge.relation == relation)
    assert statements == before


def test_duplicate_active_edges_are_deduplicated_across_writes_in_one_statement():
    first = edge("cat.src.events")
    second = edge("cat.src.events")
    first.expression = "first expression"
    second.expression = "second expression"
    original = fold_writes([statement(1, [write("cat.dw.daily_summary", [first])])]).active[0]
    result = fold_writes([statement(1, [write("cat.dw.daily_summary", [first]),
                                       write("cat.dw.daily_summary", [second])])])
    assert len(result.active) == 1
    retained = result.active[0]
    assert retained.id == original.id
    assert retained.expression == "first expression"
    assert len(retained.attributes["paths"]) == 2
    assert retained.attributes["paths"][0] == [retained.id]
    assert retained.attributes["paths"][0] != retained.attributes["paths"][1]
    assert len(result.intermediate) == 1 and result.intermediate[0].status == "intermediate"
    assert retained.attributes["paths"][1] == [result.intermediate[0].id]
    stored_ids = [edge.id for edge in result.edges]
    assert len(stored_ids) == len(set(stored_ids))
    assert all(hop in stored_ids for path in retained.attributes["paths"] for hop in path)


def test_identical_folded_edges_in_different_statements_are_not_deduplicated():
    result = fold_writes([
        statement(1, [write("cat.stage.rows", [edge("cat.src.events")], temporary=True)]),
        statement(2, [write("cat.dw.daily_summary", [edge("cat.stage.rows")])]),
        statement(3, [write("cat.dw.daily_summary", [edge("cat.stage.rows")])], job="file-b"),
    ])
    assert len(result.active) == 2
    assert len(signature(result)) == 1
    assert [edge.statement_id for edge in result.active] == ["statement-2", "statement-3"]
    assert [edge.job_id for edge in result.active] == ["file-a", "file-b"]
    assert all(edge.attributes["paths"] == [edge.attributes["hops"]] for edge in result.active)
    assert result.active[0].attributes["paths"] != result.active[1].attributes["paths"]


def test_multiple_intermediate_paths_to_same_value_are_merged_without_losing_hops():
    result = fold_writes([
        statement(1, [write("cat.stage.first", [edge("cat.src.events")], temporary=True)]),
        statement(2, [write("cat.stage.second", [edge("cat.src.events")], temporary=True)]),
        statement(3, [write("cat.stage.merged", [edge("cat.stage.first"), edge("cat.stage.second")], temporary=True)]),
        statement(4, [write("cat.dw.daily_summary", [edge("cat.stage.merged")])]),
    ])
    assert len(result.active) == 1
    raw = result.intermediate
    assert result.active[0].attributes["paths"] == [
        [raw[0].id, raw[2].id, raw[4].id],
        [raw[1].id, raw[3].id, raw[4].id],
    ]
    assert result.active[0].attributes["hops"] == result.active[0].attributes["paths"][0]
