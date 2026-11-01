"""批量导入血缘：后台解析、分页复核与来源版本留底。"""

from __future__ import annotations

import json
from pathlib import PurePosixPath
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Response, UploadFile
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import case, func, or_, select
from sqlalchemy.orm import Session

from avelis import audit, jobs
from avelis.api import schemas as S
from avelis.api.deps import current_user, get_session
from avelis.config import get_settings
from avelis.db import utcnow
from avelis.i18n import Msg, gettext
from avelis.importer import batch, service
from avelis.importer.archive import ImportFile, read_uploads
from avelis.models import (
    AppSetting,
    CandidateEdge,
    Datasource,
    ExtractedStatement,
    ImportBatch,
    ImportJob,
    ImportSource,
    LineageEdge,
    User,
)

router = APIRouter(prefix="/api", tags=["导入血缘"])
STATUSES = {"parsed", "partial", "needs_params", "failed", "unsupported", "skipped", "pending_review"}


class ModelAssist(BaseModel):
    failed_sql: bool = False
    code: bool = False


class ImportBody(BaseModel):
    datasource_id: str
    filename: str = Field(default="粘贴的 SQL.sql", min_length=1, max_length=255)
    content: str = Field(min_length=1)
    source_type: Literal["sql", "python", "shell"] | None = None
    default_db: str | None = Field(default=None, max_length=128)
    params: dict[str, str] = Field(default_factory=dict)
    model_assist: ModelAssist = Field(default_factory=ModelAssist)
    sql_functions: list[str] = Field(default_factory=list)

    @field_validator("sql_functions")
    @classmethod
    def valid_functions(cls, names: list[str]) -> list[str]:
        return SqlFunctions(sql_functions=names).sql_functions


class ParamsBody(BaseModel):
    params: dict[str, str]


class DecisionBody(BaseModel):
    decision: Literal["accept", "reject", "modify"]
    reason: str = Field(min_length=1, pattern=r"\S")
    source_ref: str | None = None
    source_column: str | None = None
    target_ref: str | None = None
    target_column: str | None = None
    relation: str | None = None


class DecisionsBody(BaseModel):
    ids: list[str] = Field(min_length=1)
    decision: Literal["accept", "reject"]
    reason: str = Field(min_length=1, pattern=r"\S")


class ManualEdge(BaseModel):
    file_id: str
    source_ref: str
    source_column: str | None = None
    target_ref: str
    target_column: str | None = None
    relation: str
    reason: str = Field(min_length=1, pattern=r"\S")
    lines: list[int] | None = None


class SqlFunctions(BaseModel):
    sql_functions: list[str]

    @field_validator("sql_functions")
    @classmethod
    def valid_names(cls, names: list[str]) -> list[str]:
        result = []
        for name in names:
            name = name.strip()
            if not name or len(name) > 128 or not all(part.isidentifier() for part in name.split(".")):
                raise ValueError(gettext("执行函数名必须是有效的标识符或点分名称，且不超过 128 个字符"))
            if name not in result:
                result.append(name)
        return result


class SqlFunction(BaseModel):
    sql_function: str

    @field_validator("sql_function")
    @classmethod
    def valid_name(cls, name: str) -> str:
        return SqlFunctions(sql_functions=[name]).sql_functions[0]


def _functions_key(datasource_id: str) -> str:
    return f"import.sql_functions:{datasource_id}"


def _sql_functions(session: Session, datasource_id: str, names: list[str]) -> list[str]:
    names = SqlFunctions(sql_functions=names).sql_functions
    setting = session.get(AppSetting, _functions_key(datasource_id))
    saved = (setting.value or {}).get("sql_functions", []) if setting else []
    return list(dict.fromkeys([*saved, *names]))


def _run(session: Session, imported: ImportBatch, *, wait: bool, created: bool = True) -> dict:
    job_id = imported.job_id
    session.commit()
    if job_id:
        if wait:
            jobs.run_and_wait(job_id, get_settings().worker_running_timeout_seconds)
        elif created:
            jobs._wake(job_id)
    session.expire_all()
    return {**batch.batch_view(session, imported), "duplicate": not created}


