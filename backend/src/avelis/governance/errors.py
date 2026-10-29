"""治理层错误：由 API 层映射为 HTTP 状态码。"""

from __future__ import annotations

from typing import Any


class GovernanceError(Exception):
    status_code = 400

    def __init__(self, message: str, **detail: Any):
        super().__init__(message)
        self.message = message
        self.detail = detail


class NotFound(GovernanceError):
    status_code = 404


class Forbidden(GovernanceError):
    status_code = 403


class Conflict(GovernanceError):
    """状态不允许、依赖已变化、检查未通过等。"""

    status_code = 409


class Invalid(GovernanceError):
    status_code = 422
