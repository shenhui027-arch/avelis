"""统一 SSE（text/event-stream）帧编码与流式响应头。"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator
from typing import Any

from fastapi.responses import StreamingResponse

SSE_HEADERS = {
    "Cache-Control": "no-cache, no-transform",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}

KEEPALIVE_FRAME = ": keepalive\n\n"


def format_sse(event: str, data: Any, *, event_id: str | int | None = None) -> str:
    payload = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False, default=str)
    lines: list[str] = []
    if event_id is not None:
        lines.append(f"id: {event_id}")
    if event:
        lines.append(f"event: {event}")
    for line in payload.splitlines() or [""]:
        lines.append(f"data: {line}")
    return "\n".join(lines) + "\n\n"


def sse_response(generator: AsyncIterator[str] | Iterator[str]) -> StreamingResponse:
    return StreamingResponse(
        generator,
        media_type="text/event-stream; charset=utf-8",
        headers=dict(SSE_HEADERS),
    )