def _created(
    session: Session,
    user: User,
    *,
    wait: bool = False,
    files: list[ImportFile] | None = None,
    notes: list[str] | None = None,
    **kwargs,
) -> dict:
    service.require_import_access(session, user)
    if files is None:
        filename, content = kwargs.pop("filename"), kwargs.pop("content")
        language = service.detect_language(filename, kwargs.pop("source_type", None))
        size = len(content.encode("utf-8"))
        if size > get_settings().import_max_bytes:
            raise HTTPException(413, Msg("文件超过 {size:g} MB", size=get_settings().import_max_bytes / 1024**2))
        files = [ImportFile(filename, content, language, filename, size)]
    kwargs["sql_functions"] = _sql_functions(session, kwargs["datasource_id"], kwargs.get("sql_functions", []))
    imported, created = batch.create_batch(session, user, files=files, notes=notes or [], **kwargs)
    return _run(session, imported, wait=wait, created=created)


@router.post("/imports", status_code=202, response_model=S.ImportBatchOut)
def create_import(
    body: ImportBody,
    response: Response,
    wait: bool = False,
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
):
    max_bytes = get_settings().import_max_bytes
    if len(body.content) > max_bytes:
        raise HTTPException(
            422,
            [
                {
                    "type": "string_too_long",
                    "loc": ["body", "content"],
                    "msg": f"String should have at most {max_bytes} characters",
                    "input": body.content,
                    "ctx": {"max_length": max_bytes},
                }
            ],
        )
    response.status_code = 200 if wait else 202
    return _created(session, user, wait=wait, **body.model_dump())


def _form_json(text: str | None, model, default):
    if not isinstance(text, str) or not text:
        return default
    try:
        value = json.loads(text)
        return model.model_validate(value).model_dump() if model else value
    except (ValueError, TypeError) as exc:
        raise HTTPException(422, "导入选项不是有效的 JSON") from exc


def _form_functions(text: str | None) -> list[str]:
    try:
        return SqlFunctions(sql_functions=_form_json(text, None, [])).sql_functions
    except (ValueError, TypeError) as exc:
        raise HTTPException(422, "执行函数名列表不合法") from exc


@router.post("/imports/upload", status_code=202, response_model=S.ImportBatchOut)
async def upload(
    datasource_id: str = Form(...),
    file: UploadFile | None = File(None),
    default_db: str | None = Form(None),
    files: list[UploadFile] | None = File(None),
    bracket_files: Annotated[list[UploadFile] | None, File(alias="files[]")] = None,
    model_assist: str | None = Form(None),
    sql_functions: str | None = Form(None),
    wait: bool = False,
    response: Response = None,
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
) -> dict:
    settings = get_settings()
    uploaded = [
        *(files if isinstance(files, list) else []),
        *(bracket_files if isinstance(bracket_files, list) else []),
    ]
    if isinstance(file, UploadFile) or (file is not None and hasattr(file, "read")):
        uploaded.append(file)
    if not uploaded:
        raise HTTPException(422, "至少上传一个文件")
    max_batch_bytes = getattr(settings, "import_max_batch_bytes", settings.import_max_bytes)
    raw_files, size = [], 0
    for item in uploaded:
        limit = max_batch_bytes if (item.filename or "").lower().endswith(".zip") else settings.import_max_bytes
        raw = await item.read(limit + 1)
        if len(raw) > limit:
            raise HTTPException(413, Msg("文件超过 {size:g} MB", size=limit / 1024**2))
        size += len(raw)
        if size > max_batch_bytes:
            raise HTTPException(413, Msg("批次超过 {size:g} MB", size=max_batch_bytes / 1024**2))
        raw_files.append((item.filename or "upload.sql", raw))
    if response is not None:
        response.status_code = 200 if wait else 202
    # 单文件仍走统一创建函数，保留直接调用时的 UTF-8 错误。
    if len(raw_files) == 1 and PurePosixPath(raw_files[0][0]).suffix.lower() in service.LANGUAGES:
        filename, raw = raw_files[0]
        try:
            content = raw.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise HTTPException(422, "文件不是 UTF-8 文本") from exc
        return _created(
            session,
            user,
            datasource_id=datasource_id,
            filename=filename,
            content=content,
            source_type=None,
            default_db=default_db,
            params={},
            wait=wait,
            model_assist=_form_json(model_assist, ModelAssist, {"failed_sql": False, "code": False}),
            sql_functions=_form_functions(sql_functions),
        )
    expanded, notes = read_uploads(raw_files, max_file_bytes=settings.import_max_bytes, max_batch_bytes=max_batch_bytes)
    return _created(
        session,
        user,
        datasource_id=datasource_id,
        files=expanded,
        notes=notes,
        default_db=default_db,
        params={},
        wait=wait,
        model_assist=_form_json(model_assist, ModelAssist, {"failed_sql": False, "code": False}),
        sql_functions=_form_functions(sql_functions),
    )


