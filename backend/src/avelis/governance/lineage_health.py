"""血缘体检：先用确定性规则检查血缘里的可疑之处，再可选地让模型给出复核结论。

规则只读当前数据（不改任何记录）：
- missing_column（严重）：血缘边引用的字段在已采集资产的当前字段里已不存在；
- uncollected_source（警告）：已采集的表依赖一张只在导入代码里出现、从未采集到的表（设计时引用）；
- definition_unparsed（警告）：视图等对象有定义，但解析失败 / 不支持 / 只解析了一部分，血缘可能缺失；
- pending_candidates（提示）：模型提出、尚未确认的候选边；其中置信度低于 LOW_CONFIDENCE 的另记 low_confidence（提示）。

核心规则是纯函数 `findings`（输入普通数据，便于单测）；`load_snapshot` 从数据库按权限组装输入，`check` / `review` 供接口调用。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from avelis.models import User

LOW_CONFIDENCE = 0.6
# 一次最多返回的发现条数（整图体检时防止响应过大）；summary 仍按全部计数
MAX_FINDINGS = 200
SEVERITY_ORDER = {"danger": 0, "warn": 1, "info": 2}
# 视为“解析不完整”的 ParseResult.status
UNPARSED_STATUSES = ("failed", "unsupported", "partial")


@dataclass(frozen=True)
class EdgeRow:
    id: str
    source_asset_id: str | None
    source_ref: str
    source_column: str | None
    target_asset_id: str | None
    target_ref: str | None
    target_column: str | None
    relation: str


@dataclass(frozen=True)
class AssetRow:
    id: str
    qualified: str
    kind: str
    columns: frozenset[str]
    # 当前版本定义的最新解析状态；没有定义（普通表）时为 None
    parse_status: str | None = None
    parse_reason: str | None = None


@dataclass(frozen=True)
class CandidateRow:
    id: str
    source_ref: str
    target_ref: str
    confidence: float | None


def findings(
    edges: list[EdgeRow],
    assets: dict[str, AssetRow],
    candidates: list[CandidateRow],
    focus: str | None = None,
) -> dict[str, Any]:
    """按规则产出发现。返回 `{"findings": [...], "summary": {code: count}, "truncated": bool}`。

    范围：focus 为资产 ID 时只看与它直接相连的边（作为来源或目标）、它自身的定义状态、目标是它的候选；
    focus 为 None 时看全部输入。assets 只包含当前用户可读的资产，引用不可读资产的边已在组装时剔除。

    每条发现是 dict：`{"code", "severity", "asset_id", "subject", "detail"}`：
    - missing_column：对每条边的每一端检查——该端 asset_id 在 assets 中、字段名非空、且不在该资产 columns 里时产出一条；
      asset_id 为该端资产，subject = `<qualified>.<字段>`，detail = `{"edge_id", "side": "source"|"target", "column"}`，
      severity "danger"。同一资产同一字段只报一次（多条边引用时 detail 里用第一条边，并加 `"edges": <边数>`）。
    - uncollected_source：source_asset_id 为空、target_asset_id 在 assets 中的边；按 (source_ref, target_asset_id) 去重，
      asset_id 为目标资产，subject = source_ref，detail = `{"target": <目标 qualified>}`，severity "warn"。
    - definition_unparsed：资产 parse_status ∈ UNPARSED_STATUSES 时产出；asset_id 为该资产，subject = qualified，
      detail = `{"status": parse_status, "reason": parse_reason}`，severity "warn"。focus 不为 None 时只检查 focus 资产。
    - pending_candidates：按目标聚合——目标 qualified 对应 assets 中某资产时，asset_id 为该资产，否则为 None；
      subject = target_ref，detail = `{"count": <候选数>}`，severity "info"。focus 不为 None 时只聚合目标是 focus 资产的候选
      （按 qualified 匹配）。
    - low_confidence：confidence 不为空且小于 LOW_CONFIDENCE 的候选，按目标聚合，同上结构，detail = `{"count", "min": <最低置信度>}`，
      severity "info"。
    排序：severity（danger、warn、info）→ code → subject。超过 MAX_FINDINGS 只保留前面的，truncated = True。
    summary 按截断前的全部发现计数，只包含出现过的 code。
    """
    # TODO(codex): 按上面的规则实现
    raise NotImplementedError


def load_snapshot(session: Session, user: User, asset_id: str | None) -> tuple[list[EdgeRow], dict[str, AssetRow], list[CandidateRow]]:
    """从数据库按权限组装 findings 的输入：
    - assets：当前用户可读的 active 资产（access.can_read_asset），columns 为 status == "active" 的 AssetColumn 名，
      parse_status / parse_reason 取当前版本 Definition 最新一条 ParseResult（没有定义则为 None）；
    - edges：status == "active" 的 LineageEdge；任一端引用了存在但不可读的资产时整条剔除；
    - candidates：status == "pending" 的 CandidateEdge，且所属 ImportJob 未被替换（status != "superseded"）；
    - asset_id 不为空时可只加载与它相关的部分以减少查询（结果须与全量加载后再按 focus 过滤一致）。
    qualified 与血缘图一致（参照 api/catalog.py 的 asset_summary / lineage_graph 的写法）。"""
    # TODO(codex): 实现
    raise NotImplementedError


def check(session: Session, user: User, asset_id: str | None) -> dict[str, Any]:
    """接口用：asset_id 给出时先按 api/catalog.py 的 readable_asset 同等规则校验可读（不可读 / 不存在 → NotFound），
    再 load_snapshot + findings(focus=asset_id)。返回 findings 的结果并加 `"scope": "asset" | "all"`。"""
    # TODO(codex): 实现
    raise NotImplementedError


def review(session: Session, user: User, asset_id: str) -> dict[str, Any]:
    """让模型复核某张表的体检结果（只读，不写任何治理记录）。参照 governance/lineage_notes.generate 的写法：
    - 策略关闭发送代码（modelcfg.policy(session).get("send_code") 为假）→ `{"skipped": "policy"}`；
      没有可用模型（modelcfg.resolve(session, "explain") 为 None）→ `{"skipped": "no_model"}`。
    - 发给模型：表的 qualified、字段名列表、与它直接相连的边（两端 qualified、字段、relation，表达式不发）、check 的发现；不含任何数据行。
    - 要求模型只输出 JSON：`{"verdict": "ok"|"suspect"|"unknown", "summary": "<一句话>", "items": [{"code": ..., "subject": ..., "note": "<一句话>"}]}`；
      解析失败或 verdict 不在三者内 → 抛 Invalid(Msg("模型没有返回可用的复核结论，请稍后再试"))；模型或网络异常同样处理，不让接口 500。
    - 返回 `{"verdict", "summary", "items", "findings": <check 的结果>}`；记审计 `lineage_health.review`（带 asset_id、verdict、发现条数）。
    系统提示用中文，要求结论保守：发现为空且结构正常才可 ok；有 danger 级发现时不得 ok。"""
    # TODO(codex): 实现
    raise NotImplementedError
