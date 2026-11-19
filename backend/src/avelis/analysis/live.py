"""运行中的实时过程（思考中的推理文字、正在执行的工具）。

步骤记录（AnalysisStep）只在一次模型调用或工具调用完成后写入；调用进行中，前端靠这里看到“正在做什么”：
模型流式返回的推理文字（DeepSeek 思考模式的 reasoning_content）与正文，以及正在执行的工具名与参数。

存在 Redis（与任务唤醒同一实例），键带过期时间，不是事实来源：丢失或 Redis 不可用只影响实时显示，
完整的推理文字仍随 model 步骤写入步骤记录。写入有节流；Redis 出错一次后本次运行不再尝试，不拖慢分析。
after_seq 是写入时已提交的最后一个步骤序号：前端只在还没有更新的步骤时显示这条实时状态，避免与已完成的步骤重复。
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from avelis.db import utcnow

log = logging.getLogger(__name__)

TTL_SECONDS = 600
FLUSH_SECONDS = 0.5
MAX_CHARS = 32_000  # 实时显示只保留末尾这么多字；完整文字在步骤记录里
BACKOFF_SECONDS = 30  # Redis 出错后本进程暂停写入实时过程的时长
_unavailable_until = 0.0


def _key(run_id: str) -> str:
    return f"avelis:analysis:live:{run_id}"


def _client():
    from avelis import cache

    return cache.client()


class LiveSink:
    """一次运行的实时状态写入器；由执行者（worker）在持有执行权时使用。"""

    def __init__(self, run_id: str, client: Any = None):
        self.run_id = run_id
        self._client = client
        self.disabled = False
        self._state: dict[str, Any] | None = None
        self._flushed = 0.0

    def _redis(self):
        if self._client is None:
            self._client = _client()
        return self._client

    def _write(self, state: dict[str, Any] | None) -> None:
        global _unavailable_until
        if self.disabled or time.monotonic() < _unavailable_until:
            return
        try:
            if state is None:
                self._redis().delete(_key(self.run_id))
            else:
                self._redis().set(_key(self.run_id), json.dumps(state, ensure_ascii=False, default=str), ex=TTL_SECONDS)
        except Exception as exc:  # noqa: BLE001 - 实时显示可有可无，不能影响分析
            self.disabled = True
            _unavailable_until = time.monotonic() + BACKOFF_SECONDS
            log.warning("分析 %s 的实时过程写入失败，本次运行不再写入：%s", self.run_id, type(exc).__name__)
        self._flushed = time.monotonic()

    def model_started(self, after_seq: int) -> None:
        self._state = {
            "phase": "model",
            "after_seq": after_seq,
            "started_at": utcnow().isoformat(),
            "thinking": "",
            "text": "",
        }
        self._write(self._state)

    def model_delta(self, thinking: str, text: str, *, force: bool = False) -> None:
        """累计到目前为止的推理文字与正文；按节流间隔写入。"""
        if self._state is None or self._state.get("phase") != "model":
            return
        self._state["thinking"] = thinking[-MAX_CHARS:]
        self._state["text"] = text[-MAX_CHARS:]
        if force or time.monotonic() - self._flushed >= FLUSH_SECONDS:
            self._write(self._state)

    def tool_started(self, after_seq: int, name: str, args: Any) -> None:
        self._state = {
            "phase": "tool",
            "after_seq": after_seq,
            "started_at": utcnow().isoformat(),
            "tool": name,
            "args": args if isinstance(args, dict) else {},
        }
        self._write(self._state)

    def clear(self) -> None:
        self._state = None
        self._write(None)


def read(run_id: str) -> dict[str, Any] | None:
    try:
        raw = _client().get(_key(run_id))
    except Exception as exc:  # noqa: BLE001
        log.warning("读取分析 %s 的实时过程失败：%s", run_id, type(exc).__name__)
        return None
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def clear(run_id: str) -> None:
    LiveSink(run_id).clear()
