"""Avelis 元数据模型：四层。

身份层   稳定 ID，与名称分离：asset、asset_column、datasource、endpoint_alias
观察层   只追加：collection_run、asset_version、column_version、definition
推导层   可重算，带解析器版本：parse_result、lineage_edge、identity_link、column_fingerprint、grain
治理层   人工决定，只追加历史：domain、metric、metric_version、join_path、confirmation、
         change_set、change_event、impact、review_task、audit_event

上层只引用下层的具体版本或指纹；“当前”只是指针（current_version_id、superseded_at IS NULL）。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from avelis.db import Base, IdType, JsonType, LongText, UTCDateTime, new_id, utcnow


def pk() -> Mapped[str]:
    return mapped_column(IdType, primary_key=True, default=new_id)


def fk(target: str, nullable: bool = False, index: bool = True) -> Mapped:
    return mapped_column(IdType, ForeignKey(target), nullable=nullable, index=index)


def created() -> Mapped[datetime]:
    return mapped_column(UTCDateTime, default=utcnow, nullable=False)


# --- 用户与权限 -------------------------------------------------------------------

class User(Base):
    __tablename__ = "app_user"
    id: Mapped[str] = pk()
    username: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String(128), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = created()


class UserRole(Base):
    """全局角色：admin 管理员、steward 维护者、reviewer 复核者、analyst 查询者。"""

    __tablename__ = "user_role"
    __table_args__ = (UniqueConstraint("user_id", "role"),)
    id: Mapped[str] = pk()
    user_id: Mapped[str] = fk("app_user.id")
    role: Mapped[str] = mapped_column(String(32), nullable=False)


class Domain(Base):
    __tablename__ = "domain"
    id: Mapped[str] = pk()
    code: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    owner_id: Mapped[str] = fk("app_user.id")
    created_at: Mapped[datetime] = created()


class DomainMember(Base):
    """业务域成员：owner 负责人、maintainer 维护者、viewer 可查询。成员才能检索和查询该域的资产与口径。"""

    __tablename__ = "domain_member"
    __table_args__ = (UniqueConstraint("domain_id", "user_id"),)
    id: Mapped[str] = pk()
    domain_id: Mapped[str] = fk("domain.id")
    user_id: Mapped[str] = fk("app_user.id")
    role: Mapped[str] = mapped_column(String(16), nullable=False)


# --- 数据源（身份层） -------------------------------------------------------------

class Datasource(Base):
    __tablename__ = "datasource"
    id: Mapped[str] = pk()
    name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)  # starrocks | mysql | fake
    host: Mapped[str] = mapped_column(String(255), nullable=False)
    port: Mapped[int] = mapped_column(Integer, nullable=False)
    options: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)
    is_test_adapter: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="active", nullable=False)
    server_identity: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)
    collect_interval_minutes: Mapped[int] = mapped_column(Integer, default=15, nullable=False)
    created_by: Mapped[str | None] = fk("app_user.id", nullable=True)
    created_at: Mapped[datetime] = created()


class DatasourceCredential(Base):
    """口令只以密文存储，接口永不回传。purpose：collect 采集、query 只读查询。"""

    __tablename__ = "datasource_credential"
    __table_args__ = (UniqueConstraint("datasource_id", "purpose"),)
    id: Mapped[str] = pk()
    datasource_id: Mapped[str] = fk("datasource.id")
    purpose: Mapped[str] = mapped_column(String(16), nullable=False)
    username: Mapped[str] = mapped_column(String(128), nullable=False)
    secret_enc: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = created()


class EndpointAlias(Base):
    """同一个数据库在不同网络位置的地址（如 StarRocks 看到 shop-mysql:3306，采集器看到 127.0.0.1:13306）。"""

    __tablename__ = "endpoint_alias"
    __table_args__ = (UniqueConstraint("host", "port"),)
    id: Mapped[str] = pk()
    datasource_id: Mapped[str] = fk("datasource.id")
    host: Mapped[str] = mapped_column(String(255), nullable=False)
    port: Mapped[int] = mapped_column(Integer, nullable=False)
    note: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[str | None] = fk("app_user.id", nullable=True)
    created_at: Mapped[datetime] = created()


# --- 资产身份 ---------------------------------------------------------------------

class Asset(Base):
    """资产身份。名称只是当前属性；身份判定见 avelis.collect.identity。"""

    __tablename__ = "asset"
    __table_args__ = (Index("ix_asset_name", "datasource_id", "catalog_name", "db_name", "name"),)
    id: Mapped[str] = pk()
    datasource_id: Mapped[str] = fk("datasource.id", index=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    catalog_name: Mapped[str] = mapped_column(String(128), nullable=False)
    db_name: Mapped[str] = mapped_column(String(128), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    physical_id: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), default="active", nullable=False)
    domain_id: Mapped[str | None] = fk("domain.id", nullable=True)
    current_version_id: Mapped[str | None] = mapped_column(IdType)
    first_seen_at: Mapped[datetime] = created()
    last_seen_at: Mapped[datetime] = created()
    deleted_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class AssetColumn(Base):
    """字段身份。字段消失后再出现视为新字段（新 ID），不会沿用旧确认。"""

    __tablename__ = "asset_column"
    __table_args__ = (Index("ix_asset_column_name", "asset_id", "name"),)
    id: Mapped[str] = pk()
    asset_id: Mapped[str] = fk("asset.id", index=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="active", nullable=False)
    first_seen_at: Mapped[datetime] = created()
    last_seen_at: Mapped[datetime] = created()
    deleted_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


# --- 观察层（只追加） -------------------------------------------------------------

class CollectionRun(Base):
    __tablename__ = "collection_run"
    id: Mapped[str] = pk()
    datasource_id: Mapped[str] = fk("datasource.id")
    trigger: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    started_at: Mapped[datetime] = created()
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    previous_run_id: Mapped[str | None] = mapped_column(IdType)
    snapshot_hash: Mapped[str | None] = mapped_column(String(64))
    stats: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)
    error: Mapped[str | None] = mapped_column(Text)
    triggered_by: Mapped[str | None] = fk("app_user.id", nullable=True)


class AssetVersion(Base):
    """资产的一次观察结果。内容哈希不变时只更新 last_seen，不产生新版本（重复采集幂等）。"""

    __tablename__ = "asset_version"
    __table_args__ = (UniqueConstraint("asset_id", "version_no"),)
    id: Mapped[str] = pk()
    asset_id: Mapped[str] = fk("asset.id")
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    observed_run_id: Mapped[str] = fk("collection_run.id", index=False)
    last_seen_run_id: Mapped[str] = mapped_column(IdType, nullable=False)
    observed_at: Mapped[datetime] = created()
    last_seen_at: Mapped[datetime] = created()
    superseded_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    catalog_name: Mapped[str] = mapped_column(String(128), nullable=False)
    db_name: Mapped[str] = mapped_column(String(128), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    table_model: Mapped[str | None] = mapped_column(String(32))
    key_columns: Mapped[list] = mapped_column(JsonType, default=list, nullable=False)
    unique_keys: Mapped[list] = mapped_column(JsonType, default=list, nullable=False)
    comment: Mapped[str | None] = mapped_column(Text)
    properties: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)


class ColumnVersion(Base):
    __tablename__ = "column_version"
    id: Mapped[str] = pk()
    asset_version_id: Mapped[str] = fk("asset_version.id")
    column_id: Mapped[str] = fk("asset_column.id")
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    data_type: Mapped[str] = mapped_column(String(255), nullable=False)
    nullable: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    comment: Mapped[str | None] = mapped_column(Text)
    is_key: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class Definition(Base):
    """视图等对象的定义原文与规范化指纹（只改格式不改变指纹）。"""

    __tablename__ = "definition"
    id: Mapped[str] = pk()
    asset_version_id: Mapped[str] = fk("asset_version.id")
    dialect: Mapped[str] = mapped_column(String(32), nullable=False)
    raw_sql: Mapped[str] = mapped_column(LongText, nullable=False)
    canonical_sql: Mapped[str | None] = mapped_column(LongText)
    canonical_hash: Mapped[str] = mapped_column(String(64), nullable=False)


# --- 推导层（可重算） -------------------------------------------------------------

class ParseResult(Base):
    __tablename__ = "parse_result"
    __table_args__ = (UniqueConstraint("definition_id", "parser_version"),)
    id: Mapped[str] = pk()
    definition_id: Mapped[str] = fk("definition.id", index=False)
    parser_version: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)  # ok | partial | unsupported | failed
    reason: Mapped[str | None] = mapped_column(Text)
    details: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)
    created_at: Mapped[datetime] = created()


class LineageEdge(Base):
    """列级血缘。relation 区分取值（value_*）与影响（filter / join_key / group_by）。
    origin：parsed 解析、manual 人工、import 导入（M2）、model_candidate 模型候选（不计入已确认血缘）。"""

    __tablename__ = "lineage_edge"
    __table_args__ = (
        Index("ix_lineage_target", "target_asset_id", "status"),
        Index("ix_lineage_source", "source_asset_id", "status"),
    )
    id: Mapped[str] = pk()
    origin: Mapped[str] = mapped_column(String(32), nullable=False)
    parse_result_id: Mapped[str | None] = mapped_column(IdType)
    target_asset_id: Mapped[str] = mapped_column(IdType, nullable=False)
    target_asset_version_id: Mapped[str | None] = mapped_column(IdType)
    target_column: Mapped[str | None] = mapped_column(String(255))
    source_asset_id: Mapped[str | None] = mapped_column(IdType)
    source_ref: Mapped[str] = mapped_column(String(512), nullable=False)
    source_column: Mapped[str | None] = mapped_column(String(255))
    relation: Mapped[str] = mapped_column(String(32), nullable=False)
    expression: Mapped[str | None] = mapped_column(LongText)
    attributes: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="active", nullable=False)
    created_by: Mapped[str | None] = mapped_column(IdType)
    reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = created()
    superseded_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class IdentityLink(Base):
    """外部表 / Catalog 表 → 他库真实表的身份映射。只按端点（含别名）+ 库表 + 列结构匹配。"""

    __tablename__ = "identity_link"
    id: Mapped[str] = pk()
    source_asset_id: Mapped[str] = fk("asset.id")
    target_asset_id: Mapped[str | None] = fk("asset.id", nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False)  # confirmed | candidate | mismatch | unresolved
    method: Mapped[str] = mapped_column(String(64), nullable=False)
    evidence: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)
    column_match: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)
    created_at: Mapped[datetime] = created()
    superseded_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class ColumnFingerprint(Base):
    """字段推导指纹：上游字段身份 + 表达式 + 影响条件。人工确认与指标版本都绑定它。"""

    __tablename__ = "column_fingerprint"
    __table_args__ = (Index("ix_fp_current", "asset_id", "column_name", "superseded_at"),)
    id: Mapped[str] = pk()
    asset_id: Mapped[str] = mapped_column(IdType, nullable=False)
    column_name: Mapped[str] = mapped_column(String(255), nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)  # ok | broken | unknown
    reason: Mapped[str | None] = mapped_column(Text)
    detail: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)
    run_id: Mapped[str | None] = mapped_column(IdType)
    computed_at: Mapped[datetime] = created()
    superseded_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class Grain(Base):
    """粒度：observed 数据库观察、parsed 解析、declared 人工声明。
    声明的粒度绑定声明时的键列指纹，键列指纹变化即失效。"""

    __tablename__ = "grain"
    id: Mapped[str] = pk()
    asset_id: Mapped[str] = fk("asset.id")
    asset_version_id: Mapped[str] = mapped_column(IdType, nullable=False)
    columns: Mapped[list] = mapped_column(JsonType, nullable=False)
    origin: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="unverified", nullable=False)
    column_fingerprints: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)
    verified_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    verify_detail: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)
    declared_by: Mapped[str | None] = mapped_column(IdType)
    reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = created()
    superseded_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


# --- 治理层 -----------------------------------------------------------------------

class Confirmation(Base):
    """人工确认的字段业务含义，绑定字段推导指纹；指纹变化后不再生效。"""

    __tablename__ = "confirmation"
    id: Mapped[str] = pk()
    asset_id: Mapped[str] = fk("asset.id")
    column_name: Mapped[str] = mapped_column(String(255), nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    meaning: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)
    confirmed_by: Mapped[str] = fk("app_user.id", index=False)
    created_at: Mapped[datetime] = created()
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class Metric(Base):
    __tablename__ = "metric"
    id: Mapped[str] = pk()
    code: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    aliases: Mapped[list] = mapped_column(JsonType, default=list, nullable=False)
    domain_id: Mapped[str] = fk("domain.id")
    owner_id: Mapped[str] = fk("app_user.id")
    created_by: Mapped[str] = fk("app_user.id", index=False)
    created_at: Mapped[datetime] = created()


class MetricVersion(Base):
    """指标版本。状态：draft 草稿 → pending_review 待复核 → published 已发布 → affected 受影响
    → retired 失效；published → archived 归档；pending_review → rejected 驳回。
    dependency 记录提交时每个依赖字段的推导指纹；指纹不一致就不能按此版本问数。"""

    __tablename__ = "metric_version"
    __table_args__ = (UniqueConstraint("metric_id", "version_no"),)
    id: Mapped[str] = pk()
    metric_id: Mapped[str] = fk("metric.id")
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    base_asset_id: Mapped[str] = fk("asset.id", index=False)
    expression: Mapped[str] = mapped_column(Text, nullable=False)
    time_column: Mapped[str] = mapped_column(String(255), nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    filters: Mapped[str | None] = mapped_column(Text)
    dimensions: Mapped[list] = mapped_column(JsonType, default=list, nullable=False)
    joins: Mapped[list] = mapped_column(JsonType, default=list, nullable=False)
    grain: Mapped[list] = mapped_column(JsonType, default=list, nullable=False)
    dedupe_basis: Mapped[str | None] = mapped_column(Text)
    dependency: Mapped[list] = mapped_column(JsonType, default=list, nullable=False)
    dependency_fingerprint: Mapped[str | None] = mapped_column(String(64))
    asset_versions: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)
    publish_check: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)
    status_reason: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[str] = fk("app_user.id", index=False)
    created_at: Mapped[datetime] = created()
    submitted_by: Mapped[str | None] = mapped_column(IdType)
    submitted_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    reviewed_by: Mapped[str | None] = mapped_column(IdType)
    reviewed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    review_note: Mapped[str | None] = mapped_column(Text)
    published_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    retired_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class MetricVersionTransition(Base):
    """状态转换历史（只追加）。actor_id 为空表示系统（采集后的影响分析）。"""

    __tablename__ = "metric_version_transition"
    id: Mapped[str] = pk()
    metric_version_id: Mapped[str] = fk("metric_version.id")
    from_status: Mapped[str | None] = mapped_column(String(16))
    to_status: Mapped[str] = mapped_column(String(16), nullable=False)
    actor_id: Mapped[str | None] = mapped_column(IdType)
    reason: Mapped[str | None] = mapped_column(Text)
    at: Mapped[datetime] = created()


class JoinPath(Base):
    """批准的关联路径。基数由粒度推出；跨域路径要两个域的负责人都批准。"""

    __tablename__ = "join_path"
    id: Mapped[str] = pk()
    code: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    left_asset_id: Mapped[str] = fk("asset.id", index=False)
    left_columns: Mapped[list] = mapped_column(JsonType, nullable=False)
    right_asset_id: Mapped[str] = fk("asset.id", index=False)
    right_columns: Mapped[list] = mapped_column(JsonType, nullable=False)
    cardinality: Mapped[str] = mapped_column(String(8), nullable=False)  # n1 | 1n | 11 | nn | unknown
    cardinality_basis: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)  # pending | approved | rejected | affected
    dependency: Mapped[list] = mapped_column(JsonType, default=list, nullable=False)
    created_by: Mapped[str] = fk("app_user.id", index=False)
    created_at: Mapped[datetime] = created()


class JoinPathApproval(Base):
    __tablename__ = "join_path_approval"
    __table_args__ = (UniqueConstraint("join_path_id", "domain_id"),)
    id: Mapped[str] = pk()
    join_path_id: Mapped[str] = fk("join_path.id", index=False)
    domain_id: Mapped[str] = fk("domain.id", index=False)
    user_id: Mapped[str] = fk("app_user.id", index=False)
    decision: Mapped[str] = mapped_column(String(16), nullable=False)
    note: Mapped[str | None] = mapped_column(Text)
    at: Mapped[datetime] = created()


class ChangeSet(Base):
    """一次采集发现的全部变化。window_start / window_end 是可确认的生效区间。"""

    __tablename__ = "change_set"
    id: Mapped[str] = pk()
    run_id: Mapped[str] = fk("collection_run.id")
    datasource_id: Mapped[str] = fk("datasource.id", index=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)  # open | resolved
    summary: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)
    window_start: Mapped[datetime | None] = mapped_column(UTCDateTime)
    window_end: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    created_at: Mapped[datetime] = created()
    resolved_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class ChangeEvent(Base):
    __tablename__ = "change_event"
    id: Mapped[str] = pk()
    change_set_id: Mapped[str] = fk("change_set.id")
    asset_id: Mapped[str | None] = mapped_column(IdType)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    column_name: Mapped[str | None] = mapped_column(String(255))
    before: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)
    after: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)
    created_at: Mapped[datetime] = created()


class Impact(Base):
    __tablename__ = "impact"
    id: Mapped[str] = pk()
    change_set_id: Mapped[str] = fk("change_set.id")
    object_type: Mapped[str] = mapped_column(String(32), nullable=False)
    object_id: Mapped[str] = mapped_column(IdType, nullable=False)
    level: Mapped[str] = mapped_column(String(24), nullable=False)  # affected | possibly_affected
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    path: Mapped[list] = mapped_column(JsonType, default=list, nullable=False)
    created_at: Mapped[datetime] = created()


class ReviewTask(Base):
    """复核任务。resolution：restored 已恢复原定义、new_version 发布新口径、retired 停用、
    rebound 确认含义不变并以新版本承接。"""

    __tablename__ = "review_task"
    id: Mapped[str] = pk()
    change_set_id: Mapped[str] = fk("change_set.id")
    object_type: Mapped[str] = mapped_column(String(32), nullable=False)
    object_id: Mapped[str] = mapped_column(IdType, nullable=False)
    assignee_id: Mapped[str] = fk("app_user.id")
    status: Mapped[str] = mapped_column(String(16), nullable=False)  # open | resolved
    resolution: Mapped[str | None] = mapped_column(String(16))
    linked_metric_version_id: Mapped[str | None] = mapped_column(IdType)
    note: Mapped[str | None] = mapped_column(Text)
    resolved_by: Mapped[str | None] = mapped_column(IdType)
    resolved_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    created_at: Mapped[datetime] = created()


# --- 运行与审计 -------------------------------------------------------------------

class Job(Base):
    """后台任务。事实来源是这张表；Redis 只用于唤醒 worker。"""

    __tablename__ = "job"
    __table_args__ = (Index("ix_job_status", "status", "created_at"),)
    id: Mapped[str] = pk()
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    payload: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)  # queued | running | succeeded | failed
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    result: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)
    error: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[str | None] = mapped_column(IdType)
    created_at: Mapped[datetime] = created()
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class AuditEvent(Base):
    """审计（只追加）。记录真实操作者。"""

    __tablename__ = "audit_event"
    __table_args__ = (Index("ix_audit_object", "object_type", "object_id"),)
    id: Mapped[str] = pk()
    at: Mapped[datetime] = created()
    actor_id: Mapped[str | None] = mapped_column(IdType)
    actor_name: Mapped[str] = mapped_column(String(64), nullable=False)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    object_type: Mapped[str] = mapped_column(String(32), nullable=False)
    object_id: Mapped[str | None] = mapped_column(IdType)
    detail: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)


class Answer(Base):
    """一次问数的完整依据：问题、意图、计划、SQL、指标版本、资产版本、执行结果。
    可追溯 ≠ 可复现：不保存数据快照。"""

    __tablename__ = "answer"
    __table_args__ = (Index("ix_answer_user", "asked_by", "asked_at"),)
    id: Mapped[str] = pk()
    question: Mapped[str] = mapped_column(Text, nullable=False)
    asked_by: Mapped[str] = fk("app_user.id", index=False)
    asked_at: Mapped[datetime] = created()
    status: Mapped[str] = mapped_column(String(16), nullable=False)  # answered | blocked | clarify | denied | error
    model_adapter: Mapped[str] = mapped_column(String(64), nullable=False)
    intent: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)
    plan: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)
    sql: Mapped[str | None] = mapped_column(LongText)
    metric_version_ids: Mapped[list] = mapped_column(JsonType, default=list, nullable=False)
    evidence: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)
    result: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)
    message: Mapped[str | None] = mapped_column(Text)
    error_code: Mapped[str | None] = mapped_column(String(32))
    freshness: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)
    executed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    cache_hit: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    recheck_status: Mapped[str | None] = mapped_column(String(32))


class AppSetting(Base):
    __tablename__ = "app_setting"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict] = mapped_column(JsonType, nullable=False)
    updated_by: Mapped[str | None] = mapped_column(IdType)
    updated_at: Mapped[datetime] = created()
