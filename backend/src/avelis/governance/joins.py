"""关联路径：基数由两侧粒度推出；涉及的每个业务域都要由其负责人批准，跨域路径需要两个域都批准。"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from avelis import audit
from avelis.collect.service import current_fingerprints
from avelis.governance import access
from avelis.governance.errors import Conflict, Forbidden, Invalid, NotFound
from avelis.governance.metrics import current_fingerprint_map
from avelis.lineage.grain import cardinality
from avelis.models import Asset, ColumnVersion, Domain, JoinPath, JoinPathApproval, User


def _columns(session: Session, asset: Asset) -> set[str]:
    return set(session.scalars(select(ColumnVersion.name).where(
        ColumnVersion.asset_version_id == asset.current_version_id)))


def required_domains(session: Session, path: JoinPath) -> list[str]:
    ids = []
    for asset_id in (path.left_asset_id, path.right_asset_id):
        asset = session.get(Asset, asset_id)
        if asset.domain_id is None:
            raise Conflict(f"资产 {asset.db_name}.{asset.name} 未归属业务域，无法确定由谁批准关联路径")
        if asset.domain_id not in ids:
            ids.append(asset.domain_id)
    return ids


def create(session: Session, actor: User, *, code: str, name: str, left_asset_id: str, left_columns: list[str],
           right_asset_id: str, right_columns: list[str]) -> JoinPath:
    access.require_role(session, actor, "steward")
    if session.scalar(select(JoinPath.id).where(JoinPath.code == code)):
        raise Conflict(f"关联路径编码 {code} 已存在")
    left, right = session.get(Asset, left_asset_id), session.get(Asset, right_asset_id)
    if left is None or right is None or "deleted" in (left.status, right.status):
        raise NotFound("关联两侧的资产不存在或已删除")
    if not left_columns or len(left_columns) != len(right_columns):
        raise Invalid("关联键两侧数量必须一致且不为空")
    for asset, cols in ((left, left_columns), (right, right_columns)):
        missing = set(cols) - _columns(session, asset)
        if missing:
            raise Invalid(f"{asset.db_name}.{asset.name} 没有列：{', '.join(sorted(missing))}")
    card = cardinality(session, left.id, left_columns, right.id, right_columns,
                       fingerprints=current_fingerprint_map(session))
    fps = current_fingerprints(session)
    dependency = [{"asset_id": a.id, "column": c, "fingerprint": getattr(fps.get((a.id, c)), "fingerprint", None)}
                  for a, cols in ((left, left_columns), (right, right_columns)) for c in cols]
    path = JoinPath(code=code, name=name, left_asset_id=left.id, left_columns=left_columns, right_asset_id=right.id,
                    right_columns=right_columns, cardinality=card.value, cardinality_basis=card.basis,
                    status="pending", dependency=dependency, created_by=actor.id)
    session.add(path)
    session.flush()
    required_domains(session, path)
    audit.record(session, actor, "join_path.create", "join_path", path.id, code=code, cardinality=card.value)
    return path


def decide(session: Session, actor: User, path: JoinPath, domain_id: str, decision: str, note: str | None) -> JoinPath:
    if decision not in ("approved", "rejected"):
        raise Invalid("decision 只能是 approved 或 rejected")
    domains = required_domains(session, path)
    if domain_id not in domains:
        raise Invalid("该业务域不涉及这条关联路径")
    domain = session.get(Domain, domain_id)
    if domain.owner_id != actor.id:
        raise Forbidden(f"只有业务域“{domain.name}”的负责人可以代表该域批准")
    if path.status not in ("pending",):
        raise Conflict(f"关联路径当前状态为 {path.status}，不能审批" +
                       ("：依据已变化，请先在变更中处理复核任务" if path.status == "affected" else ""))
    existing = session.scalar(select(JoinPathApproval).where(JoinPathApproval.join_path_id == path.id,
                                                             JoinPathApproval.domain_id == domain_id))
    if existing is None:
        session.add(JoinPathApproval(join_path_id=path.id, domain_id=domain_id, user_id=actor.id, decision=decision,
                                     note=note))
    else:
        existing.decision, existing.note, existing.user_id = decision, note, actor.id
    session.flush()
    decisions = {a.domain_id: a.decision for a in session.scalars(
        select(JoinPathApproval).where(JoinPathApproval.join_path_id == path.id))}
    if decision == "rejected":
        path.status = "rejected"
    elif all(decisions.get(d) == "approved" for d in domains):
        path.status = "approved"
    audit.record(session, actor, "join_path.decide", "join_path", path.id, domain_id=domain_id, decision=decision,
                 note=note, status=path.status)
    return path


def reset_approvals(session: Session, path: JoinPath, status: str, reason: str) -> None:
    """依据变化后，已有批准全部作废：按当前涉及的业务域重新收集，不沿用任何一方的旧批准。"""
    for approval in session.scalars(select(JoinPathApproval).where(JoinPathApproval.join_path_id == path.id)):
        session.delete(approval)
    path.status = status
    audit.record(session, None, "join_path.reapproval_required", "join_path", path.id, reason=reason, status=status)


def effectively_approved(session: Session, path: JoinPath) -> bool:
    """问数与发布时的批准判断：状态为已批准，且当前涉及的每个业务域都有批准（不只看状态字符串）。"""
    if path.status != "approved":
        return False
    try:
        domains = required_domains(session, path)
    except Conflict:
        return False
    decisions = {a.domain_id: a.decision for a in session.scalars(
        select(JoinPathApproval).where(JoinPathApproval.join_path_id == path.id))}
    return all(decisions.get(d) == "approved" for d in domains)


def on_asset_domain_changed(session: Session, asset_id: str) -> list[str]:
    """资产换了业务域：涉及它的关联路径需要按新的业务域重新批准。"""
    affected = []
    for path in session.scalars(select(JoinPath).where(
            (JoinPath.left_asset_id == asset_id) | (JoinPath.right_asset_id == asset_id),
            JoinPath.status.in_(("approved", "pending")))):
        reset_approvals(session, path, "pending", "关联一侧的资产改归其他业务域")
        affected.append(path.id)
    return affected


def drift(session: Session, path: JoinPath) -> list[dict]:
    fps = current_fingerprints(session)
    out = []
    for dep in path.dependency or []:
        fp = fps.get((dep["asset_id"], dep["column"]))
        if fp is None or fp.fingerprint != dep["fingerprint"] or fp.status != "ok":
            out.append({**dep, "current": fp.fingerprint if fp else None, "status": fp.status if fp else "broken"})
    return out


def path_or_404(session: Session, path_id: str) -> JoinPath:
    path = session.get(JoinPath, path_id)
    if path is None:
        raise NotFound("关联路径不存在")
    return path