@router.get("/imports", response_model=list[S.ImportBatchBrief])
def list_imports(user: User = Depends(current_user), session: Session = Depends(get_session)) -> list[dict]:
    service.require_import_access(session, user)
    imported = list(session.scalars(select(ImportBatch).order_by(ImportBatch.created_at.desc()).limit(100)))
    if not imported:
        return []
    for item in imported:
        batch.reconcile_batch(session, item)
    duplicate_ids = {file_id for item in imported for file_id in (item.options or {}).get("duplicate_file_ids", [])}
    files = list(
        session.scalars(
            select(ImportJob)
            .where(or_(ImportJob.batch_id.in_([b.id for b in imported]), ImportJob.id.in_(duplicate_ids)))
            .order_by(ImportJob.created_at, ImportJob.id)
        )
    )
    grouped: dict[str, list[ImportJob]] = {}
    for item in files:
        grouped.setdefault(item.batch_id, []).append(item)
    by_id = {item.id: item for item in files}
    for item in imported:
        grouped.setdefault(item.id, []).extend(
            by_id[file_id] for file_id in (item.options or {}).get("duplicate_file_ids", []) if file_id in by_id
        )
        grouped[item.id] = _ordered_files(item, grouped[item.id])
    return [
        {
            "id": b.id,
            "status": b.status,
            "file_count": len(grouped.get(b.id, [])),
            "statement_count": sum((b.stats or {}).get("statements", {}).values()),
            "progress": b.progress or {},
            "created_at": b.created_at.isoformat(),
            "filename": grouped[b.id][0].filename if grouped.get(b.id) else None,
        }
        for b in imported
    ]


@router.get("/imports/sql-functions/{datasource_id}", response_model=S.ImportSqlFunctionsOut)
def get_sql_functions(
    datasource_id: str, user: User = Depends(current_user), session: Session = Depends(get_session)
) -> dict:
    service.require_import_access(session, user)
    if session.get(Datasource, datasource_id) is None:
        raise HTTPException(404, "数据源不存在")
    return {"datasource_id": datasource_id, "sql_functions": _sql_functions(session, datasource_id, [])}


def _save_sql_functions(
    session: Session, user: User, datasource_id: str, names: list[str], *, append: bool = False
) -> dict:
    # 数据源行始终存在；设置尚未创建时也能串行化追加与全量替换。
    if session.scalar(select(Datasource.id).where(Datasource.id == datasource_id).with_for_update()) is None:
        raise HTTPException(404, "数据源不存在")
    key = _functions_key(datasource_id)
    setting = session.get(AppSetting, key, populate_existing=True)
    saved = (setting.value or {}).get("sql_functions", []) if setting else []
    if append:
        names = list(dict.fromkeys([*saved, *names]))
        if setting is not None and names == saved:
            return {"datasource_id": datasource_id, "sql_functions": saved}
    if setting is None:
        setting = AppSetting(key=key)
        session.add(setting)
    setting.value = {"sql_functions": names}
    setting.updated_by, setting.updated_at = user.id, utcnow()
    audit.record(session, user, "import.sql_functions", "datasource", datasource_id, sql_functions=names)
    session.flush()
    return {"datasource_id": datasource_id, "sql_functions": names}


