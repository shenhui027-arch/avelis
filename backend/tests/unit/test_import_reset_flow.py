"""清空表的数据生命周期经过批次解析、结构回放和折叠后仍保持一致。"""

from copy import deepcopy

import pytest

from avelis.importer import batch
from avelis.models import ExtractedStatement, LineageEdge
from tests.unit.test_import_batch_service import create, memory  # noqa: F401


@pytest.mark.parametrize("reset", ["TRUNCATE TABLE stage.buffer", "DELETE FROM stage.buffer"])
def test_batch_reset_cuts_old_sources_and_preserves_target_schema(memory, monkeypatch, reset):
    schema = {"default_catalog": {"src": {
        "before": {"id": "INT"}, "after": {"id": "INT"},
    }}}
    monkeypatch.setattr(batch, "build_schema", lambda *_: deepcopy(schema))
    imported = create(memory, f"""
CREATE TABLE stage.buffer (id INT);
INSERT INTO stage.buffer SELECT id FROM src.before;
{reset};
INSERT INTO stage.buffer SELECT id AS renamed FROM src.after;
CREATE TABLE dw.summary AS SELECT * FROM stage.buffer;
DROP TABLE stage.buffer;
""")

    batch.run_import(imported.id)

    assert imported.status == "done"
    statements = sorted(memory.rows[ExtractedStatement], key=lambda stmt: stmt.seq)
    assert statements[2].status == "skipped"
    assert statements[2].reason == "清空表数据，之前的来源不再传递"
    assert statements[3].writes[0]["target_columns"] == ["id"]
    active = [edge for edge in memory.rows[LineageEdge] if edge.status == "active"]
    assert {(edge.source_ref, edge.source_column, edge.target_ref, edge.target_column) for edge in active} == {
        ("default_catalog.src.after", "id", "default_catalog.dw.summary", "id"),
    }
