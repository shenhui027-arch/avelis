"""业务域、资产目录、列级血缘、粒度、人工确认。检索按业务域权限过滤（后端执行）。"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import and_, func, or_, select, tuple_
from sqlalchemy.orm import Session, aliased

from avelis import audit
from avelis.api import schemas as S
from avelis.api.deps import current_user, get_session
from avelis.ask.compiler import duplicate_key_sql
from avelis.collect.service import current_fingerprints
from avelis.config import get_settings
from avelis.connectors.base import QueryError
from avelis.db import utcnow
from avelis.engines import CapabilityMissing, dialect_of, engines, query_connector
from avelis.engines.base import QUERY
from avelis.governance import access
from avelis.governance.metrics import current_fingerprint_map
from avelis.i18n import Msg, gettext, translate
from avelis.lineage.grain import effective_grains
from avelis.lineage.sql import PARSER_VERSION
from avelis.models import (
    Asset,
    AssetVersion,
    ColumnVersion,
    Confirmation,
    Datasource,
    Definition,
    Domain,
    DomainMember,
    Grain,
    IdentityLink,
    JoinPath,
    LineageEdge,
    Metric,
    ParseResult,
    User,
)

router = APIRouter(prefix="/api", tags=["目录与血缘"])


@router.get("/catalog/counts", response_model=S.CatalogCountsOut)
def catalog_counts(user: User = Depends(current_user), session: Session = Depends(get_session)) -> dict:
    """与无筛选列表相同的可见数量，在一条 SQL 中聚合，不加载目录对象。"""
    left, right = aliased(Asset), aliased(Asset)
    metrics = (
        select(func.count())
        .select_from(Metric)
        .join(User, User.id == Metric.owner_id)
        .where(access.domain_visibility_clause(session, user, Metric.domain_id))
        .scalar_subquery()
    )
    assets = (
        select(func.count())
        .select_from(Asset)
        .join(Datasource, Datasource.id == Asset.datasource_id)
        .where(Asset.status == "active", access.asset_visibility_clause(session, user))
        .scalar_subquery()
    )
    domains = (
        select(func.count()).select_from(Domain).where(access.domain_visibility_clause(session, user)).scalar_subquery()
    )
    join_paths = (
        select(func.count())
        .select_from(JoinPath)
        .join(left, left.id == JoinPath.left_asset_id)
        .join(right, right.id == JoinPath.right_asset_id)
        .where(
            access.asset_visibility_clause(session, user, left), access.asset_visibility_clause(session, user, right)
        )
        .scalar_subquery()
    )
    return dict(
        session.execute(
            select(
                metrics.label("metrics"),
                assets.label("assets"),
                domains.label("domains"),
                join_paths.label("join_paths"),
            )
        )
        .one()
        ._mapping
    )


# --- 业务域 -----------------------------------------------------------------------


class DomainCreate(BaseModel):
    code: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9_]+$")
    name: str = Field(min_length=1, max_length=128)
    description: str | None = None
    owner_id: str


class ExampleQuestionIn(BaseModel):
    text: str = Field(min_length=1, max_length=200)
    mode: str = Field(default="both", pattern=r"^(ask|analysis|both)$")


class ExampleQuestionsSet(BaseModel):
    questions: list[ExampleQuestionIn] = Field(default_factory=list, max_length=12)


class MemberSet(BaseModel):
    user_id: str
    role: str = Field(pattern=r"^(owner|maintainer|viewer)$")


def _grain_view(grain) -> dict:
    """粒度说明按请求语言输出；只在接口出口翻译，写进发布检查留底的仍是简体原文。"""
    return {**grain.to_dict(), "note": translate(grain.note)}


def domain_view(session: Session, d: Domain, members: list | None = None) -> dict:
    if members is None:
        members = session.execute(
            select(DomainMember.user_id, DomainMember.role, User.username, User.display_name)
            .join(User, User.id == DomainMember.user_id)
            .where(DomainMember.domain_id == d.id)
        )
    return {
        "id": d.id,
        "code": d.code,
        "name": d.name,
        "description": d.description,
        "owner_id": d.owner_id,
        "members": [{"user_id": u, "role": r, "username": n, "display_name": dn} for u, r, n, dn in members],
        "example_questions": [
            q if isinstance(q, dict) else {"text": str(q), "mode": "both"} for q in d.example_questions or []
        ],
    }


@router.get("/domains", response_model=list[S.DomainOut])
def list_domains(
    q: str | None = None,
    domain_id: str | None = None,
    limit: int | None = Query(None, ge=1, le=500),
    offset: int = Query(0, ge=0),
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
) -> list[dict]:
    stmt = select(Domain).where(access.domain_visibility_clause(session, user))
    if domain_id:
        stmt = stmt.where(Domain.id == domain_id)
    if q:
        stmt = stmt.where(
            or_(
                *(
                    func.lower(column).contains(q.lower(), autoescape=True)
                    for column in (Domain.code, Domain.name, Domain.description)
                )
            )
        )
    stmt = stmt.order_by(Domain.code, Domain.id).offset(offset)
    if limit is not None:
        stmt = stmt.limit(limit)
    domains = list(session.scalars(stmt))
    members: dict[str, list] = {d.id: [] for d in domains}
    if domains:
        for domain, *member in session.execute(
            select(DomainMember.domain_id, DomainMember.user_id, DomainMember.role, User.username, User.display_name)
            .join(User, User.id == DomainMember.user_id)
            .where(DomainMember.domain_id.in_(members))
            .order_by(DomainMember.user_id)
        ):
            members[domain].append(member)
    return [domain_view(session, d, members[d.id]) for d in domains]


@router.post("/domains", status_code=201, response_model=S.DomainOut)
def create_domain(body: DomainCreate, user: User = Depends(current_user), session: Session = Depends(get_session)):
    access.require_role(session, user, "admin")
    if session.scalar(select(Domain.id).where(Domain.code == body.code)):
        raise HTTPException(409, Msg("业务域 {code} 已存在", code=body.code))
    if session.get(User, body.owner_id) is None:
        raise HTTPException(422, "负责人不存在")
    d = Domain(code=body.code, name=body.name, description=body.description, owner_id=body.owner_id)
    session.add(d)
    session.flush()
    session.add(DomainMember(domain_id=d.id, user_id=body.owner_id, role="owner"))
    session.flush()
    audit.record(session, user, "domain.create", "domain", d.id, code=d.code, owner_id=body.owner_id)
    return domain_view(session, d)


@router.put("/domains/{domain_id}/members", response_model=S.DomainOut)
def set_member(
    domain_id: str, body: MemberSet, user: User = Depends(current_user), session: Session = Depends(get_session)
) -> dict:
    d = session.get(Domain, domain_id) or _404("业务域不存在")
    access.require_domain_owner(session, user, domain_id)
    if session.get(User, body.user_id) is None:
        raise HTTPException(422, "用户不存在")
    row = session.scalar(
        select(DomainMember).where(DomainMember.domain_id == domain_id, DomainMember.user_id == body.user_id)
    )
    if row is None:
        session.add(DomainMember(domain_id=domain_id, user_id=body.user_id, role=body.role))
    else:
        row.role = body.role
    session.flush()
    audit.record(session, user, "domain.member", "domain", domain_id, member=body.user_id, role=body.role)
    return domain_view(session, d)


@router.put("/domains/{domain_id}/example-questions", response_model=S.DomainOut)
def set_example_questions(
    domain_id: str,
    body: ExampleQuestionsSet,
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
) -> dict:
    """业务域的示例问题（ADR 0010）：由负责人、维护者维护，问数页与分析页从 /example-questions 读取。"""
    d = session.get(Domain, domain_id) or _404("业务域不存在")
    access.require_domain_maintainer(session, user, domain_id)
    d.example_questions = [q.model_dump() for q in body.questions]
    session.flush()
    audit.record(session, user, "domain.example_questions", "domain", domain_id, count=len(body.questions))
    return domain_view(session, d)


@router.get("/example-questions", response_model=S.ExampleQuestionsOut)
def get_example_questions(
    mode: str = Query("ask", pattern=r"^(ask|analysis)$"),
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
) -> dict:
    """当前用户可见业务域配置的示例问题；都没有配置时按模板从可见口径生成（ADR 0010 第 3 点）。"""
    from avelis.ask.examples import example_questions

    return {"questions": example_questions(session, user, mode)}


# --- 资产 -------------------------------------------------------------------------


def qualified(a: Asset) -> str:
    return f"{a.catalog_name}.{a.db_name}.{a.name}"


def asset_summary(session: Session, a: Asset, ds: Datasource | None = None) -> dict:
    ds = ds if ds is not None else session.get(Datasource, a.datasource_id)
    return {
        "id": a.id,
        "qualified": qualified(a),
        "kind": a.kind,
        "status": a.status,
        "domain_id": a.domain_id,
        "datasource": {"id": ds.id, "name": ds.name, "kind": ds.kind, "is_test_adapter": ds.is_test_adapter},
        "physical_id": a.physical_id,
        "current_version_id": a.current_version_id,
        "last_seen_at": a.last_seen_at.isoformat(),
        "deleted_at": a.deleted_at.isoformat() if a.deleted_at else None,
    }


@router.get("/assets", response_model=list[S.AssetOut])
def list_assets(
    datasource_id: str | None = None,
    domain_id: str | None = None,
    q: str | None = None,
    include_deleted: bool = False,
    ids: Annotated[list[str] | None, Query(max_length=200)] = None,
    queryable: bool | None = None,
    limit: int | None = Query(None, ge=1, le=500),
    offset: int = Query(0, ge=0),
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
) -> list[dict]:
    stmt = (
        select(Asset, Datasource)
        .join(Datasource, Datasource.id == Asset.datasource_id)
        .where(access.asset_visibility_clause(session, user))
    )
    if datasource_id:
        stmt = stmt.where(Asset.datasource_id == datasource_id)
    if domain_id:
        stmt = stmt.where(Asset.domain_id == domain_id)
    if ids is not None:
        stmt = stmt.where(Asset.id.in_(ids))
    if queryable is not None:
        query_kinds = [engine.kind for engine in engines() if engine.supports(QUERY)]
        clause = Datasource.kind.in_(query_kinds)
        stmt = stmt.where(clause if queryable else ~clause)
    if not include_deleted:
        stmt = stmt.where(Asset.status == "active")
    if q:
        name = Asset.catalog_name + "." + Asset.db_name + "." + Asset.name
        stmt = stmt.where(func.lower(name).contains(q.lower(), autoescape=True))
    stmt = stmt.order_by(Asset.catalog_name, Asset.db_name, Asset.name, Asset.id).offset(offset)
    if limit is not None:
        stmt = stmt.limit(limit)
    return [asset_summary(session, a, ds) for a, ds in session.execute(stmt)]


def readable_asset(session: Session, user: User, asset_id: str) -> Asset:
    a = session.get(Asset, asset_id)
    if a is None or not access.can_read_asset(session, user, a):
        raise HTTPException(404, "资产不存在或无权访问")
    return a


def _detail_column_docs(
    session: Session, user: User, asset: Asset, columns: list[ColumnVersion]
) -> tuple[dict[str, dict], dict[str, Asset]]:
    """按依赖层批取注释；指纹快照也只需这些资产，避免列数与全目录规模放大查询。"""
    from avelis.governance.column_docs import EXTERNAL

    assets = {asset.id: asset}
    visible = {asset.id}
    own = {(asset.id, c.name): c.comment for c in columns}
    links: dict[str, IdentityLink] = {}
    sources: dict[tuple[str, str], list[LineageEdge]] = {}
    frontier = [asset]
    for depth in range(10):
        versions = {a.current_version_id: a.id for a in frontier if a.current_version_id}
        if depth and versions:
            for c in session.scalars(select(ColumnVersion).where(ColumnVersion.asset_version_id.in_(versions))):
                own[(versions[c.asset_version_id], c.name)] = c.comment
        if depth == 9:
            break
        parents: set[str] = set()
        external = [a.id for a in frontier if a.kind in EXTERNAL]
        if external:
            for link in session.scalars(
                select(IdentityLink).where(
                    IdentityLink.source_asset_id.in_(external), IdentityLink.superseded_at.is_(None)
                )
            ):
                links[link.source_asset_id] = link
                if link.status in ("confirmed", "candidate") and link.target_asset_id:
                    parents.add(link.target_asset_id)
        views = {a.current_version_id: a for a in frontier if a.kind == "view" and a.current_version_id}
        qualified_parents: set[tuple[str, str, str, str]] = set()
        if views:
            for edge in session.scalars(
                select(LineageEdge).where(
                    tuple_(LineageEdge.target_asset_id, LineageEdge.target_asset_version_id).in_(
                        [(a.id, version_id) for version_id, a in views.items()]
                    ),
                    LineageEdge.status == "active",
                    LineageEdge.origin.in_(("parsed", "manual")),
                )
            ):
                if edge.source_asset_id:
                    parents.add(edge.source_asset_id)
                if edge.origin == "parsed" and edge.relation == "value_direct":
                    sources.setdefault((edge.target_asset_id, edge.target_column), []).append(edge)
            # 粒度透传还会沿解析结果中的 base / join 表走，不能只按列级边缩小指纹范围。
            for version_id, details in session.execute(
                select(Definition.asset_version_id, ParseResult.details)
                .join(
                    ParseResult,
                    and_(ParseResult.definition_id == Definition.id, ParseResult.parser_version == PARSER_VERSION),
                )
                .where(Definition.asset_version_id.in_(views))
            ):
                if not (details or {}).get("passthrough"):
                    continue
                refs = [details["passthrough"].get("base")]
                refs.extend(j.get("right_table") for j in details.get("joins", []))
                for ref in refs:
                    if isinstance(ref, str) and len(parts := ref.split(".")) == 3:
                        qualified_parents.add((views[version_id].datasource_id, *parts))
        parents.difference_update(assets)
        if not parents and not qualified_parents:
            break
        clauses = [Asset.id.in_(parents)]
        if qualified_parents:
            clauses.append(
                and_(
                    Asset.status == "active",
                    tuple_(Asset.datasource_id, Asset.catalog_name, Asset.db_name, Asset.name).in_(qualified_parents),
                )
            )
        frontier = []
        for parent, can_see in session.execute(
            select(Asset, access.asset_visibility_clause(session, user)).where(or_(*clauses))
        ):
            if parent.id not in assets:
                assets[parent.id] = parent
                frontier.append(parent)
            if can_see:
                visible.add(parent.id)
        if not frontier:
            break

    def doc(a: Asset, column: str, depth: int = 0) -> dict:
        out = {
            "asset_id": a.id,
            "asset": qualified(a),
            "column": column,
            "comment": None,
            "status": "missing",
            "source": None,
        }
        comment = (own.get((a.id, column)) or "").strip()
        if comment and comment.lower() != column.lower():
            return {**out, "comment": comment, "status": "ok"}
        if depth > 8:
            return out
        target, source_column, status = None, column, "linked"
        if a.kind in EXTERNAL:
            link = links.get(a.id)
            if link and link.status in ("confirmed", "candidate"):
                target = assets.get(link.target_asset_id)
        elif a.kind == "view":
            edges = sources.get((a.id, column), [])
            if len(edges) == 1 and edges[0].source_column:
                target = assets.get(edges[0].source_asset_id)
                source_column = edges[0].source_column
                status = "inherited"
        if target is not None and target.id in visible:
            upstream = doc(target, source_column, depth + 1)
            if upstream["comment"]:
                return {
                    **out,
                    "comment": upstream["comment"],
                    "status": status,
                    "source": f"{upstream['asset']}.{source_column}",
                }
        return out

    return {c.name: doc(asset, c.name) for c in columns}, assets


@router.get("/assets/{asset_id}", response_model=S.AssetDetailOut)
def asset_detail(asset_id: str, user: User = Depends(current_user), session: Session = Depends(get_session)) -> dict:
    a = readable_asset(session, user, asset_id)
    version = session.get(AssetVersion, a.current_version_id) if a.current_version_id else None
    column_versions = (
        list(
            session.scalars(
                select(ColumnVersion)
                .where(ColumnVersion.asset_version_id == version.id)
                .order_by(ColumnVersion.ordinal)
            )
        )
        if version
        else []
    )
    docs, dependencies = _detail_column_docs(session, user, a, column_versions)
    fps = current_fingerprints(session, asset_ids=set(dependencies))
    confirmations: dict[str, list[Confirmation]] = {}
    for confirmation in session.scalars(
        select(Confirmation).where(Confirmation.asset_id == a.id, Confirmation.revoked_at.is_(None))
    ):
        confirmations.setdefault(confirmation.column_name, []).append(confirmation)
    columns = []
    if version:
        for c in column_versions:
            fp = fps.get((a.id, c.name))
            columns.append(
                {
                    "name": c.name,
                    "data_type": c.data_type,
                    "nullable": c.nullable,
                    "comment": c.comment,
                    "is_key": c.is_key,
                    "column_id": c.column_id,
                    "doc": docs[c.name],
                    "fingerprint": fp.fingerprint if fp else None,
                    "fingerprint_status": fp.status if fp else None,
                    "fingerprint_reason": fp.reason if fp else None,
                    "confirmations": [
                        {
                            "id": x.id,
                            "meaning": x.meaning,
                            "confirmed_by": x.confirmed_by,
                            "created_at": x.created_at.isoformat(),
                            "valid": fp is not None and x.fingerprint == fp.fingerprint,
                            "note": None
                            if fp is not None and x.fingerprint == fp.fingerprint
                            else "字段推导已变化，此确认不再生效",
                        }
                        for x in confirmations.get(c.name, [])
                    ],
                }
            )
    definition = (
        session.scalar(select(Definition).where(Definition.asset_version_id == a.current_version_id))
        if version
        else None
    )
    parse = (
        session.scalar(
            select(ParseResult).where(
                ParseResult.definition_id == definition.id, ParseResult.parser_version == PARSER_VERSION
            )
        )
        if definition
        else None
    )
    target = aliased(Asset)
    link = session.scalar(
        select(IdentityLink)
        .outerjoin(target, target.id == IdentityLink.target_asset_id)
        .where(
            IdentityLink.source_asset_id == a.id,
            IdentityLink.superseded_at.is_(None),
            or_(IdentityLink.target_asset_id.is_(None), access.asset_visibility_clause(session, user, target)),
        )
    )
    linked_from = list(
        session.scalars(
            select(IdentityLink)
            .join(Asset, Asset.id == IdentityLink.source_asset_id)
            .where(
                IdentityLink.target_asset_id == a.id,
                IdentityLink.superseded_at.is_(None),
                access.asset_visibility_clause(session, user),
            )
        )
    )
    link_asset_ids = {x.source_asset_id for x in linked_from}
    if link and link.target_asset_id:
        link_asset_ids.add(link.target_asset_id)
    if missing := link_asset_ids - dependencies.keys():
        dependencies.update({x.id: x for x in session.scalars(select(Asset).where(Asset.id.in_(missing)))})
    versions = session.scalars(
        select(AssetVersion).where(AssetVersion.asset_id == a.id).order_by(AssetVersion.version_no.desc())
    )
    return {
        **asset_summary(session, a),
        "version": _version_view(version) if version else None,
        "columns": columns,
        "definition": {
            "raw_sql": definition.raw_sql,
            "canonical_sql": definition.canonical_sql,
            "canonical_hash": definition.canonical_hash,
        }
        if definition
        else None,
        "parse": {
            "status": parse.status,
            "reason": parse.reason,
            "parser_version": parse.parser_version,
            "details": parse.details,
        }
        if parse
        else None,
        "grains": [_grain_view(g) for g in effective_grains(session, a.id, {k: f.fingerprint for k, f in fps.items()})],
        "identity_link": _link_view(session, link, dependencies) if link else None,
        "linked_from": [_link_view(session, x, dependencies) for x in linked_from],
        "versions": [_version_view(v) for v in versions],
    }


def _version_view(v: AssetVersion) -> dict:
    return {
        "id": v.id,
        "version_no": v.version_no,
        "observed_at": v.observed_at.isoformat(),
        "last_seen_at": v.last_seen_at.isoformat(),
        "superseded_at": v.superseded_at.isoformat() if v.superseded_at else None,
        "name": f"{v.catalog_name}.{v.db_name}.{v.name}",
        "kind": v.kind,
        "table_model": v.table_model,
        "key_columns": v.key_columns,
        "unique_keys": v.unique_keys,
        "comment": v.comment,
        "properties": v.properties,
        "content_hash": v.content_hash,
    }


def _link_view(session: Session, link: IdentityLink, assets: dict[str, Asset] | None = None) -> dict:
    source, target = (
        assets.get(link.source_asset_id) if assets is not None else session.get(Asset, link.source_asset_id),
        (assets.get(link.target_asset_id) if assets is not None else session.get(Asset, link.target_asset_id))
        if link.target_asset_id
        else None,
    )
    return {
        "id": link.id,
        "status": link.status,
        "method": link.method,
        "evidence": link.evidence,
        "column_match": link.column_match,
        "source": qualified(source),
        "target": {"id": target.id, "qualified": qualified(target), "datasource_id": target.datasource_id}
        if target
        else None,
        "created_at": link.created_at.isoformat(),
    }


class AssetPatch(BaseModel):
    domain_id: str | None


@router.patch("/assets/{asset_id}", response_model=S.AssetOut)
def patch_asset(
    asset_id: str, body: AssetPatch, user: User = Depends(current_user), session: Session = Depends(get_session)
) -> dict:
    access.require_role(session, user, "steward")
    a = session.get(Asset, asset_id) or _404("资产不存在")
    if body.domain_id:
        access.require_domain_maintainer(session, user, body.domain_id)
    if a.domain_id:
        access.require_domain_maintainer(session, user, a.domain_id)
    before = a.domain_id
    a.domain_id = body.domain_id
    audit.record(session, user, "asset.domain", "asset", a.id, before=before, after=body.domain_id)
    if before != body.domain_id:
        from avelis.governance.joins import on_asset_domain_changed

        on_asset_domain_changed(session, a.id)
    return asset_summary(session, a)


# --- 粒度 -------------------------------------------------------------------------


class GrainDeclare(BaseModel):
    columns: list[str] = Field(min_length=1)
    reason: str = Field(min_length=1)


@router.post("/assets/{asset_id}/grains", status_code=201)
def declare_grain(
    asset_id: str, body: GrainDeclare, user: User = Depends(current_user), session: Session = Depends(get_session)
) -> dict:
    a = readable_asset(session, user, asset_id)
    access.require_role(session, user, "steward")
    if a.domain_id:
        access.require_domain_maintainer(session, user, a.domain_id)
    names = set(
        session.scalars(select(ColumnVersion.name).where(ColumnVersion.asset_version_id == a.current_version_id))
    )
    missing = set(body.columns) - names
    if missing:
        raise HTTPException(422, Msg("列不存在：{columns}", columns=", ".join(sorted(missing))))
    fps = current_fingerprint_map(session)
    g = Grain(
        asset_id=a.id,
        asset_version_id=a.current_version_id,
        columns=body.columns,
        origin="declared",
        status="unverified",
        column_fingerprints={c: fps.get((a.id, c)) for c in body.columns},
        declared_by=user.id,
        reason=body.reason,
    )
    session.add(g)
    session.flush()
    audit.record(session, user, "grain.declare", "asset", a.id, grain_id=g.id, columns=body.columns, reason=body.reason)
    return {"id": g.id, "columns": g.columns, "origin": g.origin, "status": g.status}


@router.get("/metadata/comment-gaps", response_model=S.CommentGapsOut)
def comment_gaps(
    datasource_id: str | None = None, user: User = Depends(current_user), session: Session = Depends(get_session)
) -> dict:
    """注释缺失清单：字段含义以数据库列注释为准。按数据源汇总完整度，列出缺注释的列（含可沿用的上游注释），
    交给数据库负责人补齐。只包含当前用户可见的资产。"""
    from avelis.governance.column_docs import GAPS, asset_docs, summarize

    allowed = access.domain_ids(session, user)
    stmt = select(Asset).where(Asset.status == "active").order_by(Asset.catalog_name, Asset.db_name, Asset.name)
    if datasource_id:
        stmt = stmt.where(Asset.datasource_id == datasource_id)
    by_ds: dict[str, list[dict]] = {}
    items = []
    for asset in session.scalars(stmt):
        if not access.can_read_asset(session, user, asset, allowed):
            continue
        docs = asset_docs(session, asset, can_see=lambda a: access.can_read_asset(session, user, a, allowed))
        by_ds.setdefault(asset.datasource_id, []).extend(docs)
        items += [
            {
                **d,
                "kind": asset.kind,
                "datasource_id": asset.datasource_id,
                "suggestion": d["comment"] if d["status"] == "inherited" else None,
            }
            for d in docs
            if d["status"] in GAPS
        ]
    names = {ds.id: ds.name for ds in session.scalars(select(Datasource))}
    return {
        "datasources": [{"datasource_id": k, "name": names.get(k), **summarize(v)} for k, v in by_ds.items()],
        "items": items,
    }


@router.post("/grains/{grain_id}/verify", response_model=S.GrainOut)
def verify_grain(grain_id: str, user: User = Depends(current_user), session: Session = Depends(get_session)) -> dict:
    """只读核验唯一性：GROUP BY 键 HAVING COUNT(*) > 1 LIMIT 1，带超时；超时记为“未核验”。"""
    g = session.get(Grain, grain_id) or _404("粒度不存在")
    a = readable_asset(session, user, g.asset_id)
    access.require_role(session, user, "steward")
    # 核验会返回重复键的真实值：与问数相同，要求视图背后的完整来源都可见
    from avelis.ask.service import _live_check, upstream_closure

    allowed = access.domain_ids(session, user)
    closure = upstream_closure(session, [a.id])
    hidden = [x for x in closure.values() if not access.can_read_asset(session, user, x, allowed)]
    if hidden:
        raise HTTPException(
            403,
            Msg(
                "没有权限读取该对象背后的来源：{sources}",
                sources="、".join(sorted(f"{x.catalog_name}.{x.db_name}.{x.name}" for x in hidden)),
            ),
        )
    ds = session.get(Datasource, a.datasource_id)
    try:
        stale = _live_check(
            session,
            ds,
            query_connector(session, ds),
            closure.values(),
            [{"asset_id": a.id, "column": c} for c in g.columns],
            get_settings(),
        )
    except CapabilityMissing:
        stale = []
    if stale:
        raise HTTPException(
            409,
            Msg(
                "来源结构在上次采集之后已变化，已排队重新采集，采集完成后再核验：{changes}",
                changes="；".join(x["summary"] for x in stale),
            ),
        )
    sql = duplicate_key_sql(f"{a.catalog_name}.{a.db_name}.{a.name}", list(g.columns), dialect=dialect_of(ds))
    detail: dict[str, Any] = {"sql": sql}
    try:
        result = query_connector(session, ds).execute_select(
            sql, timeout_seconds=30, mem_limit_bytes=1024**3, max_rows=1
        )
        if result.rows:
            g.status = "violated"
            detail["duplicate_example"] = [str(v) for v in result.rows[0]]
        else:
            g.status = "verified"
    except QueryError as exc:
        g.status = "timeout" if exc.code == "timeout" else "failed"
        detail["error"] = exc.message
    except (CapabilityMissing, ValueError) as exc:
        g.status = "failed"
        detail["error"] = exc.args[0] if isinstance(exc, CapabilityMissing) else str(exc)
    g.verified_at = utcnow()
    g.verify_detail = detail
    audit.record(session, user, "grain.verify", "asset", a.id, grain_id=g.id, status=g.status)
    return {
        "id": g.id,
        "grain_id": g.id,
        "columns": g.columns,
        "origin": g.origin,
        "status": g.status,
        "verify_detail": {**detail, "error": translate(exc_message)}
        if (exc_message := detail.get("error")) is not None
        else detail,
    }


# --- 人工确认 ---------------------------------------------------------------------


class ConfirmBody(BaseModel):
    meaning: str = Field(min_length=1)
    reason: str | None = None


@router.post("/assets/{asset_id}/columns/{column}/confirmations", status_code=201)
def confirm_column(
    asset_id: str,
    column: str,
    body: ConfirmBody,
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
) -> dict:
    a = readable_asset(session, user, asset_id)
    if a.domain_id:
        access.require_domain_maintainer(session, user, a.domain_id)
    else:
        access.require_role(session, user, "steward")
    fp = current_fingerprints(session).get((a.id, column))
    if fp is None:
        raise HTTPException(404, "字段不存在或尚未计算推导指纹")
    c = Confirmation(
        asset_id=a.id,
        column_name=column,
        fingerprint=fp.fingerprint,
        meaning=body.meaning,
        reason=body.reason,
        confirmed_by=user.id,
    )
    session.add(c)
    session.flush()
    audit.record(session, user, "column.confirm", "asset", a.id, column=column, fingerprint=fp.fingerprint)
    return {"id": c.id, "fingerprint": c.fingerprint, "meaning": c.meaning}


# --- 血缘 -------------------------------------------------------------------------


@router.get("/lineage/graph", response_model=S.GraphOut)
def lineage_graph(
    domain_ids: str | None = Query(None, description="逗号分隔；多个域会串联显示"),
    asset_id: str | None = None,
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
) -> dict:
    """资产级节点、列级边、外部表身份映射与关联路径。只返回用户可见的资产。"""
    allowed = access.domain_ids(session, user)
    assets = {
        a.id: a
        for a in session.scalars(select(Asset).where(Asset.status == "active"))
        if access.can_read_asset(session, user, a, allowed)
    }
    if domain_ids:
        wanted = set(domain_ids.split(","))
        seeds = {i for i, a in assets.items() if a.domain_id in wanted}
    elif asset_id:
        seeds = {asset_id} if asset_id in assets else set()
    else:
        seeds = set(assets)

    # 端点：已采集资产用资产 ID；导入的设计时血缘中未采集的表用 "ref:库表"
    def endpoint(asset_id: str | None, ref: str | None) -> str | None:
        if asset_id:
            return asset_id if asset_id in assets else None
        return f"ref:{ref}" if ref else None

    edges = []
    for e in session.scalars(select(LineageEdge).where(LineageEdge.status == "active")):
        src, dst = endpoint(e.source_asset_id, e.source_ref), endpoint(e.target_asset_id, e.target_ref)
        if src and dst:
            edges.append((src, dst, e))
    links = [
        x
        for x in session.scalars(select(IdentityLink).where(IdentityLink.superseded_at.is_(None)))
        if x.source_asset_id in assets and (x.target_asset_id is None or x.target_asset_id in assets)
    ]
    # 从种子出发向上下游扩展（含身份映射），多个域时自然串联
    neighbours: dict[str, set[str]] = {}
    for src, dst, _ in edges:
        neighbours.setdefault(src, set()).add(dst)
        neighbours.setdefault(dst, set()).add(src)
    for x in links:
        if x.target_asset_id:
            neighbours.setdefault(x.source_asset_id, set()).add(x.target_asset_id)
            neighbours.setdefault(x.target_asset_id, set()).add(x.source_asset_id)
    included, stack = set(), list(seeds)
    while stack:
        node = stack.pop()
        if node in included:
            continue
        included.add(node)
        stack.extend(neighbours.get(node, ()))
    fpmap = current_fingerprint_map(session)
    nodes = []
    for i in included:
        if i.startswith("ref:"):
            nodes.append(
                {
                    "id": i,
                    "qualified": i[4:],
                    "kind": "design_time",
                    "status": "not_collected",
                    "grains": [],
                    "in_scope": False,
                    "note": gettext("导入代码中出现、尚未在数据源中采集到的表（设计时血缘）"),
                }
            )
        else:
            nodes.append(
                {
                    **asset_summary(session, assets[i]),
                    "grains": [_grain_view(g) for g in effective_grains(session, i, fpmap)],
                    "in_scope": i in seeds,
                }
            )
    grouped: dict[tuple, dict] = {}
    for src, dst, e in edges:
        if dst not in included:
            continue
        g = grouped.setdefault(
            (src, dst),
            {
                "source": src,
                "source_ref": e.source_ref,
                "target": dst,
                "columns": [],
                "relations": set(),
                "origins": set(),
            },
        )
        g["columns"].append(
            {
                "source_column": e.source_column,
                "target_column": e.target_column,
                "relation": e.relation,
                "expression": e.expression,
                "origin": e.origin,
                "attributes": e.attributes,
            }
        )
        g["relations"].add(e.relation)
        g["origins"].add(e.origin)
    edge_list = [{**g, "relations": sorted(g["relations"]), "origins": sorted(g["origins"])} for g in grouped.values()]
    joins = [
        {
            "id": j.id,
            "code": j.code,
            "left_asset_id": j.left_asset_id,
            "left_columns": j.left_columns,
            "right_asset_id": j.right_asset_id,
            "right_columns": j.right_columns,
            "cardinality": j.cardinality,
            "status": j.status,
        }
        for j in session.scalars(
            select(JoinPath).where(JoinPath.status.in_(["approved", "pending"])).order_by(JoinPath.code)
        )
        if j.left_asset_id in included and j.right_asset_id in included
    ]
    return {
        "nodes": nodes,
        "edges": edge_list,
        "identity_links": [_link_view(session, x) for x in links if x.source_asset_id in included],
        "joins": joins,
    }


def _404(message: str):
    raise HTTPException(404, message)
