"""E2 运行器的离线单测：隔离配置文件、模型、数据库和演示状态目录。"""

from __future__ import annotations

import builtins
import hashlib
import importlib.util
import json
import os
import sys
from collections import Counter
from dataclasses import replace
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
        if name in {"demo", "eval_analysis", "tests.eval.scoring_v2_1_1"} or (
            name == "tests.eval" and "scoring_v2_1_1" in (fromlist or ())
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
    import tests.eval as eval_package

    truth = {"unit": "明确传入的测试真值"}
    truth_path = tmp_path / "truth.json"
    truth_path.write_text(json.dumps(truth), encoding="utf-8")
    scorer_path = tmp_path / "scorer.py"
    scorer_path.write_text("# 测试评分器\n", encoding="utf-8")
    questions = [{"id": scenario, "user": "unit-user", "question": scenario,
                  "window": {"start": "2026-10-01", "end": "2026-10-31"}} for scenario in e2.SCENARIOS]
    state = SimpleNamespace(run=COSTED_RUN, asks=[], scores=[], clients=[], logins=[], truths=[], saves=[],
                            truth_path=truth_path, scorer_path=scorer_path, questions=questions)

    def forbidden(*args, **kwargs):
        pytest.fail("E2 不应读取演示环境或 E1 注入材料")

    def load_questions(value):
        state.truths.append(value)
        return {"questions": questions}

    def client(env):
        state.clients.append(True)
        return object()

    def login(client, user, password):
        state.logins.append((user, password))
        return {"Authorization": "unit-token"}

    def ask(client, headers, question):
        state.asks.append((question, settings_flags()))
        if isinstance(state.run, Exception):
            raise state.run
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
    scorer = SimpleNamespace(VERSION="unit-v2.1.1", __file__=str(scorer_path),
                             load_questions=load_questions, score=score)
    monkeypatch.setattr(eval_package, "scoring_v2_1_1", scorer, raising=False)
    monkeypatch.setitem(sys.modules, "tests.eval.scoring_v2_1_1", scorer)
    monkeypatch.setattr(sys.modules["e1"], "load_material", forbidden)
    write_text = Path.write_text

    def capture_save(path, text, *args, **kwargs):
        if path.parent == tmp_path / "experiments" / "e2":
            state.saves.append(json.loads(text))
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
    assert meta["scorer"] == "unit-v2.1.1"
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