@router.put("/imports/sql-functions/{datasource_id}", response_model=S.ImportSqlFunctionsOut)
def put_sql_functions(
    datasource_id: str, body: SqlFunctions, user: User = Depends(current_user), session: Session = Depends(get_session)
) -> dict:
    service.require_import_access(session, user)
    return _save_sql_functions(session, user, datasource_id, body.sql_functions)


@router.post("/imports/sql-functions/{datasource_id}", response_model=S.ImportSqlFunctionsOut)
def post_sql_function(
    datasource_id: str, body: SqlFunction, user: User = Depends(current_user), session: Session = Depends(get_session)
) -> dict:
    service.require_import_access(session, user)
    return _save_sql_functions(session, user, datasource_id, [body.sql_function], append=True)


@router.get("/imports/{batch_id}", response_model=S.ImportBatchOut)
def get_import(batch_id: str, user: User = Depends(current_user), session: Session = Depends(get_session)) -> dict:
    service.require_import_access(session, user)
    return batch.batch_view(session, batch.batch_or_404(session, batch_id))


def _file(session: Session, batch_id: str, file_id: str) -> ImportJob:
    item = service.job_or_404(session, file_id)
    imported = batch.batch_or_404(session, batch_id)
    if (item.stats or {}).get("duplicate_of") or (
        item.batch_id != batch_id and file_id not in (imported.options or {}).get("duplicate_file_ids", [])
    ):
        raise HTTPException(404, "文件不属于此导入批次")
    return item


def _file_ids(session: Session, batch_id: str):
    imported = batch.batch_or_404(session, batch_id)
    duplicates = (imported.options or {}).get("duplicate_file_ids", [])
    return select(ImportJob.id).where(or_(ImportJob.batch_id == batch_id, ImportJob.id.in_(duplicates)))


def _ordered_files(imported: ImportBatch, files: list[ImportJob]) -> list[ImportJob]:
    order = _file_positions(imported)
    visible = {item.id: item for item in files if not (item.stats or {}).get("duplicate_of")}
    return sorted(
        visible.values(),
        key=lambda item: (
            order.get(item.id, (item.stats or {}).get("upload_order", len(files))),
            item.created_at,
            item.id,
        ),
    )


def _file_positions(imported: ImportBatch) -> dict[str, int]:
    order = {}
    for index, file_id in enumerate((imported.options or {}).get("file_order", [])):
        order.setdefault(file_id, index)
    return order


def _file_order(session: Session, batch_id: str) -> tuple:
    imported = batch.batch_or_404(session, batch_id)
    order = _file_positions(imported)
    ordinal = (
        case(order, value=ImportJob.id, else_=len(order))
        if order
        else ImportJob.stats["upload_order"].as_integer().nulls_last()
    )
    return ordinal, ImportJob.created_at, ImportJob.id


def _statement_view(item: ExtractedStatement) -> dict:
    return {
        "id": item.id,
        "file_id": item.job_id,
        "seq": item.seq,
        "lines": [item.line_start, item.line_end],
        "method": item.method,
        "statement_type": item.statement_type,
        "target": item.target_ref,
        "writes": item.writes or [],
        "status": item.status,
        "reason": item.reason,
        "placeholders": item.placeholders or [],
        "raw_text": item.raw_text,
        "sql_text": item.sql_text,
        "text_hash": item.text_hash,
    }


