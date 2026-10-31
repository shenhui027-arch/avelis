"""E2 运行器的离线单测：隔离配置文件、模型、数据库和演示状态目录。"""

from __future__ import annotations

import builtins
import hashlib
import importlib.util
import json
import os
import sys
from collections import Counter
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from avelis import config

ROOT = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location("e2_runner", ROOT / "experiments" / "agent-v2" / "e2.py")
e2 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(e2)

ENV_RETRIEVAL = "AVELIS_ANALYSIS_RETRIEVAL"
ENV_INVESTIGATION = "AVELIS_ANALYSIS_INVESTIGATION"
ENV_NAMES = (ENV_RETRIEVAL, ENV_INVESTIGATION)
FROZEN_SCENARIOS = (
    "h10", "e01", "h07", "f02", "h09", "d02", "e02", "h05",
    "d06", "f01", "c02", "g01", "d03", "n01", "a02", "h12",
)
EXPECTED_FLAGS = {
    "B0": {"analysis_retrieval": False, "analysis_investigation": False},
    "R": {"analysis_retrieval": True, "analysis_investigation": False},
    "H": {"analysis_retrieval": False, "analysis_investigation": True},
    "RH": {"analysis_retrieval": True, "analysis_investigation": True},
}


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch):
    # 保留真实的解析与缓存，只屏蔽配置文件读取，并隔离宿主的连接配置。
    monkeypatch.setattr(config, "_read_env_file", lambda path: {})
    monkeypatch.setenv("AVELIS_SECRET", "e2-unit-test-secret")
    monkeypatch.setenv("AVELIS_DATABASE_URL", "postgresql+psycopg://unit@127.0.0.1/unit")
    monkeypatch.delenv(ENV_RETRIEVAL, raising=False)
    monkeypatch.delenv(ENV_INVESTIGATION, raising=False)
    config.get_settings.cache_clear()
    yield
    config.get_settings.cache_clear()


def settings_flags():
    settings = config.get_settings()
    return {field: getattr(settings, field) for field in EXPECTED_FLAGS["B0"]}


def set_initial_env(monkeypatch, values):
    for name, value in zip(ENV_NAMES, values):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    config.get_settings.cache_clear()


def forbid_run_imports(monkeypatch):
    original_import = builtins.__import__

    def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name in {"demo", "eval_analysis", "tests.eval.scoring_v2_1_3"} or (
            name == "tests.eval" and "scoring_v2_1_3" in (fromlist or ())
        ):
            pytest.fail(f"计划或参数检查不应导入运行依赖：{name}")
        return original_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", guarded_import)


def test_scenarios_are_the_frozen_16_and_include_all_six_e1_scenarios():
    assert e2.SCENARIOS == FROZEN_SCENARIOS
    assert len(set(e2.SCENARIOS)) == 16
    assert {"h10", "e01", "h07", "f02", "h09", "d02"} <= set(e2.SCENARIOS)
    assert e2.CONDITIONS == ("B0", "R", "H", "RH")
    assert e2.FLAGS == EXPECTED_FLAGS
    assert e2.SEED == 20261101


def test_plan_is_deterministic_and_contains_every_cell_once_per_repeat():
    order = e2.plan(list(e2.SCENARIOS), list(e2.CONDITIONS))
    assert len(order) == 128
    assert order == e2.plan(list(e2.SCENARIOS), list(e2.CONDITIONS), 2, 20261101)
    expected = Counter((s, c) for s in e2.SCENARIOS for c in e2.CONDITIONS)
    assert [item["repeat"] for item in order] == [1] * 64 + [2] * 64
    for repeat in (1, 2):
        assert Counter((item["scenario"], item["condition"]) for item in order
                       if item["repeat"] == repeat) == expected
    assert order != e2.plan(list(e2.SCENARIOS), list(e2.CONDITIONS), seed=20261102)


def test_plan_supports_a_small_retest():
    order = e2.plan(["h10", "h12"], ["B0", "RH"], 1)
    assert len(order) == 4
    assert Counter((item["scenario"], item["condition"]) for item in order) == Counter(
        (s, c) for s in ("h10", "h12") for c in ("B0", "RH")
    )
    assert all(item["repeat"] == 1 for item in order)


