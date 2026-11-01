"""导入血缘接口：上传文件、粘贴 SQL、补参数、处理候选、手工补边。"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from avelis.api import schemas as S
from avelis.api.deps import current_user, get_session
from avelis.config import get_settings
from avelis.i18n import Msg
from avelis.importer import service
from avelis.models import CandidateEdge, ImportJob, User

router = APIRouter(prefix="/api", tags=["导入血缘"])


class ImportBody(BaseModel):
    datasource_id: str
    filename: str = Field(default="粘贴的 SQL.sql", max_length=255)
    content: str = Field(min_length=1)
    source_type: Literal["sql", "python", "shell"] | None = None
    default_db: str | None = Field(default=None, max_length=128)
    params: dict[str, str] = Field(default_factory=dict)


class ParamsBody(BaseModel):
    params: dict[str, str]


class DecisionBody(BaseModel):
    decision: Literal["accept", "reject", "modify"]
    reason: str = Field(min_length=1)
    source_ref: str | None = None
    source_column: str | None = None
    target_ref: str | None = None
    target_column: str | None = None
    relation: str | None = None


class ManualEdge(BaseModel):
    source_ref: str
    source_column: str | None = None
    target_ref: str
    target_column: str | None = None
    relation: str
    reason: str = Field(min_length=1)
    lines: list[int] | None = None


def _created(session: Session, user: User, **kwargs) -> dict:
    job, created = service.create_import(session, user, **kwargs)
    return {**service.job_view(session, job), "duplicate": not created}


@router.post("/imports", response_model=S.ImportJobOut)
def create_import(body: ImportBody, user: User = Depends(current_user), session: Session = Depends(get_session)):
    max_bytes = get_settings().import_max_bytes
    if len(body.content) > max_bytes:
        # 保留原 Field(max_length=...) 的 422 响应格式与按字符计数的语义。
        raise HTTPException(422, [{"type": "string_too_long", "loc": ["body", "content"],
                                   "msg": f"String should have at most {max_bytes} characters",
                                   "input": body.content, "ctx": {"max_length": max_bytes}}])
    return _created(session, user, **body.model_dump())


@router.post("/imports/upload", response_model=S.ImportJobOut)
async def upload(datasource_id: str = Form(...), file: UploadFile = File(...), default_db: str | None = Form(None),
                 user: User = Depends(current_user), session: Session = Depends(get_session)) -> dict:
    max_bytes = get_settings().import_max_bytes
    raw = await file.read(max_bytes + 1)
    if len(raw) > max_bytes:
        raise HTTPException(413, Msg("文件超过 {size:g} MB", size=max_bytes / 1024**2))
    try:
        content = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HTTPException(422, "文件不是 UTF-8 文本") from exc
    return _created(session, user, datasource_id=datasource_id, filename=file.filename or "upload.sql",
                    content=content, source_type=None, default_db=default_db, params={})


@router.get("/imports", response_model=list[S.ImportJobOut])
def list_imports(user: User = Depends(current_user), session: Session = Depends(get_session)) -> list[dict]:
    service.require_import_access(session, user)
    return [{"id": j.id, "filename": j.filename, "language": j.language, "status": j.status, "stats": j.stats,
             "created_at": j.created_at.isoformat()}
            for j in session.scalars(select(ImportJob).order_by(ImportJob.created_at.desc()).limit(100))]


@router.get("/imports/{job_id}", response_model=S.ImportJobOut)
def get_import(job_id: str, user: User = Depends(current_user), session: Session = Depends(get_session)) -> dict:
    service.require_import_access(session, user)
    return service.job_view(session, service.job_or_404(session, job_id))


@router.put("/imports/{job_id}/params", response_model=S.ImportJobOut)
def set_params(job_id: str, body: ParamsBody, user: User = Depends(current_user),
               session: Session = Depends(get_session)) -> dict:
    service.require_import_access(session, user)
    job = service.set_params(session, user, service.job_or_404(session, job_id), body.params)
    return service.job_view(session, job)


@router.post("/candidates/{candidate_id}/decision", response_model=S.ImportJobOut)
def decide(candidate_id: str, body: DecisionBody, user: User = Depends(current_user),
           session: Session = Depends(get_session)) -> dict:
    service.require_import_access(session, user)
    candidate = session.get(CandidateEdge, candidate_id)
    if candidate is None:
        raise HTTPException(404, "候选不存在")
    changes = body.model_dump(exclude={"decision", "reason"}, exclude_none=True)
    service.decide(session, user, candidate, body.decision, body.reason, changes)
    return service.job_view(session, session.get(ImportJob, candidate.job_id))


@router.post("/imports/{job_id}/edges", status_code=201, response_model=S.ImportJobOut)
def manual_edge(job_id: str, body: ManualEdge, user: User = Depends(current_user),
                session: Session = Depends(get_session)) -> dict:
    service.require_import_access(session, user)
    job = service.job_or_404(session, job_id)
    service.add_manual_edge(session, user, job, body.model_dump(exclude={"reason"}), body.reason)
    return service.job_view(session, job)