@router.get("/imports/{batch_id}/statements", response_model=S.ImportStatementPage)
def statements(
    batch_id: str,
    file_id: str | None = None,
    status: str | None = None,
    q: str | None = None,
    offset: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
) -> dict:
    service.require_import_access(session, user)
    batch.batch_or_404(session, batch_id)
    if file_id:
        _file(session, batch_id, file_id)
    if status and status not in STATUSES:
        raise HTTPException(422, "语句状态不合法")
    conditions = [
        ExtractedStatement.job_id == file_id if file_id else ExtractedStatement.job_id.in_(_file_ids(session, batch_id))
    ]
    if q:
        conditions.append(
            or_(
                ExtractedStatement.raw_text.icontains(q, autoescape=True),
                ExtractedStatement.sql_text.icontains(q, autoescape=True),
            )
        )
    pending = (
        select(CandidateEdge.id)
        .where(CandidateEdge.statement_id == ExtractedStatement.id, CandidateEdge.status == "pending")
        .exists()
    )
    counts = dict(
        session.execute(
            select(ExtractedStatement.status, func.count()).where(*conditions).group_by(ExtractedStatement.status)
        ).all()
    )
    counts["pending_review"] = (
        session.scalar(select(func.count()).select_from(ExtractedStatement).where(*conditions, pending)) or 0
    )
    filtered = [*conditions]
    if status:
        filtered.append(pending if status == "pending_review" else ExtractedStatement.status == status)
    total = session.scalar(select(func.count()).select_from(ExtractedStatement).where(*filtered)) or 0
    items = session.scalars(
        select(ExtractedStatement)
        .join(ImportJob, ImportJob.id == ExtractedStatement.job_id)
        .where(*filtered)
        .order_by(*_file_order(session, batch_id), ExtractedStatement.seq)
        .offset(offset)
        .limit(limit)
    )
    return {
        "items": [_statement_view(item) for item in items],
        "total": total,
        "counts": counts,
        "offset": offset,
        "limit": limit,
    }


def _edge_view(edge: LineageEdge) -> dict:
    return {
        "id": edge.id,
        "source_ref": edge.source_ref,
        "source_column": edge.source_column,
        "target_ref": edge.target_ref,
        "target_column": edge.target_column,
        "relation": edge.relation,
    }


def _candidate_views(session: Session, items: list[CandidateEdge]) -> list[dict]:
    edge_ids = [c.lineage_edge_id for c in items if c.lineage_edge_id]
    edges = (
        {e.id: e for e in session.scalars(select(LineageEdge).where(LineageEdge.id.in_(edge_ids)))} if edge_ids else {}
    )
    hop_ids = {
        hop
        for attributes in [
            *(edge.attributes or {} for edge in edges.values()),
            *(item.evidence or {} for item in items),
        ]
        for hop in attributes.get("hops", [])
        if isinstance(hop, str)
    }
    hops = {e.id: e for e in session.scalars(select(LineageEdge).where(LineageEdge.id.in_(hop_ids)))} if hop_ids else {}
    result = []
    for c in items:
        edge = edges.get(c.lineage_edge_id)
        attributes = {**(c.evidence or {}), **(edge.attributes or {} if edge else {})}
        hop_views = [
            _edge_view(hops[h]) if h in hops else {"id": h} for h in attributes.get("hops", []) if isinstance(h, str)
        ]
        result.append(
            {
                "id": c.id,
                "file_id": c.job_id,
                "statement_id": c.statement_id,
                "origin": c.origin,
                "status": c.status,
                "source": f"{c.source_ref}.{c.source_column}" if c.source_column else c.source_ref,
                "target": f"{c.target_ref}.{c.target_column}" if c.target_column else c.target_ref,
                "source_ref": c.source_ref,
                "source_column": c.source_column,
                "target_ref": c.target_ref,
                "target_column": c.target_column,
                "relation": c.relation,
                "expression": c.expression,
                "confidence": c.confidence,
                "evidence": c.evidence or {},
                "lineage_edge_id": c.lineage_edge_id,
                "modified": c.modified or {},
                "reason": c.reason,
                "decided_by": c.decided_by,
                "attributes": attributes,
                "via": attributes.get("via", []),
                "hops": hop_views,
            }
        )
    return result