def test_b0_plan_contains_32_deterministic_runs(monkeypatch, capsys):
    forbid_run_imports(monkeypatch)
    monkeypatch.setattr(sys, "argv", ["e2.py", "--plan", "--conditions", "B0"])
    assert e2.main() == 0
    order = json.loads(capsys.readouterr().out)["order"]
    assert order == e2.plan(list(FROZEN_SCENARIOS), ["B0"], 2, e2.SEED)
    assert len(order) == 32
    assert [item["repeat"] for item in order] == [1] * 16 + [2] * 16
    assert Counter((item["scenario"], item["condition"]) for item in order) == Counter(
        {(scenario, "B0"): 2 for scenario in FROZEN_SCENARIOS}
    )


@pytest.mark.parametrize(("scenarios", "conditions", "repeat"), [
    (list(FROZEN_SCENARIOS), list(EXPECTED_FLAGS), 3),
    (["h10"], ["B0"], 129),
    ([], ["B0"], 1),
    (["h10"], [], 1),
    (["h10", "h10"], ["B0"], 1),
    (["h10"], ["B0", "B0"], 1),
    (["unknown"], ["B0"], 1),
    (["h10"], ["D0"], 1),
    (["h10"], ["B0"], 0),
    (["h10"], ["B0"], -1),
])
def test_plan_rejects_invalid_selections_or_more_than_128_runs(scenarios, conditions, repeat):
    with pytest.raises(ValueError):
        e2.plan(scenarios, conditions, repeat)


@pytest.mark.parametrize("condition", EXPECTED_FLAGS)
@pytest.mark.parametrize(("initial", "baseline"), [
    ((None, None), {"analysis_retrieval": False, "analysis_investigation": False}),
    ((" YeS ", "0"), {"analysis_retrieval": True, "analysis_investigation": False}),
    ((None, " TrUe "), {"analysis_retrieval": False, "analysis_investigation": True}),
    (("FALSE", None), {"analysis_retrieval": False, "analysis_investigation": False}),
])
def test_install_switches_real_settings_and_restores_original_env(monkeypatch, condition, initial, baseline):
    set_initial_env(monkeypatch, initial)
    assert settings_flags() == baseline  # 先填充缓存，验证进入时确实清理缓存。
    with e2.install(condition) as flags:
        assert flags == EXPECTED_FLAGS[condition]
        assert settings_flags() == EXPECTED_FLAGS[condition]
        assert tuple(os.environ[name] for name in ENV_NAMES) == tuple(
            str(value).lower() for value in EXPECTED_FLAGS[condition].values()
        )
    assert tuple(os.environ.get(name) for name in ENV_NAMES) == initial
    assert settings_flags() == baseline  # 退出后再次读取，不能沿用实验条件的缓存。


@pytest.mark.parametrize("condition", EXPECTED_FLAGS)
def test_install_restores_env_and_settings_when_the_body_raises(monkeypatch, condition):
    initial = (" YeS ", None)
    set_initial_env(monkeypatch, initial)
    with pytest.raises(RuntimeError, match="模拟运行失败"):
        with e2.install(condition):
            assert settings_flags() == EXPECTED_FLAGS[condition]
            raise RuntimeError("模拟运行失败")
    assert tuple(os.environ.get(name) for name in ENV_NAMES) == initial
    assert settings_flags() == {"analysis_retrieval": True, "analysis_investigation": False}


def test_install_restores_env_when_its_entry_assertion_fails(monkeypatch):
    initial = (None, " TrUe ")
    set_initial_env(monkeypatch, initial)
    build_settings = config.build_settings
    monkeypatch.setattr(config, "build_settings", lambda: replace(build_settings(), analysis_retrieval=False))
    with pytest.raises(RuntimeError, match="不匹配"):
        with e2.install("RH"):
            pytest.fail("开关失配时不能开始运行")
    assert tuple(os.environ.get(name) for name in ENV_NAMES) == initial
    assert settings_flags() == {"analysis_retrieval": False, "analysis_investigation": True}


def test_assert_flags_detects_a_change_before_the_next_request():
    with e2.install("RH"):
        assert e2.assert_flags("RH") == EXPECTED_FLAGS["RH"]
        os.environ[ENV_RETRIEVAL] = "false"  # 由 install 恢复，避免测试清理时重新写回实验值。
        config.get_settings.cache_clear()
        with pytest.raises(RuntimeError, match="不匹配"):
            e2.assert_flags("RH")


