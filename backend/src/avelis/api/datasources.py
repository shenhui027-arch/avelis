"""数据源、凭据、端点别名、采集。口令只写不读：接口永不回传口令或其密文。"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from avelis import audit, jobs
from avelis.api import schemas as S
from avelis.api.deps import current_user, get_session
from avelis.cache import LockBusy
from avelis.collect.service import CollectionFailed
from avelis.engines import engine_of, engines, get_engine
from avelis.engines.base import QUERY, UnsupportedEngine
from avelis.governance import access
from avelis.models import CollectionRun, Datasource, DatasourceCredential, EndpointAlias, Job, User
from avelis.security import encrypt_secret

router = APIRouter(prefix="/api", tags=["数据源与采集"])


class Credential(BaseModel):
    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=512, repr=False)


class DatasourceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    kind: str = Field(description="引擎类型，取值见 GET /api/engines")
    host: str = Field(min_length=1, max_length=255)
    port: int = Field(ge=1, le=65535)
    options: dict = Field(default_factory=dict)
    collect_interval_minutes: int = Field(default=15, ge=1, le=1440)
    collect: Credential | None = None
    query: Credential | None = None

    @field_validator("kind")
    @classmethod
    def _known_engine(cls, kind: str) -> str:
        try:
            get_engine(kind)
        except UnsupportedEngine as exc:
            raise ValueError(str(exc)) from exc
        return kind


def datasource_view(session: Session, ds: Datasource) -> dict:
    creds = session.scalars(select(DatasourceCredential).where(DatasourceCredential.datasource_id == ds.id))
    last = session.scalar(select(CollectionRun).where(CollectionRun.datasource_id == ds.id)
                          .order_by(CollectionRun.started_at.desc()).limit(1))
    return {
        "id": ds.id, "name": ds.name, "kind": ds.kind, "host": ds.host, "port": ds.port, "options": ds.options,
        "is_test_adapter": ds.is_test_adapter,
        "source_label": "测试适配器" if ds.is_test_adapter else "真实数据库",
        "status": ds.status, "server_identity": ds.server_identity,
        "collect_interval_minutes": ds.collect_interval_minutes,
        "credentials": [{"purpose": c.purpose, "username": c.username, "password": "已设置（不回传）",
                         "updated_at": c.updated_at.isoformat()} for c in creds],
        "last_run": run_view(last) if last else None,
        "engine": engine_of(ds).describe(),
        "aliases": [{"id": a.id, "host": a.host, "port": a.port, "note": a.note} for a in session.scalars(
            select(EndpointAlias).where(EndpointAlias.datasource_id == ds.id))],
    }


def run_view(run: CollectionRun) -> dict:
    return {"id": run.id, "status": run.status, "trigger": run.trigger, "started_at": run.started_at.isoformat(),
            "finished_at": run.finished_at.isoformat() if run.finished_at else None, "stats": run.stats,
            "error": run.error, "snapshot_hash": run.snapshot_hash}


def _set_credential(session: Session, ds: Datasource, purpose: str, cred: Credential) -> None:
    row = session.scalar(select(DatasourceCredential).where(DatasourceCredential.datasource_id == ds.id,
                                                            DatasourceCredential.purpose == purpose))
    if row is None:
        row = DatasourceCredential(datasource_id=ds.id, purpose=purpose, username=cred.username, secret_enc="")
        session.add(row)
    row.username = cred.username
    row.secret_enc = encrypt_secret(cred.password)


@router.get("/datasources", response_model=list[S.DatasourceOut])
def list_datasources(user: User = Depends(current_user), session: Session = Depends(get_session)) -> list[dict]:
    return [datasource_view(session, ds) for ds in session.scalars(select(Datasource).order_by(Datasource.name))]


@router.get("/engines", response_model=list[S.EngineOut])
def list_engines(user: User = Depends(current_user)) -> list[dict]:
    """已登记（真实环境联调过）的引擎及其能力。前端按能力决定表单项，不按类型写死。"""
    return [e.describe() for e in engines() if not e.is_test_adapter]


@router.post("/datasources", status_code=201, response_model=S.DatasourceOut)
def create_datasource(body: DatasourceCreate, user: User = Depends(current_user),
                      session: Session = Depends(get_session)) -> dict:
    access.require_role(session, user, "steward")
    if session.scalar(select(Datasource.id).where(Datasource.name == body.name)):
        raise HTTPException(409, f"数据源 {body.name} 已存在")
    engine = get_engine(body.kind)
    if body.query is not None and not engine.supports(QUERY):
        raise HTTPException(422, f"{engine.label} 适配器不支持问数执行，不需要只读查询账号")
    missing = [o.label for o in engine.options if o.required and not (body.options or {}).get(o.key)]
    if missing:
        raise HTTPException(422, f"{engine.label} 数据源缺少配置：{'、'.join(missing)}")
    ds = Datasource(name=body.name, kind=body.kind, host=body.host, port=body.port, options=body.options,
                    is_test_adapter=engine.is_test_adapter, collect_interval_minutes=body.collect_interval_minutes,
                    created_by=user.id)
    session.add(ds)
    session.flush()
    for purpose in ("collect", "query"):
        cred = getattr(body, purpose)
        if cred is not None:
            _set_credential(session, ds, purpose, cred)
    session.flush()
    audit.record(session, user, "datasource.create", "datasource", ds.id, name=ds.name, kind=ds.kind,
                 host=ds.host, port=ds.port, credentials=[p for p in ("collect", "query") if getattr(body, p)])
    return datasource_view(session, ds)


@router.put("/datasources/{datasource_id}/credentials/{purpose}", response_model=S.DatasourceOut)
def set_credential(datasource_id: str, purpose: Literal["collect", "query"], body: Credential,
                   user: User = Depends(current_user), session: Session = Depends(get_session)) -> dict:
    access.require_role(session, user, "admin")
    ds = session.get(Datasource, datasource_id) or _404("数据源不存在")
    _set_credential(session, ds, purpose, body)
    audit.record(session, user, "datasource.credential", "datasource", ds.id, purpose=purpose, username=body.username)
    return datasource_view(session, ds)


class AliasCreate(BaseModel):
    datasource_id: str
    host: str = Field(min_length=1, max_length=255)
    port: int = Field(ge=1, le=65535)
    note: str | None = None


@router.post("/endpoint-aliases", status_code=201, response_model=S.AliasOut)
def create_alias(body: AliasCreate, user: User = Depends(current_user),
                 session: Session = Depends(get_session)) -> dict:
    access.require_role(session, user, "steward")
    if session.get(Datasource, body.datasource_id) is None:
        _404("数据源不存在")
    if session.scalar(select(EndpointAlias.id).where(EndpointAlias.host == body.host, EndpointAlias.port == body.port)):
        raise HTTPException(409, f"端点 {body.host}:{body.port} 已登记")
    alias = EndpointAlias(datasource_id=body.datasource_id, host=body.host, port=body.port, note=body.note,
                          created_by=user.id)
    session.add(alias)
    session.flush()
    audit.record(session, user, "endpoint_alias.create", "datasource", body.datasource_id, host=body.host,
                 port=body.port)
    return {"id": alias.id, "datasource_id": alias.datasource_id, "host": alias.host, "port": alias.port}


@router.post("/datasources/{datasource_id}/collect", response_model=S.CollectOut)
def collect(datasource_id: str, wait: bool = False, user: User = Depends(current_user),
            session: Session = Depends(get_session)) -> dict:
    access.require_role(session, user, "steward")
    if session.get(Datasource, datasource_id) is None:
        _404("数据源不存在")
    session.commit()
    if not wait:
        job_id = jobs.enqueue("collect", {"datasource_id": datasource_id, "trigger": "manual"}, user.id)
        return {"job_id": job_id, "status": "queued"}
    try:
        run_id = jobs.collect_now(datasource_id, trigger="manual", actor_id=user.id)
    except LockBusy as exc:
        raise HTTPException(409, "该数据源正在采集中") from exc
    except CollectionFailed as exc:
        raise HTTPException(502, f"采集失败：{exc}") from exc
    session.expire_all()
    return run_view(session.get(CollectionRun, run_id))


@router.get("/datasources/{datasource_id}/runs", response_model=list[S.RunOut])
def list_runs(datasource_id: str, user: User = Depends(current_user), session: Session = Depends(get_session)) -> list:
    return [run_view(r) for r in session.scalars(select(CollectionRun).where(
        CollectionRun.datasource_id == datasource_id).order_by(CollectionRun.started_at.desc()).limit(50))]


@router.get("/jobs/{job_id}", response_model=S.JobOut)
def get_job(job_id: str, user: User = Depends(current_user), session: Session = Depends(get_session)) -> dict:
    job = session.get(Job, job_id) or _404("任务不存在")
    return {"id": job.id, "kind": job.kind, "status": job.status, "payload": job.payload, "result": job.result,
            "error": job.error, "created_at": job.created_at.isoformat(),
            "finished_at": job.finished_at.isoformat() if job.finished_at else None}


def _404(message: str):
    raise HTTPException(404, message)
