"""M4B 资产数据安全策略接口：列级脱敏策略（PII）与行级权限（RLS）谓词配置。"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from avelis import audit
from avelis.api.catalog import readable_asset
from avelis.api.deps import current_user, get_session
from avelis.db import utcnow
from avelis.governance import access, data_policy
from avelis.governance.access import ROLES
from avelis.i18n import Msg
from avelis.models import Asset, AssetColumnPolicy, AssetRlsPolicy, ColumnVersion, User

router = APIRouter(prefix="/api", tags=["数据安全策略"])


class ColumnPolicyUpsert(BaseModel):
    classification: Literal["pii", "sensitive", "confidential"] = "pii"
    masking_strategy: Literal["none", "mask", "hash", "redact"] = "mask"
    exempt_roles: list[str] = Field(default_factory=list)
    note: str | None = Field(default=None, max_length=500)


class RlsPolicyCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    scope_type: Literal["all", "role", "domain", "user"] = "all"
    scope_value: str | None = Field(default=None, max_length=128)
    predicate_sql: str = Field(min_length=1, max_length=2000)
    enabled: bool = True


class RlsPolicyPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    scope_type: Literal["all", "role", "domain", "user"] | None = None
    scope_value: str | None = Field(default=None, max_length=128)
    predicate_sql: str | None = Field(default=None, min_length=1, max_length=2000)
    enabled: bool | None = None


def _require_policy_writer(session: Session, user: User, asset: Asset) -> None:
    if asset.domain_id:
        access.require_domain_maintainer(session, user, asset.domain_id)
    else:
        access.require_role(session, user, "steward")


def _asset_columns(session: Session, asset: Asset) -> list[str]:
    if not asset.current_version_id:
        return []
    return list(
        session.scalars(
            select(ColumnVersion.name)
            .where(ColumnVersion.asset_version_id == asset.current_version_id)
            .order_by(ColumnVersion.ordinal)
        )
    )


def _col_view(p: AssetColumnPolicy) -> dict:
    return {
        "id": p.id,
        "asset_id": p.asset_id,
        "column_name": p.column_name,
        "classification": p.classification,
        "masking_strategy": p.masking_strategy,
        "exempt_roles": list(p.exempt_roles or []),
        "note": p.note,
        "updated_by": p.updated_by,
        "updated_at": p.updated_at.isoformat() if p.updated_at else None,
    }


def _rls_view(p: AssetRlsPolicy) -> dict:
    return {
        "id": p.id,
        "asset_id": p.asset_id,
        "name": p.name,
        "scope_type": p.scope_type,
        "scope_value": p.scope_value,
        "predicate_sql": p.predicate_sql,
        "enabled": p.enabled,
        "updated_by": p.updated_by,
        "created_at": p.created_at.isoformat() if p.created_at else None,
        "updated_at": p.updated_at.isoformat() if p.updated_at else None,
    }


@router.get("/assets/{asset_id}/security-policies")
def list_security_policies(
    asset_id: str,
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
) -> dict:
    asset = readable_asset(session, user, asset_id)
    cols = _asset_columns(session, asset)
    col_pols = list(
        session.scalars(
            select(AssetColumnPolicy)
            .where(AssetColumnPolicy.asset_id == asset.id)
            .order_by(AssetColumnPolicy.column_name)
        )
    )
    rls_pols = list(
        session.scalars(
            select(AssetRlsPolicy).where(AssetRlsPolicy.asset_id == asset.id).order_by(AssetRlsPolicy.created_at)
        )
    )
    return {
        "asset_id": asset.id,
        "columns": cols,
        "column_policies": [_col_view(p) for p in col_pols],
        "rls_policies": [_rls_view(p) for p in rls_pols],
    }


@router.put("/assets/{asset_id}/column-policies/{column}")
def upsert_column_policy(
    asset_id: str,
    column: str,
    body: ColumnPolicyUpsert,
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
) -> dict:
    asset = readable_asset(session, user, asset_id)
    _require_policy_writer(session, user, asset)
    cols = _asset_columns(session, asset)
    matched_col = next((c for c in cols if c.casefold() == column.casefold()), None)
    if matched_col is None:
        raise HTTPException(404, Msg("字段 {column} 不存在或尚未采集", column=column))
    unknown_roles = sorted(set(body.exempt_roles) - set(ROLES))
    if unknown_roles:
        raise HTTPException(422, Msg("未知角色：{roles}", roles="、".join(unknown_roles)))
    exempt = sorted(set(body.exempt_roles))
    pol = session.scalar(
        select(AssetColumnPolicy).where(
            AssetColumnPolicy.asset_id == asset.id, AssetColumnPolicy.column_name == matched_col
        )
    )
    if pol is None:
        pol = AssetColumnPolicy(
            asset_id=asset.id,
            column_name=matched_col,
            classification=body.classification,
            masking_strategy=body.masking_strategy,
            exempt_roles=exempt,
            note=body.note,
            updated_by=user.id,
            updated_at=utcnow(),
        )
        session.add(pol)
    else:
        pol.classification = body.classification
        pol.masking_strategy = body.masking_strategy
        pol.exempt_roles = exempt
        pol.note = body.note
        pol.updated_by = user.id
        pol.updated_at = utcnow()
    session.flush()
    audit.record(
        session,
        user,
        "asset_column_policy.upsert",
        "asset",
        asset.id,
        column=matched_col,
        classification=pol.classification,
        masking_strategy=pol.masking_strategy,
        exempt_roles=exempt,
    )
    return _col_view(pol)


@router.delete("/assets/{asset_id}/column-policies/{column}", status_code=204)
def delete_column_policy(
    asset_id: str,
    column: str,
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
) -> Response:
    asset = readable_asset(session, user, asset_id)
    _require_policy_writer(session, user, asset)
    pol = session.scalar(
        select(AssetColumnPolicy).where(AssetColumnPolicy.asset_id == asset.id, AssetColumnPolicy.column_name == column)
    )
    if pol is None:
        raise HTTPException(404, "列级脱敏策略不存在")
    session.delete(pol)
    session.flush()
    audit.record(session, user, "asset_column_policy.delete", "asset", asset.id, column=column)
    return Response(status_code=204)


def _validate_rls_fields(scope_type: str, scope_value: str | None, predicate_sql: str, cols: list[str]) -> str | None:
    if scope_type == "all":
        cleaned_scope = None
    else:
        cleaned_scope = (scope_value or "").strip()
        if not cleaned_scope:
            raise HTTPException(422, "指定作用域时必须填写 scope_value")
        if scope_type == "role" and cleaned_scope not in ROLES:
            raise HTTPException(422, Msg("未知角色：{roles}", roles=cleaned_scope))
    try:
        data_policy.validate_rls_predicate(predicate_sql, set(cols))
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    return cleaned_scope


@router.post("/assets/{asset_id}/rls-policies", status_code=201)
def create_rls_policy(
    asset_id: str,
    body: RlsPolicyCreate,
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
) -> dict:
    asset = readable_asset(session, user, asset_id)
    _require_policy_writer(session, user, asset)
    cols = _asset_columns(session, asset)
    cleaned_scope = _validate_rls_fields(body.scope_type, body.scope_value, body.predicate_sql, cols)
    pol = AssetRlsPolicy(
        asset_id=asset.id,
        name=body.name.strip(),
        scope_type=body.scope_type,
        scope_value=cleaned_scope,
        predicate_sql=body.predicate_sql.strip(),
        enabled=body.enabled,
        updated_by=user.id,
        created_at=utcnow(),
        updated_at=utcnow(),
    )
    session.add(pol)
    session.flush()
    audit.record(
        session,
        user,
        "asset_rls_policy.create",
        "asset",
        asset.id,
        policy_id=pol.id,
        name=pol.name,
        scope_type=pol.scope_type,
        enabled=pol.enabled,
    )
    return _rls_view(pol)


@router.patch("/assets/{asset_id}/rls-policies/{policy_id}")
def update_rls_policy(
    asset_id: str,
    policy_id: str,
    body: RlsPolicyPatch,
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
) -> dict:
    asset = readable_asset(session, user, asset_id)
    _require_policy_writer(session, user, asset)
    pol = session.get(AssetRlsPolicy, policy_id)
    if pol is None or pol.asset_id != asset.id:
        raise HTTPException(404, "行级权限策略不存在")
    next_scope_type = body.scope_type if body.scope_type is not None else pol.scope_type
    next_scope_value = body.scope_value if body.scope_value is not None else pol.scope_value
    next_predicate = body.predicate_sql.strip() if body.predicate_sql is not None else pol.predicate_sql
    cols = _asset_columns(session, asset)
    cleaned_scope = _validate_rls_fields(next_scope_type, next_scope_value, next_predicate, cols)
    if body.name is not None:
        pol.name = body.name.strip()
    pol.scope_type = next_scope_type
    pol.scope_value = cleaned_scope
    pol.predicate_sql = next_predicate
    if body.enabled is not None:
        pol.enabled = body.enabled
    pol.updated_by = user.id
    pol.updated_at = utcnow()
    session.flush()
    audit.record(
        session,
        user,
        "asset_rls_policy.update",
        "asset",
        asset.id,
        policy_id=pol.id,
        name=pol.name,
        scope_type=pol.scope_type,
        enabled=pol.enabled,
    )
    return _rls_view(pol)


@router.delete("/assets/{asset_id}/rls-policies/{policy_id}", status_code=204)
def delete_rls_policy(
    asset_id: str,
    policy_id: str,
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
) -> Response:
    asset = readable_asset(session, user, asset_id)
    _require_policy_writer(session, user, asset)
    pol = session.get(AssetRlsPolicy, policy_id)
    if pol is None or pol.asset_id != asset.id:
        raise HTTPException(404, "行级权限策略不存在")
    session.delete(pol)
    session.flush()
    audit.record(
        session,
        user,
        "asset_rls_policy.delete",
        "asset",
        asset.id,
        policy_id=policy_id,
        name=pol.name,
    )
    return Response(status_code=204)
