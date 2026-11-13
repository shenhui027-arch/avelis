"""资产档案页（ADR 0022）：一张表一页的聚合视图，以及字段血缘的人话说明。

聚合只读、按当前用户的权限裁剪；说明由模型起草、负责人确认。业务逻辑在
avelis.governance.dossier 与 avelis.governance.lineage_notes，这里只做路由。"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from avelis.api import schemas as S
from avelis.api.catalog import readable_asset
from avelis.api.deps import current_user, get_session
from avelis.governance import dossier, lineage_notes
from avelis.models import User

router = APIRouter(prefix="/api", tags=["资产档案"])


@router.get("/assets/{asset_id}/dossier", response_model=S.AssetDossierOut)
def asset_dossier(asset_id: str, user: User = Depends(current_user), session: Session = Depends(get_session)) -> dict:
    return dossier.build(session, user, readable_asset(session, user, asset_id))


@router.put("/assets/{asset_id}/lineage-notes", response_model=S.LineageNoteOut)
def save_lineage_note(
    asset_id: str,
    body: S.LineageNoteIn,
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
) -> dict:
    """确认或改写一句说明（保存即确认）。需要这张表所在业务域的负责人或维护者；未归属业务域的表需要维护者角色。"""
    asset = readable_asset(session, user, asset_id)
    return lineage_notes.save(session, user, asset, body.column or "", body.text)


@router.post("/assets/{asset_id}/lineage-notes/generate", response_model=S.LineageNoteGenerateOut)
def generate_lineage_notes(
    asset_id: str,
    body: S.LineageNoteGenerateIn,
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
) -> dict:
    """让模型起草说明（草稿，待确认）。权限同保存；没有可用模型或已关闭向模型发送代码片段时不生成，返回原因。"""
    asset = readable_asset(session, user, asset_id)
    return lineage_notes.generate(session, user, asset, body.columns, summary=body.summary)