@router.get("/imports/{batch_id}/candidates", response_model=S.ImportCandidatePage)
def candidates(
    batch_id: str,
    file_id: str | None = None,
    status: str | None = None,
    origin: str | None = None,
    statement_id: str | None = None,
    offset: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
) -> dict:
    service.require_import_access(session, user)
    batch.batch_or_404(session, batch_id)
    if file_id:
        _file(session, batch_id, file_id)
    conditions = [
        CandidateEdge.job_id == file_id if file_id else CandidateEdge.job_id.in_(_file_ids(session, batch_id))
    ]
    if status:
        conditions.append(CandidateEdge.status == status)
    if origin:
        conditions.append(CandidateEdge.origin == origin)
    if statement_id:
        conditions.append(CandidateEdge.statement_id == statement_id)
    total = session.scalar(select(func.count()).select_from(CandidateEdge).where(*conditions)) or 0
    items = list(
        session.scalars(
            select(CandidateEdge)
            .join(ImportJob, ImportJob.id == CandidateEdge.job_id)
            .where(*conditions)
            .order_by(*_file_order(session, batch_id), CandidateEdge.created_at, CandidateEdge.id)
            .offset(offset)
            .limit(limit)
        )
    )
    return {"items": _candidate_views(session, items), "total": total, "offset": offset, "limit": limit}


@router.get("/imports/{batch_id}/files/{job_id}/content", response_model=S.ImportContentOut)
def content(
    batch_id: str, job_id: str, user: User = Depends(current_user), session: Session = Depends(get_session)
) -> dict:
    service.require_import_access(session, user)
    batch.batch_or_404(session, batch_id)
    item = _file(session, batch_id, job_id)
    return {
        "file_id": item.id,
        "filename": item.filename,
        "content": session.scalar(select(ImportSource.content).where(ImportSource.job_id == item.id)) or "",
    }


@router.get("/imports/{batch_id}/model-payload", response_model=list[dict])
def model_payload(
    batch_id: str, user: User = Depends(current_user), session: Session = Depends(get_session)
) -> list[dict]:
    service.require_import_access(session, user)
    imported = batch.batch_or_404(session, batch_id)
    files = _ordered_files(
        imported, list(session.scalars(select(ImportJob).where(ImportJob.id.in_(_file_ids(session, batch_id)))))
    )
    return [
        {**payload, "file_id": item.id, "filename": item.filename}
        for item in files
        for payload in (item.model_payload or [])
    ]


@router.put("/imports/{batch_id}/params", status_code=202, response_model=S.ImportBatchOut)
def set_params(
    batch_id: str,
    body: ParamsBody,
    response: Response,
    wait: bool = False,
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
) -> dict:
    service.require_import_access(session, user)
    imported = batch.set_params(session, user, batch.batch_or_404(session, batch_id), body.params)
    response.status_code = 200 if wait else 202
    return _run(session, imported, wait=wait)


def _decision_problem(session: Session, candidate: CandidateEdge, decision: str) -> str | None:
    if candidate.status == "intermediate":
        return "中间段只用于查证，请处理折叠后的边"
    item = session.get(ImportJob, candidate.job_id, populate_existing=True)
    if item is None or item.status == "superseded":
        return "文件版本已被替换"
    imported = session.get(ImportBatch, item.batch_id, populate_existing=True)
    if imported is not None and imported.status in ("queued", "running"):
        return "批次仍在处理"
    if candidate.status not in ("pending", "written"):
        return f"候选当前状态为 {candidate.status}，不能再处理"
    if candidate.status == "written":
        edge = session.get(LineageEdge, candidate.lineage_edge_id) if candidate.lineage_edge_id else None
        if edge is not None and edge.status == "intermediate":
            return "中间段只用于查证，请处理折叠后的边"
        if edge is None or edge.status != "active":
            return "候选对应的血缘边已失效，不能再处理"
    if candidate.status == "written" and decision == "accept":
        return "解析边已写入，无需接受"
    if decision == "accept" and candidate.relation not in service.RELATIONS:
        return "候选的关系类型不合法"
    return None