@pytest.mark.parametrize("with_truth", [False, True])
def test_plan_cli_does_not_read_truth_or_import_run_dependencies(monkeypatch, tmp_path, capsys, with_truth):
    from avelis.analysis import tools

    memory_settings = config.build_settings({"AVELIS_SECRET": "offline-unit-test-secret-0123456789"})
    monkeypatch.setattr(tools, "get_settings", lambda: memory_settings)
    forbid_run_imports(monkeypatch)

    def forbidden(*args, **kwargs):
        pytest.fail("计划不应读取配置文件或 E1 注入材料")

    monkeypatch.setattr(config, "_read_env_file", forbidden)
    monkeypatch.setattr(sys.modules["e1"], "load_material", forbidden)
    argv = ["e2.py", "--plan"]
    if with_truth:
        argv += ["--truth", str(tmp_path / "does-not-exist.json")]
    monkeypatch.setattr(sys, "argv", argv)
    assert e2.main() == 0
    data = json.loads(capsys.readouterr().out)
    assert data["order"] == e2.plan(list(e2.SCENARIOS), list(e2.CONDITIONS))
    assert data["bounds"] == e2.budget_bounds(128)
    assert data["bounds"]["model_requests"] == 128 * 16
    assert data["bounds"]["seconds_wall"] == 128 * 300
    assert data["ledger"] == {
        "cap_cny": "20", "spent_cny": "0", "per_run_reserve_cny": "1.5",
        "remaining_cny": "20", "can_start": True,
    }


@pytest.mark.parametrize("args", [
    ["--run"],
    ["--plan", "--run"],
    ["--plan", "--repeat", "3"],
    ["--plan", "--only", "unknown"],
    ["--plan", "--conditions", "D0"],
    ["--plan", "--effort", "high"],
    ["--plan", "--output-token-cap", "0"],
    ["--plan", "--output-token-cap", "-1"],
    ["--plan", "--output-token-cap", "1.5"],
    ["--plan", "--model-timeout", "0"],
    ["--plan", "--model-timeout", "-1"],
    ["--plan", "--model-timeout", "nan"],
    ["--plan", "--model-timeout", "inf"],
    ["--plan", "--model-timeout=-inf"],
    ["--plan", "--run-seconds", "0"],
    ["--plan", "--run-seconds", "-1"],
    ["--plan", "--run-seconds", "1.5"],
    ["--plan", "--run-seconds", "nan"],
    ["--plan", "--run-seconds", "inf"],
    ["--plan", "--tag", ""],
    ["--plan", "--tag", "."],
    ["--plan", "--tag", ".."],
    ["--plan", "--tag", "../outside"],
    ["--plan", "--tag", "one/two"],
    ["--plan", "--tag", "one\\two"],
    ["--plan", "--tag", "/outside"],
    ["--plan", "--tag", "C:\\outside"],
    ["--plan", "--tag", "C:outside"],
])
def test_cli_rejects_invalid_arguments_before_importing_run_dependencies(monkeypatch, args):
    forbid_run_imports(monkeypatch)
    monkeypatch.setattr(sys, "argv", ["e2.py", *args])
    with pytest.raises(SystemExit) as exc:
        e2.main()
    assert exc.value.code == 2


COSTED_STEP = {
    "kind": "model", "status": "ok", "at": "2026-10-30T10:00:00+08:00",
    "output": {"input_tokens": 300_000, "cache_hit_tokens": 0, "output_tokens": 0},
}
COSTED_RUN = {"id": "unit-e2-run", "status": "succeeded", "steps": [COSTED_STEP]}


