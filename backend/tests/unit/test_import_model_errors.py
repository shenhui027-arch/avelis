"""模型候选失败时，写入导入任务前先清理凭据。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from avelis import modelcfg
from avelis.importer import service


@pytest.mark.parametrize("use_model", [False, True])
def test_candidate_failure_persists_only_redacted_errors(monkeypatch, use_model):
    api_key = "private-import-api-key"
    resolved = modelcfg.Resolved(object(), "test:model", api_key) if use_model else None
    monkeypatch.setattr(modelcfg, "policy", lambda session: {"send_code": True})
    monkeypatch.setattr(modelcfg, "resolve", lambda session, purpose: resolved)
    message = ("Authorization: Bearer private-bearer-token\n"
               "request https://private-user:private-password@model.example/v1 failed")
    if use_model:
        message += f" with {api_key}"
    message += "x" * 300

    def fail(*args, **kwargs):
        raise RuntimeError(message)

    monkeypatch.setattr(service, "model_candidates", fail)
    monkeypatch.setattr(service, "demo_candidates", fail)
    job = SimpleNamespace(model_payload=[], model_adapter=None)
    stmt = SimpleNamespace(id="statement-1", line_start=1, line_end=1, reason=None,
                           raw_text='df.write.saveAsTable("report")')
    service._model_candidates(object(), job, stmt, {}, "shop", "default_catalog")
    assert stmt.reason.startswith("模型推断失败：RuntimeError:")
    assert len(stmt.reason.removeprefix("模型推断失败：")) <= 200
    assert len(job.model_payload) == 1
    assert job.model_payload[0]["error"] == stmt.reason
    for secret in (api_key, "private-bearer-token", "private-user", "private-password"):
        assert secret not in stmt.reason
        assert secret not in str(job.model_payload)