def _lock_datasources(session: Session, datasource_ids: set[str]) -> None:
    if datasource_ids:
        # 与整批替换使用同一把锁，拿锁后才重新检查文件版本。
        session.execute(
            select(Datasource.id).where(Datasource.id.in_(datasource_ids)).order_by(Datasource.id).with_for_update()
        ).all()


def _lock_candidate_sources(session: Session, candidates: list[CandidateEdge]) -> None:
    source_ids = set()
    for job_id in {item.job_id for item in candidates}:
        item = session.get(ImportJob, job_id)
        if item is not None:
            source_ids.add(item.datasource_id)
    _lock_datasources(session, source_ids)


@router.post("/candidates/decisions", response_model=S.ImportDecisionsOut)
def decide_many(
    body: DecisionsBody, user: User = Depends(current_user), session: Session = Depends(get_session)
) -> dict:
    service.require_import_access(session, user)
    ids = list(dict.fromkeys(body.ids))
    initial = list(session.scalars(select(CandidateEdge).where(CandidateEdge.id.in_(ids))))
    _lock_candidate_sources(session, initial)
    found = {
        c.id: c
        for c in session.scalars(
            select(CandidateEdge)
            .where(CandidateEdge.id.in_(ids))
            .order_by(CandidateEdge.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    }
    problems = [
        {
            "id": candidate_id,
            "reason": "候选不存在"
            if candidate_id not in found
            else _decision_problem(session, found[candidate_id], body.decision),
        }
        for candidate_id in ids
    ]
    problems = [item for item in problems if item["reason"]]
    if problems:
        raise HTTPException(409, {"message": "部分候选不能处理，整批未作修改", "candidates": problems})
    for candidate_id in ids:
        service.decide(session, user, found[candidate_id], body.decision, body.reason, {})
    return {"ids": ids, "decision": body.decision, "processed": len(ids)}


@router.post("/candidates/{candidate_id}/decision", response_model=S.ImportBatchOut)
def decide(
    candidate_id: str, body: DecisionBody, user: User = Depends(current_user), session: Session = Depends(get_session)
) -> dict:
    service.require_import_access(session, user)
    initial = session.get(CandidateEdge, candidate_id)
    if initial is None:
        raise HTTPException(404, "候选不存在")
    _lock_candidate_sources(session, [initial])
    candidate = session.scalar(
        select(CandidateEdge)
        .where(CandidateEdge.id == candidate_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if candidate is None:
        raise HTTPException(404, "候选不存在")
    problem = _decision_problem(session, candidate, body.decision)
    if problem:
        raise HTTPException(409, problem)
    changes = body.model_dump(exclude={"decision", "reason"}, exclude_none=True)
    service.decide(session, user, candidate, body.decision, body.reason, changes)
    item = session.get(ImportJob, candidate.job_id)
    return batch.batch_view(session, batch.batch_or_404(session, item.batch_id))


@router.post("/imports/{batch_id}/edges", status_code=201, response_model=S.ImportBatchOut)
def manual_edge(
    batch_id: str, body: ManualEdge, user: User = Depends(current_user), session: Session = Depends(get_session)
) -> dict:
    service.require_import_access(session, user)
    imported = batch.batch_or_404(session, batch_id)
    item = _file(session, batch_id, body.file_id)
    _lock_datasources(session, {item.datasource_id})
    item = session.get(ImportJob, item.id, populate_existing=True)
    imported = session.get(ImportBatch, imported.id, populate_existing=True)
    if imported.status in ("queued", "running") or item.status == "superseded":
        raise HTTPException(409, "批次正在处理或文件版本已被替换，不能补边")
    service.add_manual_edge(session, user, item, body.model_dump(exclude={"file_id", "reason"}), body.reason)
    return batch.batch_view(session, imported)
