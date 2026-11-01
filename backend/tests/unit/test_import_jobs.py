"""导入任务复用 worker 的领取与终态，不访问数据库。"""

from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from avelis import jobs
from avelis.importer import batch


@pytest.mark.parametrize("failure", [False, True])
def test_worker_dispatches_import_with_claimed_attempt_and_records_terminal_status(monkeypatch, failure):
    executed, received = [], []
    task = SimpleNamespace(kind="import", payload={"batch_id": "batch-1"}, created_by="actor-1",
                           attempts=3, result={"existing": "kept"})

    class Session:
        def execute(self, statement):
            executed.append(statement)
            return SimpleNamespace(rowcount=1)

        def get(self, model, identifier):
            assert identifier == "worker-job-1"
            return task

    @contextmanager
    def scope():
        yield Session()

    def run_import(batch_id, *, job_id, attempt):
        received.append((batch_id, job_id, attempt))
        if failure:
            raise RuntimeError("模拟导入失败")
        return {"batch_id": batch_id, "status": "done"}

    monkeypatch.setattr(jobs, "session_scope", scope)
    monkeypatch.setattr(batch, "run_import", run_import)
    jobs.run_job("worker-job-1")
    assert received == [("batch-1", "worker-job-1", 3)]
    terminal = executed[-1].compile().params
    assert terminal["status"] == ("failed" if failure else "succeeded")
    assert terminal["result"]["existing"] == "kept"
    assert terminal["error"] == ("RuntimeError: 模拟导入失败" if failure else None)
    if not failure:
        assert terminal["result"]["batch_id"] == "batch-1"
