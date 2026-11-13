"""资产档案页的聚合（ADR 0022）。只读；每一项都按当前用户的权限裁剪。

入口是 build()；各段各一个函数，便于单测。字段血缘的说明读写在 avelis.governance.lineage_notes。"""

from __future__ import annotations

from sqlalchemy.orm import Session

from avelis.models import Asset, User

ASKED_DAYS = 30


def build(session: Session, user: User, asset: Asset) -> dict:
    """返回 schemas.AssetDossierOut 形状的 dict。调用方已确认 user 可读 asset。

    owner 取资产所在业务域的负责人（未归属业务域为空）。summary：当前版本有表注释时 source=comment；
    否则有整表说明（lineage_notes.notes_for 的空串键）时 source=note。fields 的 note 来自 notes_for。
    can_edit_notes = lineage_notes.can_edit；model_available = lineage_notes.model_available。"""
    raise NotImplementedError


def field_lineage(session: Session, asset: Asset) -> list[dict]:
    """本表每个字段的来历（schemas.DossierFieldOut，不含 note，另带 basis_hash 供说明比对）。

    取 target_asset_id = asset.id、status = active、relation 为 value_* 的列级边，按 target_column 归并：
    sources 去重；同一字段既有数据库定义解析（origin=parsed）又有导入的边时以 parsed 的表达式与关系为准。
    顺序按当前版本的字段顺序，没有任何来源的字段不出现。"""
    raise NotImplementedError


def basis_hash(relation: str | None, expression: str | None, sources: list[dict]) -> str:
    """字段来历的校验码：关系、表达式（去首尾空白）与排序后的来源（ref, column）的 sha256。
    lineage_notes 用它判断说明是否过期，两边必须用同一个函数。"""
    raise NotImplementedError


def neighbors(session: Session, user: User, asset: Asset) -> tuple[list[dict], list[dict]]:
    """直接上游与直接下游的表（schemas.DossierTableRefOut），各按 ref 排序。

    只看一跳、status = active 的边（含表级与影响类关系）；columns 是涉及的本表字段数（上游）或对方字段数（下游）。
    对方未采集（asset_id 为空）或当前用户不可读时，asset_id 置空、不给注释，只保留写法。"""
    raise NotImplementedError


def metrics_using(session: Session, user: User, asset: Asset) -> list[dict]:
    """用到这张表的指标的当前口径（schemas.DossierMetricOut）。

    每个指标取一个版本：有 published 用 published，否则取最新的 pending_review / affected / draft；
    retired、archived、rejected 不算。role：base_asset_id 命中为 base，dependency[*].asset_id 命中为 dependency，
    经 joins[*].join_path_id 的关联路径任一侧命中为 join（取最强的一种，base > dependency > join）。
    只返回当前用户可见业务域的指标。"""
    raise NotImplementedError


def asked(session: Session, user: User, asset: Asset, days: int = ASKED_DAYS) -> dict:
    """最近谁问过（schemas.DossierAskedOut）：近 days 天内引用过这张表的问数回答与分析运行，按人归并。

    问数看 answer.evidence["sources"][*].asset_id；分析看 analysis_dataset.meta 的 tables[*].asset_id 与 asset_ids，
    同一次分析运行只算一次。people 按最近一次时间倒序，最多 8 人；total 是总次数。
    last_question 只在该人是当前用户、或当前用户是管理员时给出，其余为空。"""
    raise NotImplementedError


def produced(session: Session, user: User, asset: Asset, *, show_sql: bool) -> dict | None:
    """这张表是怎么产出的（schemas.DossierProducedOut）；视图返回 kind=view，没有导入脚本写入它时返回 None。

    脚本来自写入这张表的导入边（origin 为 import / model_confirmed，import_job_id 指向未被取代的 import_job）
    及其 attributes.statement_id 对应的 extracted_statement；按文件名、行号排序，最多 5 段。
    show_sql 为假时 sql 置空。write_mode 只在能确定时给出：有 INSERT OVERWRITE，或同一文件里先 TRUNCATE / 整表 DELETE
    这张表再 INSERT → overwrite；CREATE TABLE AS（含 CREATE OR REPLACE）→ create；只有 INSERT 且同文件没有清空它的语句
    → append；MERGE、UPDATE 或几种写法并存 → 空。"""
    raise NotImplementedError