@pytest.fixture
def fake_runtime(monkeypatch, tmp_path):
    import avelis
    import avelis.db as db
    import tests.eval as eval_package
    from avelis.models import ModelAssignment, User

    truth = {"unit": "明确传入的测试真值"}
    truth_path = tmp_path / "truth.json"
    truth_path.write_text(json.dumps(truth), encoding="utf-8")
    scorer_path = tmp_path / "scorer.py"
    scorer_path.write_text("# 测试评分器\n", encoding="utf-8")
    questions = [{"id": scenario, "user": "unit-user", "question": scenario,
                  "window": {"start": "2026-10-01", "end": "2026-10-31"}} for scenario in e2.SCENARIOS]
    state = SimpleNamespace(run=COSTED_RUN, asks=[], scores=[], clients=[], logins=[], truths=[], saves=[],
                            truth_path=truth_path, scorer_path=scorer_path, questions=questions,
                            events=[], settings_saves=[], sessions=[], save_paths=[], client_effort=None,
                            assignment=SimpleNamespace(purpose="analysis", provider_id="before-client-provider",
                                                       model_id="before-client-model", reasoning_effort="high"),
                            admin=SimpleNamespace(id="unit-admin"))

    class FakeSession:
        def get(self, model, key):
            if model is User:
                assert key == state.admin.id
                return state.admin
            assert model is ModelAssignment and key == "analysis"
            return state.assignment

    @contextmanager
    def session_scope():
        session = FakeSession()
        state.sessions.append(session)
        yield session

    def ensure_admin(*, superadmin):
        assert superadmin is False
        state.events.append("ensure_admin")
        return state.admin.id

    def save_settings(session, actor, assignments, new_policy):
        assert session in state.sessions and actor is state.admin and new_policy is None
        assert set(assignments) == {"analysis"}
        target = dict(assignments["analysis"])
        state.settings_saves.append((session, target))
        state.events.append(("save_settings", target["reasoning_effort"]))
        for name, value in target.items():
            setattr(state.assignment, name, value)

    def forbidden(*args, **kwargs):
        pytest.fail("E2 不应读取演示环境或 E1 注入材料")

    def load_questions(value):
        state.truths.append(value)
        return {"questions": questions}

    def client(env):
        state.clients.append(True)
        state.events.append("client")
        # 模拟 model_from_env 重写指派；必须等它执行后再取模型及原思考强度。
        state.assignment.provider_id = "unit-provider"
        state.assignment.model_id = "unit-model"
        state.assignment.reasoning_effort = state.client_effort
        return object()

    def login(client, user, password):
        state.logins.append((user, password))
        return {"Authorization": "unit-token"}

    def ask(client, headers, question):
        state.asks.append((question, settings_flags()))
        if isinstance(state.run, BaseException):
            raise state.run
        if callable(state.run):
            return state.run()
        return state.run

    def score(question, run):
        state.scores.append((question, run))
        return {"passed": bool(run and run.get("status") == "succeeded")}

    monkeypatch.setenv("AVELIS_DEMO_USER_PASSWORD", "unit-password")
    monkeypatch.setitem(sys.modules, "demo", SimpleNamespace(STATE=tmp_path, load_env=forbidden))
    monkeypatch.setitem(sys.modules, "eval_analysis", SimpleNamespace(
        _ask=ask, _client=client, _login=login,
        code_version=lambda: {"git_sha": "unit-sha", "git_dirty": True},
    ))
    scorer = SimpleNamespace(VERSION="unit-v2.1.3", __file__=str(scorer_path),
                             load_questions=load_questions, score=score)
    monkeypatch.setattr(eval_package, "scoring_v2_1_3", scorer, raising=False)
    monkeypatch.setitem(sys.modules, "tests.eval.scoring_v2_1_3", scorer)
    bootstrap = SimpleNamespace(ensure_admin=ensure_admin)
    modelcfg = SimpleNamespace(save_settings=save_settings)
    monkeypatch.setitem(sys.modules, "avelis.bootstrap", bootstrap)
    monkeypatch.setattr(avelis, "bootstrap", bootstrap, raising=False)
    monkeypatch.setitem(sys.modules, "avelis.modelcfg", modelcfg)
    monkeypatch.setattr(avelis, "modelcfg", modelcfg, raising=False)
    monkeypatch.setattr(db, "session_scope", session_scope)
    monkeypatch.setattr(sys.modules["e1"], "load_material", forbidden)
    write_text = Path.write_text

    def capture_save(path, text, *args, **kwargs):
        if path.parent.parent == tmp_path / "experiments":
            state.saves.append(json.loads(text))
            state.save_paths.append(path)
        return write_text(path, text, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", capture_save)
    return state


@pytest.mark.parametrize(("spent", "run", "expected_runs", "known_cost", "accounted_cost", "reason"), [
    ("0", COSTED_RUN, 1, "0.6", "0.6", None),
    ("1", COSTED_RUN, 0, "0", "0", None),
    ("0", None, 1, "0", "1.5", "未取得运行详情"),
    ("0", {**COSTED_RUN, "steps": [COSTED_STEP, {"kind": "model", "status": "error", "output": {}}]},
     1, "0.6", "2.1", "1 个模型步骤缺少用量"),
])
def test_run_accounts_cost_and_stops_when_the_next_reserve_does_not_fit(
    monkeypatch, fake_runtime, spent, run, expected_runs, known_cost, accounted_cost, reason,
):
    state = fake_runtime
    state.run = run
    monkeypatch.setattr(sys, "argv", ["e2.py", "--run", "--truth", str(state.truth_path),
                                      "--only", "h10", "--conditions", "B0,R", "--repeat", "1",
                                      "--cap", "2", "--spent", spent])
    assert e2.main() == 0
    assert len(state.asks) == len(state.scores) == expected_runs
    assert len(state.clients) == len(state.logins) == expected_runs
    data = state.saves[-1]
    assert len(data["results"]) == expected_runs
    assert "费用预留不足" in data["stop_reason"]
    assert Decimal(data["ledger"]["spent_cny"]) == Decimal(spent) + Decimal(accounted_cost)
    if expected_runs:
        result = data["results"][0]
        assert Decimal(result["cost"]["total_cny"]) == Decimal(known_cost)
        assert Decimal(result["cost_accounted_cny"]) == Decimal(accounted_cost)
        assert result["cost_estimated"] is bool(reason)
        assert result["cost_estimate_reason"] == reason
        assert result["run"] == run
        assert result["ledger"] == data["ledger"]


def test_run_saves_each_result_and_preserves_failed_run_without_retry(monkeypatch, fake_runtime):
    state = fake_runtime
    state.run = {**COSTED_RUN, "status": "failed", "error": "保留原始失败"}
    initial = (" YeS ", None)
    set_initial_env(monkeypatch, initial)
    monkeypatch.setattr(sys, "argv", ["e2.py", "--run", "--truth", str(state.truth_path),
                                      "--only", "h10", "--conditions", "H,RH", "--repeat", "1"])
    assert e2.main() == 0
    assert len(state.asks) == len(state.scores) == 2
    assert len(state.clients) == 1
    assert [len(snapshot["results"]) for snapshot in state.saves] == [1, 2, 2]
    data = state.saves[-1]
    assert data["stop_reason"] is None
    assert state.truths == [{"unit": "明确传入的测试真值"}]
    assert state.logins == [("unit-user", "unit-password")] * 2
    assert tuple(os.environ.get(name) for name in ENV_NAMES) == initial
    for result, (question, flags), scored in zip(data["results"], state.asks, state.scores):
        assert question is state.questions[0]
        assert scored == (question, state.run)
        assert result["flags"] == flags == EXPECTED_FLAGS[result["condition"]]
        assert result["run"] == state.run and result["run_id"] == "unit-e2-run"
        assert result["score"] == {"passed": False}
    meta = data["meta"]
    assert meta["git_sha"] == "unit-sha" and meta["git_dirty"] is True
    assert meta["conditions"] == ["H", "RH"] and meta["scenarios"] == ["h10"]
    assert meta["flags"] == {condition: EXPECTED_FLAGS[condition] for condition in ("H", "RH")}
    assert meta["seed"] == 20261101 and meta["initial_spent_cny"] == "0"
    assert meta["scorer"] == "unit-v2.1.3"
    assert meta["scorer_sha256"] == hashlib.sha256(state.scorer_path.read_bytes()).hexdigest()


def test_transport_exception_is_saved_and_not_retried(monkeypatch, fake_runtime):
    state = fake_runtime
    state.run = RuntimeError("模拟传输失败")
    monkeypatch.setattr(sys, "argv", ["e2.py", "--run", "--truth", str(state.truth_path),
                                      "--only", "h10", "--conditions", "RH", "--repeat", "1"])
    with pytest.raises(RuntimeError, match="模拟传输失败"):
        e2.main()
    assert len(state.asks) == 1 and state.scores == []
    assert "RuntimeError: 模拟传输失败" in state.saves[-1]["stop_reason"]
    assert state.saves[-1]["results"] == []
    assert all(name not in os.environ for name in ENV_NAMES)
    assert settings_flags() == EXPECTED_FLAGS["B0"]


def test_run_rechecks_flags_before_asking_and_restores_env_on_mismatch(monkeypatch, fake_runtime):
    state = fake_runtime
    initial = (" YeS ", None)
    set_initial_env(monkeypatch, initial)
    evaluation = sys.modules["eval_analysis"]
    original_login = evaluation._login

    def login_then_change_flags(*args):
        headers = original_login(*args)
        assert settings_flags() == EXPECTED_FLAGS["RH"]  # 进入条件时的核对已经通过。
        os.environ[ENV_RETRIEVAL] = "false"
        config.get_settings.cache_clear()
        return headers

    monkeypatch.setattr(evaluation, "_login", login_then_change_flags)
    monkeypatch.setattr(sys, "argv", ["e2.py", "--run", "--truth", str(state.truth_path),
                                      "--only", "h10", "--conditions", "RH", "--repeat", "1"])
    with pytest.raises(RuntimeError, match="不匹配"):
        e2.main()
    assert len(state.clients) == len(state.logins) == 1
    assert state.asks == state.scores == []
    assert state.saves[-1]["results"] == []
    assert "不匹配" in state.saves[-1]["stop_reason"]
    assert tuple(os.environ.get(name) for name in ENV_NAMES) == initial
    assert settings_flags() == {"analysis_retrieval": True, "analysis_investigation": False}


def run_fake(monkeypatch, state, *args):
    monkeypatch.setattr(sys, "argv", ["e2.py", "--run", "--truth", str(state.truth_path),
                                      "--only", "h10", "--conditions", "B0,R", "--repeat", "1", *args])
    return e2.main()


def live_run(state):
    from avelis.analysis.tools import Budget

    return {**COSTED_RUN, "reasoning_effort": state.assignment.reasoning_effort,
            "budget": Budget().view()}


def test_default_run_does_not_save_settings_or_change_budget(monkeypatch, fake_runtime):
    from avelis.analysis import agent
    from avelis.analysis.tools import Budget

    state = fake_runtime
    defaults = Budget.__init__.__defaults__
    limits = Budget().view()["limits"]
    assert limits["output_tokens"] == 30_716
    assert limits["seconds"] == 300
    assert agent.MODEL_TIMEOUT_SECONDS == 60

    def run():
        assert Budget.__init__.__defaults__ is defaults
        assert Budget().view()["limits"] == limits
        assert agent.MODEL_TIMEOUT_SECONDS == 60
        return live_run(state)

    state.run = run
    assert run_fake(monkeypatch, state) == 0
    assert state.settings_saves == state.sessions == []
    assert state.events == ["client"]
    assert Budget.__init__.__defaults__ is defaults
    assert Budget().view()["limits"] == limits
    assert agent.MODEL_TIMEOUT_SECONDS == 60
    meta = state.saves[-1]["meta"]
    assert meta["effort"] is None and meta["output_token_cap"] is None and meta["tag"] == "e2"
    assert meta["model_timeout"] is None and meta["run_seconds"] is None


@pytest.mark.parametrize("effort", ["low", "max"])
@pytest.mark.parametrize("original", [None, "high"])
def test_effort_is_set_after_client_and_restored_without_changing_model(monkeypatch, fake_runtime, effort, original):
    state = fake_runtime
    state.client_effort = original
    state.run = lambda: live_run(state)
    assert run_fake(monkeypatch, state, "--effort", effort) == 0
    assert state.events == ["client", "ensure_admin", ("save_settings", effort), ("save_settings", original)]
    assert len(state.settings_saves) == 2
    set_session, set_target = state.settings_saves[0]
    restore_session, restore_target = state.settings_saves[1]
    assert set_session is not restore_session
    assert set_target == {"provider_id": "unit-provider", "model_id": "unit-model", "reasoning_effort": effort}
    assert restore_target == {**set_target, "reasoning_effort": original}
    assert vars(state.assignment) == {"purpose": "analysis", **restore_target}
    data = state.saves[-1]
    assert len(state.asks) == len(data["results"]) == 2
    assert data["stop_reason"] is None and data["meta"]["effort"] == effort
    assert all(result["run"]["reasoning_effort"] == effort for result in data["results"])


@pytest.mark.parametrize("failure", [RuntimeError("模拟传输失败"), KeyboardInterrupt("模拟中断")])
def test_run_overrides_are_restored_on_exception_or_interrupt(monkeypatch, fake_runtime, failure):
    from avelis.analysis import agent
    from avelis.analysis.tools import Budget

    state = fake_runtime
    state.client_effort = "low"
    monkeypatch.setattr(agent, "MODEL_TIMEOUT_SECONDS", 47.0)
    defaults = Budget.__init__.__defaults__
    limits = Budget().view()["limits"]

    def run():
        assert state.assignment.reasoning_effort == "max"
        assert Budget().output_tokens == 90_000
        assert Budget().seconds == 42
        assert agent.MODEL_TIMEOUT_SECONDS == 17.5
        raise failure

    state.run = run
    with pytest.raises(type(failure), match=str(failure)):
        run_fake(monkeypatch, state, "--effort", "max", "--output-token-cap", "90000",
                 "--model-timeout", "17.5", "--run-seconds", "42")
    assert len(state.asks) == 1 and state.scores == []
    assert state.assignment.reasoning_effort == "low"
    assert [target["reasoning_effort"] for _, target in state.settings_saves] == ["max", "low"]
    assert Budget.__init__.__defaults__ is defaults
    assert Budget().view()["limits"] == limits
    assert agent.MODEL_TIMEOUT_SECONDS == 47.0
    assert type(failure).__name__ in state.saves[-1]["stop_reason"]
    assert state.saves[-1]["results"] == []


def test_output_cap_changes_only_new_budget_output_default_and_restores_it(monkeypatch, fake_runtime):
    from avelis.analysis.tools import Budget

    state = fake_runtime
    defaults = Budget.__init__.__defaults__
    existing = Budget()
    limits = existing.view()["limits"]
    expected = {**limits, "output_tokens": 90_000}

    def run():
        assert Budget().view()["limits"] == expected
        assert Budget(output_tokens=123).output_tokens == 123
        assert existing.view()["limits"] == limits
        return live_run(state)

    state.run = run
    assert run_fake(monkeypatch, state, "--output-token-cap", "90000") == 0
    assert state.settings_saves == []
    assert Budget.__init__.__defaults__ is defaults
    assert Budget().view()["limits"] == limits
    data = state.saves[-1]
    assert data["meta"]["output_token_cap"] == 90_000
    assert data["meta"]["effort"] is None
    assert all(result["run"]["budget"]["limits"] == expected for result in data["results"])


def test_plan_output_cap_updates_bounds_and_restores_budget(monkeypatch, capsys):
    from avelis.analysis.tools import Budget

    forbid_run_imports(monkeypatch)
    defaults = Budget.__init__.__defaults__
    baseline = e2.budget_bounds(32)
    monkeypatch.setattr(sys, "argv", ["e2.py", "--plan", "--conditions", "B0", "--effort", "max",
                                      "--output-token-cap", "90000"])
    assert e2.main() == 0
    data = json.loads(capsys.readouterr().out)
    assert data["bounds"] == {**baseline, "output_tokens": 32 * 90_000}
    assert Budget.__init__.__defaults__ is defaults
    assert e2.budget_bounds(32) == baseline


def test_model_timeout_is_read_for_each_request_and_retry_and_restores_original(monkeypatch):
    from avelis.analysis import agent

    monkeypatch.setattr(agent, "MODEL_TIMEOUT_SECONDS", 47.0)
    requests = []
    model = SimpleNamespace(factory=lambda **kwargs: requests.append(kwargs) or "stub-model")
    budget = SimpleNamespace(remaining_output_tokens=lambda: None, remaining=lambda: 80,
                             input_tokens=None, used_model_requests=0, model_blocked=lambda: False)
    steps = []
    toolbox = SimpleNamespace(budget=budget, stopped=False, _step=lambda *args, **kwargs: steps.append(args),
                              check_executor=lambda: None, consume_messages=lambda: None,
                              consume_compact_request=lambda: False, interjections=[])

    def fail(messages):
        raise TimeoutError("模拟请求超时")

    bound = SimpleNamespace(invoke=fail)
    with e2.install_model_timeout(17.5):
        assert agent._model_for(model, budget) == ("stub-model", False)
        assert requests[-1]["timeout"] == 17.5 and requests[-1]["max_retries"] == 0
        with pytest.raises(TimeoutError, match="模拟请求超时"):
            agent._invoke(bound, [], toolbox)
        assert budget.used_model_requests == 2
        assert [step[4]["retry"] for step in steps] == [True, False]

    assert agent.MODEL_TIMEOUT_SECONDS == 47.0
    assert agent._model_for(model, budget) == ("stub-model", False)
    assert requests[-1]["timeout"] == 47.0
    steps.clear()
    budget.used_model_requests = 0
    with pytest.raises(TimeoutError, match="模拟请求超时"):
        agent._invoke(bound, [], toolbox)
    assert budget.used_model_requests == 1 and steps[0][4]["retry"] is False


def test_timeout_and_run_seconds_apply_to_new_budgets_and_restore(monkeypatch, fake_runtime):
    from avelis.analysis import agent
    from avelis.analysis.tools import Budget

    state = fake_runtime
    defaults = Budget.__init__.__defaults__
    original_timeout = agent.MODEL_TIMEOUT_SECONDS
    existing = Budget()
    limits = existing.view()["limits"]
    expected = {**limits, "output_tokens": 90_000, "seconds": 42}

    def run():
        assert agent.MODEL_TIMEOUT_SECONDS == 17.5
        assert Budget().view()["limits"] == expected
        assert Budget(seconds=123, output_tokens=456).seconds == 123
        assert Budget(seconds=123, output_tokens=456).output_tokens == 456
        assert existing.view()["limits"] == limits
        return live_run(state)

    state.run = run
    assert run_fake(monkeypatch, state, "--model-timeout", "17.5", "--run-seconds", "42",
                    "--output-token-cap", "90000") == 0
    assert agent.MODEL_TIMEOUT_SECONDS == original_timeout
    assert Budget.__init__.__defaults__ is defaults
    assert Budget().view()["limits"] == limits
    data = state.saves[-1]
    assert data["meta"]["model_timeout"] == 17.5 and data["meta"]["run_seconds"] == 42
    assert data["stop_reason"] is None and len(data["results"]) == 2
    assert all(result["run"]["budget"]["limits"] == expected for result in data["results"])


@pytest.mark.parametrize("failure", [RuntimeError("模拟失败"), KeyboardInterrupt("模拟中断")])
def test_run_seconds_restores_the_previous_override_on_failure(failure):
    from avelis.analysis.tools import Budget

    defaults = Budget.__init__.__defaults__
    with e2.install_run_seconds(410):
        outer_defaults = Budget.__init__.__defaults__
        with pytest.raises(type(failure), match=str(failure)):
            with e2.install_run_seconds(42):
                assert Budget().seconds == 42
                raise failure
        assert Budget.__init__.__defaults__ is outer_defaults
        assert Budget().seconds == 410
    assert Budget.__init__.__defaults__ is defaults


def test_plan_run_seconds_updates_bounds_and_restores_budget(monkeypatch, capsys):
    from avelis.analysis import agent
    from avelis.analysis.tools import Budget

    forbid_run_imports(monkeypatch)
    defaults = Budget.__init__.__defaults__
    original_timeout = agent.MODEL_TIMEOUT_SECONDS
    baseline = e2.budget_bounds(32)
    monkeypatch.setattr(sys, "argv", ["e2.py", "--plan", "--conditions", "B0",
                                      "--run-seconds", "42", "--model-timeout", "17.5",
                                      "--output-token-cap", "90000"])
    assert e2.main() == 0
    data = json.loads(capsys.readouterr().out)
    assert data["bounds"] == {**baseline, "seconds_wall": 32 * 42, "output_tokens": 32 * 90_000}
    assert agent.MODEL_TIMEOUT_SECONDS == original_timeout
    assert Budget.__init__.__defaults__ is defaults
    assert e2.budget_bounds(32) == baseline


@pytest.mark.parametrize(("parameter", "value", "field", "run"), [
    ("--effort", "max", "reasoning_effort", {**COSTED_RUN, "reasoning_effort": "low"}),
    ("--effort", "max", "reasoning_effort", COSTED_RUN),
    ("--output-token-cap", "90000", "output_tokens",
     {**COSTED_RUN, "budget": {"limits": {"output_tokens": 30_716}}}),
    ("--output-token-cap", "90000", "output_tokens", COSTED_RUN),
    ("--output-token-cap", "90000", "output_tokens", {**COSTED_RUN, "budget": {}}),
    ("--output-token-cap", "90000", "output_tokens", {**COSTED_RUN, "budget": {"limits": {}}}),
    ("--run-seconds", "42", "seconds", {**COSTED_RUN, "budget": {"limits": {"seconds": 300}}}),
    ("--run-seconds", "42", "seconds", COSTED_RUN),
    ("--run-seconds", "42", "seconds", {**COSTED_RUN, "budget": {}}),
    ("--run-seconds", "42", "seconds", {**COSTED_RUN, "budget": {"limits": {}}}),
])
def test_run_parameter_mismatch_is_saved_and_stops_without_retry(monkeypatch, fake_runtime, parameter, value, field, run):
    from avelis.analysis.tools import Budget

    state = fake_runtime
    state.run = run
    defaults = Budget.__init__.__defaults__
    with pytest.raises(RuntimeError, match=field):
        run_fake(monkeypatch, state, parameter, value)
    assert len(state.asks) == len(state.scores) == 1
    assert [len(snapshot["results"]) for snapshot in state.saves] == [1, 1]
    data = state.saves[-1]
    assert field in data["stop_reason"] and "不匹配" in data["stop_reason"]
    assert data["results"][0]["run"] == run
    assert data["results"][0]["score"] == {"passed": True}
    assert Decimal(data["ledger"]["spent_cny"]) == Decimal("0.6")
    assert state.assignment.reasoning_effort is None
    assert Budget.__init__.__defaults__ is defaults


def test_tag_selects_output_directory_and_is_recorded(monkeypatch, fake_runtime, tmp_path):
    state = fake_runtime
    assert run_fake(monkeypatch, state, "--tag", "e2-max-输出") == 0
    assert state.save_paths
    assert all(path.parent == tmp_path / "experiments" / "e2-max-输出" for path in state.save_paths)
    data = state.saves[-1]
    assert data["meta"]["tag"] == "e2-max-输出"
    started = datetime.fromisoformat(data["meta"]["started"])
    assert state.save_paths[-1].name == f"{started.strftime('%Y%m%d-%H%M%S')}.json"
    assert json.loads(state.save_paths[-1].read_text(encoding="utf-8")) == data
