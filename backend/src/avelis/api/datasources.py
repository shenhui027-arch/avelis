"""数据源、凭据、端点别名、采集。口令只写不读：接口永不回传口令或其密文。"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from avelis import audit, jobs
from avelis.api import schemas as S
from avelis.api.deps import current_user, get_session
from avelis.cache import LockBusy
from avelis.collect.service import CollectionFailed
from avelis.config import get_settings
from avelis.connectors.tls import parse_tls_options
from avelis.engines import CredentialMissing, collect_connector, engine_of, engines, get_engine
from avelis.engines.base import QUERY, UnsupportedEngine
from avelis.governance import access
from avelis.i18n import Msg, gettext, translate
from avelis.models import (
    Asset,
    ChangeSet,
    CollectionRun,
    Datasource,
    DatasourceCredential,
    EndpointAlias,
    ImportBatch,
    ImportJob,
    Job,
    User,
    VerifiedQuery,
)
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


class DatasourceUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    host: str | None = Field(default=None, min_length=1, max_length=255)
    port: int | None = Field(default=None, ge=1, le=65535)
    options: dict | None = None
    collect_interval_minutes: int | None = Field(default=None, ge=1, le=1440)


def _validate_engine_options(engine, options: dict | None) -> None:
    opts = options or {}
    # 配置项名是适配器的固定标签，连同列表分隔符按请求语言取译文；可选值是协议取值，原样列出
    missing = [gettext(o.label) for o in engine.options if o.required and not opts.get(o.key)]
    if missing:
        raise HTTPException(
            422, Msg("{engine} 数据源缺少配置：{options}", engine=engine.label, options=gettext("、").join(missing))
        )
    invalid = [
        gettext("{label}（可选 {choices}）", label=gettext(o.label), choices=gettext("、").join(o.choices))
        for o in engine.options
        if o.choices and opts.get(o.key) not in (None, "", *o.choices)
    ]
    if invalid:
        raise HTTPException(
            422,
            Msg("{engine} 数据源配置取值不合法：{options}", engine=engine.label, options=gettext("；").join(invalid)),
        )
    if not engine.is_test_adapter:
        try:
            parse_tls_options(opts, check_exists=False)
        except ValueError as exc:
            raise HTTPException(
                422,
                Msg("{engine} 数据源 TLS 配置无效：{error}", engine=engine.label, error=str(exc)),
            ) from None


def datasource_view(session: Session, ds: Datasource) -> dict:
    creds = session.scalars(select(DatasourceCredential).where(DatasourceCredential.datasource_id == ds.id))
    last = session.scalar(
        select(CollectionRun)
        .where(CollectionRun.datasource_id == ds.id)
        .order_by(CollectionRun.started_at.desc())
        .limit(1)
    )
    return {
        "id": ds.id,
        "name": ds.name,
        "kind": ds.kind,
        "host": ds.host,
        "port": ds.port,
        "options": ds.options,
        "is_test_adapter": ds.is_test_adapter,
        "source_label": gettext("测试适配器" if ds.is_test_adapter else "真实数据库"),
        "status": ds.status,
        "server_identity": ds.server_identity,
        "collect_interval_minutes": ds.collect_interval_minutes,
        "allow_exploration": ds.allow_exploration,
        "exploration_supported": _exploration_supported(ds),
        "credentials": [
            {
                "purpose": c.purpose,
                "username": c.username,
                "password": "已设置（不回传）",
                "updated_at": c.updated_at.isoformat(),
            }
            for c in creds
        ],
        "last_run": run_view(last) if last else None,
        "engine": engine_of(ds).describe(),
        "aliases": [
            {"id": a.id, "host": a.host, "port": a.port, "note": a.note}
            for a in session.scalars(select(EndpointAlias).where(EndpointAlias.datasource_id == ds.id))
        ],
    }


def _exploration_supported(ds: Datasource) -> bool:
    """引擎有探索档案（第一版：StarRocks、PostgreSQL）且具备问数执行能力。"""
    from avelis.analysis.explore import profile_for

    try:
        return profile_for(ds.kind) is not None and engine_of(ds).supports(QUERY)
    except Exception:  # noqa: BLE001 - 未知引擎
        return False


class Exploration(BaseModel):
    allow: bool


def run_view(run: CollectionRun) -> dict:
    return {
        "id": run.id,
        "status": run.status,
        "trigger": run.trigger,
        "started_at": run.started_at.isoformat(),
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        "stats": run.stats,
        "error": run.error,
        "snapshot_hash": run.snapshot_hash,
    }


def _set_credential(session: Session, ds: Datasource, purpose: str, cred: Credential) -> None:
    row = session.scalar(
        select(DatasourceCredential).where(
            DatasourceCredential.datasource_id == ds.id, DatasourceCredential.purpose == purpose
        )
    )
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
def create_datasource(
    body: DatasourceCreate, user: User = Depends(current_user), session: Session = Depends(get_session)
) -> dict:
    access.require_role(session, user, "steward")
    if session.scalar(select(Datasource.id).where(Datasource.name == body.name)):
        raise HTTPException(409, Msg("数据源 {name} 已存在", name=body.name))
    engine = get_engine(body.kind)
    if body.query is not None and not engine.supports(QUERY):
        raise HTTPException(422, Msg("{engine} 适配器不支持问数执行，不需要只读查询账号", engine=engine.label))
    _validate_engine_options(engine, body.options)
    interval = (
        body.collect_interval_minutes
        if "collect_interval_minutes" in body.model_fields_set
        else get_settings().collect_interval_minutes
    )
    ds = Datasource(
        name=body.name,
        kind=body.kind,
        host=body.host,
        port=body.port,
        options=body.options,
        is_test_adapter=engine.is_test_adapter,
        collect_interval_minutes=interval,
        created_by=user.id,
    )
    session.add(ds)
    session.flush()
    for purpose in ("collect", "query"):
        cred = getattr(body, purpose)
        if cred is not None:
            _set_credential(session, ds, purpose, cred)
    session.flush()
    audit.record(
        session,
        user,
        "datasource.create",
        "datasource",
        ds.id,
        name=ds.name,
        kind=ds.kind,
        host=ds.host,
        port=ds.port,
        credentials=[p for p in ("collect", "query") if getattr(body, p)],
    )
    return datasource_view(session, ds)


@router.patch("/datasources/{datasource_id}", response_model=S.DatasourceOut)
def update_datasource(
    datasource_id: str,
    body: DatasourceUpdate,
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
) -> dict:
    access.require_role(session, user, "steward")
    ds = session.get(Datasource, datasource_id) or _404("数据源不存在")
    changes = body.model_dump(exclude_unset=True)
    if "name" in changes and changes["name"] is not None:
        dup = session.scalar(select(Datasource.id).where(Datasource.name == changes["name"], Datasource.id != ds.id))
        if dup:
            raise HTTPException(409, Msg("数据源 {name} 已存在", name=changes["name"]))
    if "options" in changes and changes["options"] is not None:
        _validate_engine_options(engine_of(ds), changes["options"])
    for key in ("name", "host", "port", "options", "collect_interval_minutes"):
        if key in changes and changes[key] is not None:
            setattr(ds, key, changes[key])
    session.flush()
    audit.record(
        session,
        user,
        "datasource.update",
        "datasource",
        ds.id,
        fields=sorted(k for k, v in changes.items() if v is not None),
    )
    return datasource_view(session, ds)


@router.delete("/datasources/{datasource_id}", status_code=204)
def delete_datasource(
    datasource_id: str, user: User = Depends(current_user), session: Session = Depends(get_session)
) -> None:
    access.require_role(session, user, "steward")
    ds = session.get(Datasource, datasource_id) or _404("数据源不存在")
    has_history = any(
        session.scalar(select(model.id).where(model.datasource_id == ds.id).limit(1)) is not None
        for model in (Asset, CollectionRun, ChangeSet, ImportBatch, ImportJob)
    )
    if not has_history:
        active_collect = any(
            (job.payload or {}).get("datasource_id") == ds.id
            for job in session.scalars(select(Job).where(Job.kind == "collect", Job.status.in_(("queued", "running"))))
        )
        has_history = active_collect
    if has_history:
        raise HTTPException(409, "该数据源已有采集或血缘记录，不能删除")
    session.execute(delete(DatasourceCredential).where(DatasourceCredential.datasource_id == ds.id))
    session.execute(delete(EndpointAlias).where(EndpointAlias.datasource_id == ds.id))
    session.execute(update(VerifiedQuery).where(VerifiedQuery.datasource_id == ds.id).values(datasource_id=None))
    audit.record(session, user, "datasource.delete", "datasource", ds.id, name=ds.name, kind=ds.kind)
    session.delete(ds)
    session.flush()


@router.post("/datasources/{datasource_id}/test", response_model=S.TestConnectionOut)
def test_datasource_connection(
    datasource_id: str, user: User = Depends(current_user), session: Session = Depends(get_session)
) -> dict:
    access.require_role(session, user, "steward")
    ds = session.get(Datasource, datasource_id) or _404("数据源不存在")
    started = time.monotonic()
    try:
        collector = collect_connector(session, ds)
    except (CredentialMissing, UnsupportedEngine) as exc:
        return {"ok": False, "latency_ms": 0, "server_identity": None, "error": translate(str(exc))}
    pool = ThreadPoolExecutor(max_workers=1)
    future = pool.submit(collector.server_identity)
    try:
        identity = future.result(timeout=8)
        latency_ms = int((time.monotonic() - started) * 1000)
        if isinstance(identity, dict) and identity:
            ds.server_identity = identity
        audit.record(session, user, "datasource.test", "datasource", ds.id, ok=True, latency_ms=latency_ms)
        return {"ok": True, "latency_ms": latency_ms, "server_identity": identity, "error": None}
    except FuturesTimeoutError:
        future.cancel()
        latency_ms = int((time.monotonic() - started) * 1000)
        audit.record(session, user, "datasource.test", "datasource", ds.id, ok=False, latency_ms=latency_ms)
        return {"ok": False, "latency_ms": latency_ms, "server_identity": None, "error": translate("连接超时（8秒）")}
    except Exception as exc:  # noqa: BLE001
        latency_ms = int((time.monotonic() - started) * 1000)
        audit.record(session, user, "datasource.test", "datasource", ds.id, ok=False, latency_ms=latency_ms)
        return {"ok": False, "latency_ms": latency_ms, "server_identity": None, "error": str(exc)[:300]}
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


@router.put("/datasources/{datasource_id}/exploration", response_model=S.DatasourceOut)
def set_exploration(
    datasource_id: str, body: Exploration, user: User = Depends(current_user), session: Session = Depends(get_session)
) -> dict:
    """按数据源开关探索查询（ADR 0009 第 6 点）：默认开启，管理员可以关闭。"""
    access.require_role(session, user, "admin")
    ds = session.get(Datasource, datasource_id)
    if ds is None:
        raise HTTPException(404, "数据源不存在")
    ds.allow_exploration = body.allow
    session.flush()
    audit.record(session, user, "datasource.exploration", "datasource", ds.id, allow=body.allow)
    return datasource_view(session, ds)


@router.put("/datasources/{datasource_id}/credentials/{purpose}", response_model=S.DatasourceOut)
def set_credential(
    datasource_id: str,
    purpose: Literal["collect", "query"],
    body: Credential,
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
) -> dict:
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
def create_alias(
    body: AliasCreate, user: User = Depends(current_user), session: Session = Depends(get_session)
) -> dict:
    access.require_role(session, user, "steward")
    if session.get(Datasource, body.datasource_id) is None:
        _404("数据源不存在")
    if session.scalar(select(EndpointAlias.id).where(EndpointAlias.host == body.host, EndpointAlias.port == body.port)):
        raise HTTPException(409, Msg("端点 {host}:{port} 已登记", host=body.host, port=body.port))
    alias = EndpointAlias(
        datasource_id=body.datasource_id, host=body.host, port=body.port, note=body.note, created_by=user.id
    )
    session.add(alias)
    session.flush()
    audit.record(
        session, user, "endpoint_alias.create", "datasource", body.datasource_id, host=body.host, port=body.port
    )
    return {"id": alias.id, "datasource_id": alias.datasource_id, "host": alias.host, "port": alias.port}


@router.post("/datasources/{datasource_id}/collect", response_model=S.CollectOut)
def collect(
    datasource_id: str, wait: bool = False, user: User = Depends(current_user), session: Session = Depends(get_session)
) -> dict:
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
        raise HTTPException(502, Msg("采集失败：{error}", error=exc)) from exc
    session.expire_all()
    return run_view(session.get(CollectionRun, run_id))


@router.post("/datasources/{datasource_id}/scope", response_model=S.ScopeAddOut)
def add_scope(
    datasource_id: str, body: S.ScopeAddIn, user: User = Depends(current_user), session: Session = Depends(get_session)
) -> dict:
    """把导入血缘里未匹配的读取来源所在的库 / 模式加入采集范围，有新增时立即排队采集。"""
    # TODO(codex): 权限同编辑数据源（access.require_role(session, user, "steward")）；数据源不存在 404；
    #   调 importer.scope.add_to_scope(session, user, ds, engine_of(ds), body.names)，ValueError → 422（Msg 文案）；
    #   有新增时先 session.commit() 再 jobs.enqueue("collect", {"datasource_id": ..., "trigger": "manual"}, user.id)；
    #   返回 {"added": [...], "job_id": <任务 ID 或 None>}。
    raise NotImplementedError


@router.get("/datasources/{datasource_id}/runs", response_model=list[S.RunOut])
def list_runs(datasource_id: str, user: User = Depends(current_user), session: Session = Depends(get_session)) -> list:
    return [
        run_view(r)
        for r in session.scalars(
            select(CollectionRun)
            .where(CollectionRun.datasource_id == datasource_id)
            .order_by(CollectionRun.started_at.desc())
            .limit(50)
        )
    ]


@router.get("/jobs/{job_id}", response_model=S.JobOut)
def get_job(job_id: str, user: User = Depends(current_user), session: Session = Depends(get_session)) -> dict:
    job = session.get(Job, job_id) or _404("任务不存在")
    if not (access.is_admin(session, user) or (job.created_by is not None and job.created_by == user.id)):
        raise HTTPException(403, "无权访问该任务")
    return {
        "id": job.id,
        "kind": job.kind,
        "status": job.status,
        "payload": job.payload,
        "result": job.result,
        "error": job.error,
        "created_at": job.created_at.isoformat(),
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
    }


def _404(message: str):
    raise HTTPException(404, message)
