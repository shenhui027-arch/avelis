"""技能市场的登录接口。"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse
from fastapi.routing import APIRoute
from sqlalchemy.orm import Session
from starlette.datastructures import UploadFile
from starlette.exceptions import HTTPException as StarletteHTTPException

from avelis.api import schemas as S
from avelis.api.deps import current_user, get_session
from avelis.config import get_settings
from avelis.governance.errors import Invalid
from avelis.i18n import Msg
from avelis.models import User
from avelis.skills import service


class _SkillRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def handle(request: Request):
            try:
                return await handler(request)
            except RequestValidationError as exc:
                raise Invalid(Msg("技能请求参数无效")) from exc
            except StarletteHTTPException as exc:
                if exc.status_code != 400:
                    raise
                raise Invalid(Msg("技能请求参数无效")) from exc

        return handle


router = APIRouter(
    prefix="/api/skills",
    tags=["技能"],
    route_class=_SkillRoute,
    responses={
        422: {
            "description": "技能请求或技能包无效",
            "content": {
                "application/json": {
                    "schema": {
                        "type": "object",
                        "required": ["detail"],
                        "properties": {"detail": {"type": "string"}},
                    }
                }
            },
        }
    },
)


_MULTIPART_OVERHEAD = 64 * 1024


def _multipart_schema(**properties) -> dict:
    return {
        "requestBody": {
            "required": True,
            "content": {
                "multipart/form-data": {
                    "schema": {
                        "type": "object",
                        "required": ["file"],
                        "properties": {"file": {"type": "string", "format": "binary"}, **properties},
                    }
                }
            },
        }
    }


async def _read_upload(request: Request) -> tuple[bytes, str, str | None]:
    limit = get_settings().skills_max_zip_bytes
    body_limit = limit + _MULTIPART_OVERHEAD
    message = Msg("技能包不能超过 {limit} 字节", limit=limit)
    try:
        length = int(request.headers.get("content-length", ""))
    except ValueError:
        raise HTTPException(413, message) from None
    if not 0 <= length <= body_limit:
        raise HTTPException(413, message)

    remaining = body_limit

    async def receive():
        nonlocal remaining
        event = await request.receive()
        remaining -= len(event.get("body", b""))
        if remaining < 0:
            raise HTTPException(413, message)
        return event

    # Starlette 的 max_part_size 只限制文本字段；实际请求流也要受总量限制。
    bounded = Request(request.scope, receive=receive)
    async with bounded.form(max_files=1, max_fields=4, max_part_size=_MULTIPART_OVERHEAD) as form:
        file = form.get("file")
        mode = form.get("mode", "new")
        note = form.get("note")
        if (
            not isinstance(file, UploadFile)
            or not isinstance(mode, str)
            or (note is not None and not isinstance(note, str))
        ):
            raise Invalid(Msg("技能请求参数无效"))
        data = await file.read(limit + 1)
        if len(data) > limit:
            raise HTTPException(413, message)
        return data, mode, note


def _commit_upload(session: Session, user: User, data: bytes, mode: str, note: str | None) -> dict:
    response = service.upload(session, user, data, mode, note)
    try:
        session.commit()
    except Exception:
        service.discard_failed_upload(session, response["name"], response["version"])
        raise
    return response


@router.get("/summary", response_model=S.SkillSummaryOut)
def summary(user: User = Depends(current_user), session: Session = Depends(get_session)) -> dict:
    return service.summary(session, user)


@router.get("", response_model=list[S.SkillOut])
def list_skills(
    view: Literal["market", "enabled", "mine", "review"] = "market",
    q: str = "",
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
) -> list[dict]:
    return service.list_skills(session, user, view, q)


@router.get("/{skill_id}", response_model=S.SkillDetailOut)
def detail(skill_id: str, user: User = Depends(current_user), session: Session = Depends(get_session)) -> dict:
    return service.detail(session, user, skill_id)


@router.get("/{skill_id}/versions/{version}/files", response_model=S.SkillFileContentOut)
def file_content(
    skill_id: str,
    version: int,
    path: str,
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
) -> dict:
    return service.file_content(session, user, skill_id, version, path)


@router.get(
    "/{skill_id}/versions/{version}/download",
    response_class=FileResponse,
    responses={200: {"content": {"application/zip": {"schema": {"type": "string", "format": "binary"}}}}},
)
def download(
    skill_id: str,
    version: int,
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
) -> FileResponse:
    path, filename = service.download_path(session, user, skill_id, version)
    return FileResponse(path, media_type="application/zip", filename=filename)


@router.post("/validate", response_model=S.SkillValidationOut, openapi_extra=_multipart_schema())
async def validate(
    request: Request, user: User = Depends(current_user), session: Session = Depends(get_session)
) -> dict:
    data, _, _ = await _read_upload(request)
    return await run_in_threadpool(service.validate, session, user, data)


@router.post(
    "/upload",
    status_code=201,
    response_model=S.SkillVersionOut,
    openapi_extra=_multipart_schema(
        mode={"type": "string", "enum": ["new", "overwrite"], "default": "new"},
        note={"type": "string", "maxLength": 500},
    ),
)
async def upload(request: Request, user: User = Depends(current_user), session: Session = Depends(get_session)) -> dict:
    data, mode, note = await _read_upload(request)
    return await run_in_threadpool(_commit_upload, session, user, data, mode, note)


@router.put("/{skill_id}/enabled", response_model=S.SkillOut)
def set_enabled(
    skill_id: str,
    body: S.SkillEnabledIn,
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
) -> dict:
    response = service.set_enabled(session, user, skill_id, body.enabled)
    session.commit()
    return response


@router.post("/versions/{version_id}/approve", response_model=S.SkillDetailOut)
def approve(version_id: str, user: User = Depends(current_user), session: Session = Depends(get_session)) -> dict:
    return service.approve(session, user, version_id)


@router.post("/versions/{version_id}/reject", response_model=S.SkillVersionOut)
def reject(
    version_id: str,
    body: S.SkillRejectIn,
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
) -> dict:
    response = service.reject(session, user, version_id, body.reason)
    session.commit()
    return response


@router.post("/versions/{version_id}/withdraw", response_model=S.SkillVersionOut)
def withdraw(version_id: str, user: User = Depends(current_user), session: Session = Depends(get_session)) -> dict:
    response = service.withdraw(session, user, version_id)
    session.commit()
    return response


@router.post("/{skill_id}/delist", response_model=S.SkillOut)
def delist(skill_id: str, user: User = Depends(current_user), session: Session = Depends(get_session)) -> dict:
    return service.delist(session, user, skill_id)
