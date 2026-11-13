"""档案页的人话说明（ADR 0022）：模型起草，负责人确认，按字段来历的校验码判断是否过期。"""

from __future__ import annotations

from sqlalchemy.orm import Session

from avelis.models import Asset, User

MAX_TEXT = 500
# 一次请求最多让模型写多少个字段，避免一张宽表产生过大的请求
MAX_GENERATE = 60


def can_edit(session: Session, user: User, asset: Asset) -> bool:
    """能否确认、改写、生成说明：资产所在业务域的负责人或维护者（管理员恒可）；未归属业务域的表需要 steward。"""
    raise NotImplementedError


def model_available(session: Session) -> bool:
    """有没有能用来起草说明的模型：modelcfg.policy()["send_code"] 为真，
    且 modelcfg.resolve(session, "explain") 不为空。"""
    raise NotImplementedError


def notes_for(session: Session, asset: Asset, basis: dict[str, str]) -> dict[str, dict]:
    """这张表现有的说明，按 column_name（空串 = 整张表）索引，值为 schemas.LineageNoteOut 形状。

    basis 是 {字段: 当前 basis_hash}（来自 dossier.field_lineage）；字段说明记录的 basis_hash 与当前不同、
    或字段已没有来历时，状态显示为 stale。整张表的说明不判断过期。"""
    raise NotImplementedError


def save(session: Session, user: User, asset: Asset, column: str, text: str) -> dict:
    """保存即确认：写入或覆盖（资产，字段）的说明为 confirmed，记录确认人与当前 basis_hash，并记审计
    （动作 lineage_note.confirm）。文本与现有模型草稿相同则 origin 保持 model，否则为 human。
    column 非空时必须是本表当前有来历的字段，否则 NotFound；无权限 Forbidden。返回 LineageNoteOut 形状。"""
    raise NotImplementedError


def generate(session: Session, user: User, asset: Asset, columns: list[str] | None, *, summary: bool) -> dict:
    """让模型起草说明，写成 draft（schemas.LineageNoteGenerateOut）。权限同 save。

    不覆盖已确认且未过期的说明；columns 为空时取所有需要写的字段，最多 MAX_GENERATE 个。
    summary 为真且这张表没有数据库注释、也没有已确认的整表说明时，同时起草一句整表说明。
    发给模型的只有表名、字段名、字段注释、来源字段与表达式（表达式先经 importer.candidates.redact 脱敏），不含任何数据。
    没有可用模型返回 skipped=no_model，策略关闭返回 policy，没有可写的返回 nothing；模型输出解析失败的字段跳过。
    记审计（动作 lineage_note.generate，带字段数与 model_adapter）。"""
    raise NotImplementedError
