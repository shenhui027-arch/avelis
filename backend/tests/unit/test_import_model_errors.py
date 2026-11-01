"""模型候选失败时，写入导入任务前先清理凭据。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from avelis import modelcfg
from avelis.importer import assist
from avelis.lineage.sql import SqlDialect


@pytest.mark.parametrize("use_model", [False, True])
def test_candidate_failure_persists_only_redacted_errors(monkeypatch, use_model):
    api_key = "private-import-api-key"
    resolved = modelcfg.Resolved(object(), "test:model", api_key) if use_model else None
    message = ("Authorization: Bearer private-bearer-token\n"
               "request https://private-user:private-password@model.example/v1 failed")
    if use_model:
        message += f" with {api_key}"
    message += "x" * 300

    def fail(*args, **kwargs):
        raise RuntimeError(message)

    monkeypatch.setattr(assist, "model_candidates", fail)
    monkeypatch.setattr(assist, "demo_candidates", fail)
    stmt = SimpleNamespace(id="statement-1", line_start=1, line_end=1, reason=None,
                           method="unrecognized", raw_text='df.write.saveAsTable("report")')
    result = assist.run_assistance(language="python", content=stmt.raw_text, rule_statements=[stmt],
                                   dialect=SqlDialect("postgres", "default_catalog"), schema={}, default_db="dw",
                                   options={}, policy={"send_code": True}, resolved=resolved)
    reason = result.statement_updates[stmt.id]["reason"]
    assert reason.startswith("模型推断失败：RuntimeError:")
    assert len(reason.removeprefix("模型推断失败：")) <= 200
    assert len(result.payloads) == 1
    assert result.payloads[0]["error"] == reason
    for secret in (api_key, "private-bearer-token", "private-user", "private-password"):
        assert secret not in reason
        assert secret not in str(result.payloads)
