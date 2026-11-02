"""接口响应模型：给前端生成类型用。关键字段带类型，其余字段允许扩展（extra="allow"），
视图函数仍返回 dict，由 FastAPI 按这些模型校验与序列化。"""

from __future__ import annotations

from datetime import date
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from avelis.i18n import gettext

Json = dict[str, Any]


class Out(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)


# --- 系统设置 ---------------------------------------------------------------------

BusinessCalendarWeekday = Annotated[int, Field(strict=True, ge=0, le=6)]
BusinessCalendarDate = Annotated[
    str, Field(strict=True, pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$", json_schema_extra={"format": "date"})
]


class BusinessCalendarIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    weekend_days: list[BusinessCalendarWeekday] = Field(default_factory=lambda: [5, 6], max_length=2000)
    holidays: list[BusinessCalendarDate] = Field(default_factory=list, max_length=2000)
    workdays: list[BusinessCalendarDate] = Field(default_factory=list, max_length=2000)

    @field_validator("weekend_days")
    @classmethod
    def normalize_weekdays(cls, values: list[int]) -> list[int]:
        return sorted(set(values))

    @field_validator("holidays", "workdays")
    @classmethod
    def normalize_dates(cls, values: list[str]) -> list[str]:
        for value in values:
            try:
                date.fromisoformat(value)
            except ValueError:
                raise ValueError(gettext("日期必须是合法的 YYYY-MM-DD")) from None
        return sorted(set(values))

    @model_validator(mode="after")
    def disjoint_dates(self) -> "BusinessCalendarIn":
        if set(self.holidays) & set(self.workdays):
            raise ValueError(gettext("同一天不能既是节假日又是调休上班日"))
        return self


class BusinessCalendarOut(BusinessCalendarIn):
    configured: bool


class WorkerHealthOut(Out):
    alive: bool
    last_seen: str | None = None


# --- 数据源 -----------------------------------------------------------------------


class RunOut(Out):
    id: str
    status: str
    trigger: str
    started_at: str
    finished_at: str | None = None
    stats: Json = Field(default_factory=dict)
    error: str | None = None
    snapshot_hash: str | None = None


class CollectOut(Out):
    id: str | None = None
    job_id: str | None = None
    status: str
    stats: Json | None = None
    error: str | None = None


class CredentialOut(Out):
    purpose: str
    username: str
    password: str
    updated_at: str


class AliasOut(Out):
    id: str
    host: str
    port: int
    note: str | None = None


class EngineOptionOut(Out):
    key: str
    label: str
    type: str
    required: bool
    hint: str
    choices: list[str] = []
    default: str = ""


class CredentialFreeOut(Out):
    option: str
    values: list[str]


class EngineOut(Out):
    kind: str
    label: str
    dialect: str
    default_catalog: str
    name_levels: list[str]
    capabilities: list[str]
    default_port: int | None = None
    is_test_adapter: bool
    note: str
    options: list[EngineOptionOut] = []
    credential_free: CredentialFreeOut | None = None


class DatasourceOut(Out):
    id: str
    name: str
    kind: str
    host: str
    port: int
    options: Json
    is_test_adapter: bool
    source_label: str
    status: str
    server_identity: Json
    collect_interval_minutes: int
    allow_exploration: bool = True
    exploration_supported: bool = False
    credentials: list[CredentialOut]
    last_run: RunOut | None = None
    aliases: list[AliasOut]
    engine: EngineOut


class JobOut(Out):
    id: str
    kind: str
    status: str
    payload: Json
    result: Json
    error: str | None = None
    created_at: str
    finished_at: str | None = None


# --- 目录、血缘 --------------------------------------------------------------------


class MemberOut(Out):
    user_id: str
    role: str
    username: str
    display_name: str


class ExampleQuestion(Out):
    text: str
    mode: str = "both"


class DomainOut(Out):
    id: str
    code: str
    name: str
    description: str | None = None
    owner_id: str
    members: list[MemberOut]
    example_questions: list[ExampleQuestion] = []


class ExampleQuestionOut(Out):
    text: str
    domain: str | None = None
    source: str


class ExampleQuestionsOut(Out):
    questions: list[ExampleQuestionOut]


class DatasourceRef(Out):
    id: str
    name: str
    kind: str
    is_test_adapter: bool


class GrainOut(Out):
    columns: list[str]
    origin: str
    status: str
    grain_id: str | None = None
    note: str | None = None


class AssetOut(Out):
    id: str
    qualified: str
    kind: str
    status: str
    domain_id: str | None = None
    datasource: DatasourceRef
    physical_id: str | None = None
    current_version_id: str | None = None
    last_seen_at: str
    deleted_at: str | None = None


class ConfirmationOut(Out):
    id: str
    meaning: str
    confirmed_by: str
    created_at: str
    valid: bool
    note: str | None = None


class ColumnDocOut(Out):
    """字段含义的数据库注释状态（governance/column_docs.py）。"""

    asset_id: str
    asset: str
    column: str
    comment: str | None = None
    status: Literal["ok", "linked", "inherited", "missing"]
    source: str | None = None


class CommentGapOut(ColumnDocOut):
    kind: str
    datasource_id: str
    suggestion: str | None = None


class CommentCoverageOut(Out):
    datasource_id: str
    name: str | None = None
    ok: int
    linked: int
    inherited: int
    missing: int
    total: int
    gaps: int
    completeness: float | None = None


class CommentGapsOut(Out):
    datasources: list[CommentCoverageOut]
    items: list[CommentGapOut]


class ColumnOut(Out):
    name: str
    data_type: str
    nullable: bool
    comment: str | None = None
    is_key: bool
    column_id: str
    fingerprint: str | None = None
    fingerprint_status: str | None = None
    fingerprint_reason: str | None = None
    doc: ColumnDocOut | None = None
    confirmations: list[ConfirmationOut]


class AssetVersionOut(Out):
    id: str
    version_no: int
    observed_at: str
    last_seen_at: str
    superseded_at: str | None = None
    name: str
    kind: str
    table_model: str | None = None
    key_columns: list[str]
    unique_keys: list[list[str]]
    comment: str | None = None
    properties: Json
    content_hash: str


class LinkTarget(Out):
    id: str
    qualified: str
    datasource_id: str


class LinkOut(Out):
    id: str
    status: str
    method: str
    evidence: Json
    column_match: Json
    source: str
    target: LinkTarget | None = None
    created_at: str


class DefinitionOut(Out):
    raw_sql: str
    canonical_sql: str | None = None
    canonical_hash: str


class ParseOut(Out):
    status: str
    reason: str | None = None
    parser_version: str
    details: Json


class AssetDetailOut(AssetOut):
    version: AssetVersionOut | None = None
    columns: list[ColumnOut]
    definition: DefinitionOut | None = None
    parse: ParseOut | None = None
    grains: list[GrainOut]
    identity_link: LinkOut | None = None
    linked_from: list[LinkOut]
    versions: list[AssetVersionOut]


class GraphNodeOut(Out):
    id: str
    qualified: str
    kind: str
    status: str
    grains: list[GrainOut]
    in_scope: bool
    domain_id: str | None = None
    datasource: DatasourceRef | None = None
    note: str | None = None


class ColumnEdgeOut(Out):
    source_column: str | None = None
    target_column: str | None = None
    relation: str
    expression: str | None = None
    origin: str
    attributes: Json


class GraphEdgeOut(Out):
    source: str
    source_ref: str
    target: str
    columns: list[ColumnEdgeOut]
    relations: list[str]
    origins: list[str]


class GraphJoinOut(Out):
    id: str
    code: str
    left_asset_id: str
    left_columns: list[str]
    right_asset_id: str
    right_columns: list[str]
    cardinality: str
    status: Literal["approved", "pending"]


class GraphOut(Out):
    nodes: list[GraphNodeOut]
    edges: list[GraphEdgeOut]
    identity_links: list[LinkOut]
    joins: list[GraphJoinOut] = Field(default_factory=list)


# --- 口径、变更 --------------------------------------------------------------------


class OwnerRef(Out):
    id: str
    display_name: str


class VersionBrief(Out):
    id: str
    version_no: int
    status: str
    status_reason: str | None = None
    unit: str | None = None
    published_at: str | None = None


class MetricAIContextOut(BaseModel):
    instructions: str | None
    synonyms: list[str]
    examples: list[str]


class MetricOut(Out):
    id: str
    code: str
    name: str
    aliases: list[str]
    ai_context: MetricAIContextOut
    domain_id: str
    owner: OwnerRef
    versions: list[VersionBrief]


class TransitionOut(Out):
    from_: str | None = Field(default=None, alias="from")
    to: str
    actor_id: str | None = None
    reason: str | None = None
    at: str


class ImpactOut(Out):
    id: str
    change_set_id: str
    object_type: str
    object_id: str
    level: str
    reason: str
    path: list[Any]
    created_at: str


class MetricVersionOut(Out):
    id: str
    kind: str = "base"
    components: list[Json] = []
    metric_id: str
    metric_code: str
    metric_name: str
    version_no: int
    status: str
    status_reason: str | None = None
    description: str
    expression: str
    base_asset: Json | None = None
    time_column: str
    timezone: str
    time_format: Literal["yyyymmdd", "yyyy-mm-dd", "yyyymm"] | None = None
    data_grain: Literal["day", "week", "month", "quarter", "year"] = "day"
    time_aggregation: Literal["sum", "last", "avg"] = "sum"
    value_kind: Literal["level", "ratio", "count"] | None = None
    polarity: Literal["up_good", "down_good"] | None = None
    filters: str | None = None
    dimensions: list[Json]
    joins: list[Json]
    grain: list[str]
    dedupe_basis: str | None = None
    unit: str | None = None
    unit_scale: int = 1
    material_change_ratio: float | None = None
    dependency: list[Json]
    dependency_fingerprint: str | None = None
    asset_versions: Json
    publish_check: Json
    created_by: str
    submitted_by: str | None = None
    reviewed_by: str | None = None
    review_note: str | None = None
    published_at: str | None = None
    created_at: str | None = None
    owner_id: str
    domain_id: str
    transitions: list[TransitionOut] = Field(default_factory=list)
    impacts: list[ImpactOut] = Field(default_factory=list)
    drift: list[Json] = Field(default_factory=list)


class PublishCheckOut(Out):
    ok: bool
    blockers: list[str]
    warnings: list[str]
    joins: list[Json]
    base_grains: list[Json]
    dependency: list[Json]
    checked_at: str


class JoinPathOut(Out):
    id: str
    code: str
    name: str
    status: str
    left: Json
    right: Json
    cardinality: str
    cardinality_basis: Json
    required_domains: list[str]
    approvals: list[Json]
    source: Json | None = None
    created_by: str | None = None


class TaskOut(Out):
    id: str
    change_set_id: str
    target: Json
    assignee_id: str
    status: str
    resolution: str | None = None
    note: str | None = None
    resolved_by: str | None = None
    linked_metric_version_id: str | None = None
    created_at: str
    resolved_at: str | None = None


class ChangeEventOut(Out):
    id: str
    kind: str
    asset_id: str | None = None
    asset: str | None = None
    column: str | None = None
    before: Json
    after: Json
    actor_db_user: str | None = None
    attribution_status: str | None = None
    attribution: Json = Field(default_factory=dict)


class ChangeSetOut(Out):
    id: str
    run_id: str
    datasource_id: str
    status: str
    summary: Json
    created_at: str
    effective_window: Json
    events: list[ChangeEventOut] | None = None
    impacts: list[ImpactOut] | None = None
    tasks: list[TaskOut] | None = None


class ChangeRecordOut(ChangeEventOut):
    change_set_id: str
    datasource_id: str
    datasource: str
    detected_at: str
    window: Json
    chain_seq: int | None = None
    row_hash: str | None = None


class ChangeEventPage(Out):
    total: int
    limit: int
    offset: int
    items: list[ChangeRecordOut]
    actors: list[str]


class ChainIssueOut(Out):
    kind: str
    seq: int | None = None
    table: str
    row_id: str | None = None
    detail: str


class HistoryVerifyOut(Out):
    ok: bool
    verified_at: str
    checked_rows: int
    tables: dict[str, int]
    head: Json | None = None
    first_break: ChainIssueOut | None = None
    issue_count: int
    issues: list[ChainIssueOut]
    anchors: int


class HistoryStatusOut(Out):
    retention_days: int
    purge_interval_hours: int
    export_max_rows: int
    chain_head: Json | None = None
    last_purge: Json | None = None
    anchors: list[Json]


# --- 问数、审计 --------------------------------------------------------------------


class AnswerOut(Out):
    id: str
    question: str
    asked_by: str
    asked_at: str
    status: str
    message: str | None = None
    error_code: str | None = None
    model_adapter: str
    intent: Json
    plan: Json
    sql: str | None = None
    metric_version_ids: list[str]
    result: Json
    evidence: Json
    freshness: Json
    executed_at: str | None = None
    duration_ms: int | None = None
    cache_hit: bool
    recheck_status: str | None = None
    thread_id: str | None = None
    steps: list[Json] = Field(default_factory=list)
    explanation: str | None = None
    explanation_source: str | None = None


class ThreadOut(Out):
    id: str
    title: str
    status: str
    created_at: str | None = None
    updated_at: str | None = None


class ThreadDetailOut(Out):
    id: str
    title: str
    status: str
    answers: list[AnswerOut]


class AuditOut(Out):
    id: str
    at: str
    actor_id: str | None = None
    actor: str
    action: str
    object_type: str
    object_id: str | None = None
    detail: Json


# --- 导入血缘 ---------------------------------------------------------------------


class StatementOut(Out):
    id: str
    file_id: str | None = None
    seq: int
    lines: list[int]
    method: str
    statement_type: str | None = None
    target: str | None = None
    status: str
    reason: str | None = None
    placeholders: list[str]
    raw_text: str
    sql_text: str | None = None
    text_hash: str | None = None
    writes: list[Json] = Field(default_factory=list)


class CandidateOut(Out):
    id: str
    file_id: str | None = None
    statement_id: str | None = None
    origin: str
    status: str
    source: str
    target: str
    source_ref: str
    source_column: str | None = None
    target_ref: str
    target_column: str | None = None
    relation: str
    expression: str | None = None
    confidence: float | None = None
    evidence: Json
    lineage_edge_id: str | None = None
    modified: Json
    reason: str | None = None
    decided_by: str | None = None
    attributes: Json = Field(default_factory=dict)
    via: list[str] = Field(default_factory=list)
    hops: list[Json] = Field(default_factory=list)


class ImportJobOut(Out):
    id: str
    filename: str
    language: str
    source_type: str | None = None
    datasource_id: str | None = None
    default_db: str | None = None
    status: str
    params: Json = Field(default_factory=dict)
    stats: Json
    model_adapter: str | None = None
    model_payload: list[Json] = Field(default_factory=list)
    content_hash: str | None = None
    created_at: str
    statements: list[StatementOut] = Field(default_factory=list)
    candidates: list[CandidateOut] = Field(default_factory=list)
    duplicate: bool | None = None
    content: str | None = None


class ImportFileOut(Out):
    id: str
    filename: str
    source_key: str
    revision: int
    supersedes: str | None = None
    superseded_by: str | None = None
    language: str
    status: str
    statement_count: int = 0
    stats: Json = Field(default_factory=dict)


class ImportBatchBrief(Out):
    id: str
    status: str
    file_count: int
    statement_count: int
    progress: Json
    created_at: str
    filename: str | None = None


class ImportBatchOut(Out):
    id: str
    datasource_id: str
    default_db: str | None = None
    status: str
    progress: Json = Field(default_factory=dict)
    stats: Json = Field(default_factory=dict)
    options: Json = Field(default_factory=dict)
    params: Json = Field(default_factory=dict)
    files: list[ImportFileOut] = Field(default_factory=list)
    unmatched_tables: list[Json] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    job_id: str | None = None
    created_at: str
    updated_at: str | None = None
    error: str | None = None
    duplicate: bool | None = None


class ImportStatementPage(Out):
    items: list[StatementOut]
    total: int
    counts: dict[str, int]
    offset: int
    limit: int


class ImportCandidatePage(Out):
    items: list[CandidateOut]
    total: int
    offset: int
    limit: int


class ImportContentOut(Out):
    file_id: str
    filename: str
    content: str


class ImportSqlFunctionsOut(Out):
    datasource_id: str
    sql_functions: list[str]


class ImportDecisionsOut(Out):
    ids: list[str]
    decision: str
    processed: int


# --- 工作台 -----------------------------------------------------------------------


class StageOut(Out):
    key: str
    label: str
    value: str
    unit: str
    status: str
    note: str


class TodoOut(Out):
    kind: str
    status: str
    title: str
    meta: str
    ref: str
    link: str
    at: str


class WorkbenchOut(Out):
    stages: list[StageOut]
    todos: list[TodoOut]
    my_metrics: list[Json]
    blocked_answers: list[AnswerOut]
    datasources: list[Json]
    cadence: list[Json]
    recent_changes: list[Json]


# --- 分析智能体（M8） ---------------------------------------------------------------


class AnalysisProposalItem(Out):
    id: str
    kind: Literal["asset", "join", "hypothesis"]
    content: str
    source: Literal["upstream", "experience", "inferred", "user"]
    refs: list[Json] = Field(default_factory=list)
    experience_id: str | None = None
    experience_version: int | None = None
    join: Json | None = None


class AnalysisProposalOut(Out):
    items: list[AnalysisProposalItem] = Field(default_factory=list)


class AnalysisDecisionIn(BaseModel):
    item_id: str = Field(min_length=1, max_length=64)
    decision: Literal["confirmed", "rejected"]


class AnalysisResumeIn(BaseModel):
    answer: str = Field(default="", max_length=2000)
    decisions: list[AnalysisDecisionIn] = Field(default_factory=list, max_length=100)
    supplement: str = Field(default="", max_length=4000)


class AnalysisModelIn(BaseModel):
    provider_id: str
    model_id: str


class AnalysisModelChoice(AnalysisModelIn):
    reasoning_effort: str


class AnalysisEffortOut(BaseModel):
    value: str
    label: str


class AnalysisModelOut(AnalysisModelIn):
    provider_name: str
    efforts: list[AnalysisEffortOut]
    context_window: int | None = None
    context_window_source: Literal["configured", "default"] | None = None


class AnalysisModelsOut(BaseModel):
    models: list[AnalysisModelOut]
    default: AnalysisModelChoice | None = None


class AnalysisFollowupIn(BaseModel):
    text: str = Field(min_length=1, max_length=4000, pattern=r"\S")
    confirmation_mode: Literal["auto", "always"] = "auto"
    model: AnalysisModelIn | None = None
    reasoning_effort: str | None = None


class AnalysisMessageIn(BaseModel):
    text: str = Field(min_length=1, max_length=2000, pattern=r"\S")


class AnalysisMessageOut(Out):
    id: str
    text: str
    created_at: str
    consumed_at: str | None = None


class ExperienceRef(BaseModel):
    object_type: Literal["asset", "metric", "metric_version"]
    object_id: str = Field(min_length=1, max_length=64)
    version: int | None = Field(default=None, ge=1, strict=True)


class ExperienceSource(BaseModel):
    run_id: str = Field(min_length=1, max_length=64)
    checkpoint: str = Field(min_length=1, max_length=80, pattern=r"\S")
    user_text: str = Field(default="", max_length=4000)


class ExperienceCandidate(BaseModel):
    kind: Literal["join", "driver", "alias", "rule", "preference", "negative"]
    content: str = Field(min_length=1, max_length=4000, pattern=r"\S")
    refs: list[ExperienceRef] = Field(default_factory=list)
    source: ExperienceSource


class ExperienceRevokeIn(BaseModel):
    reason: str = Field(default="", max_length=1000)


class ExperienceOut(Out):
    id: str
    owner_id: str
    scope: Literal["personal"]
    current_version: int
    status: Literal["active", "revoked", "superseded"]
    created_at: str
    version_id: str
    version: int
    kind: Literal["join", "driver", "alias", "rule", "preference", "negative"]
    content: str
    refs: list[ExperienceRef]
    source: ExperienceSource
    confirmed_by: str
    confirmed_at: str
    superseded_by: str | None = None


class AnalysisStepOut(Out):
    seq: int
    kind: str
    tool: str | None = None
    status: str
    reason: str | None = None
    dataset: str | None = None
    args: Json = Field(default_factory=dict)
    output: Json = Field(default_factory=dict)


class ColumnRole(Out):
    role: Literal["time", "dimension", "measure", "change", "rate"]
    label: str


class AnalysisDatasetOut(Out):
    ref: str
    seq: int
    kind: str
    title: str
    columns: list[str]
    rows: list[list[Any]]
    row_count: int
    truncated: bool = False
    restricted: bool = False
    inputs: list[str] = Field(default_factory=list)
    sql: str | None = None
    summary: Any = None
    metric_version_ids: list[str] = Field(default_factory=list)
    time_column: str | None = None  # 按时间分组时的时间段列（据此画趋势图）
    units: Json = Field(default_factory=dict)
    polarity: dict[str, Literal["up_good", "down_good"]] = Field(default_factory=dict)
    column_roles: dict[str, ColumnRole] = Field(default_factory=dict)


class AnalysisDatasetLegacyOut(Out):
    """数字序号 GET 的原有完整数据集结构。"""

    ref: str
    kind: str
    title: str
    columns: list[str]
    rows: list[list[Any]]
    truncated: bool
    meta: Json
    inputs: list[str]
    answer_id: str | None
    column_roles: dict[str, ColumnRole] = Field(default_factory=dict)


class AnalysisDatasetSource(Out):
    run_id: str
    ref: str


class AnalysisDatasetDetailOut(Out):
    ref: str
    kind: str
    title: str
    columns: list[str]
    row_count: int
    truncated: bool
    inputs: list[str]
    sql: str | None
    metric_version_ids: list[str]
    metric_codes: list[str]
    window: Json
    time_column: str | None
    units: Json
    polarity: dict[str, Literal["up_good", "down_good"]] = Field(default_factory=dict)
    column_roles: dict[str, ColumnRole] = Field(default_factory=dict)
    summary_units: Json
    purpose: str | None
    answer_id: str | None
    created_at: str
    explore: bool
    assumption: bool
    partial: bool
    rerunnable: bool
    source: AnalysisDatasetSource | None
    meta: Json
    offset: int
    limit: int
    rows: list[list[Any]]
    total: int = Field(description="已保存的行数；truncated=true 时不代表来源数据全集")


class AnalysisDatasetRerunWindow(BaseModel):
    start: date
    end: date
    compare_start: date | None = None
    compare_end: date | None = None

    @model_validator(mode="after")
    def valid_range(self) -> "AnalysisDatasetRerunWindow":
        if self.start > self.end:
            raise ValueError(gettext("时间窗口的开始日期晚于结束日期"))
        if bool(self.compare_start) != bool(self.compare_end):
            raise ValueError(gettext("对比窗口需要同时提供 compare_start 与 compare_end"))
        if self.compare_start and self.compare_end and self.compare_start > self.compare_end:
            raise ValueError(gettext("对比窗口的开始日期晚于结束日期"))
        return self


class AnalysisDatasetRerunIn(BaseModel):
    window: AnalysisDatasetRerunWindow


class AnalysisRunOut(Out):
    id: str
    conversation_id: str
    turn: int
    question: str
    status: str
    trigger: str
    title: str | None = None
    verified: bool | None = None
    window: Json = Field(default_factory=dict)
    budget: Json = Field(default_factory=dict)
    model_adapter: str | None = None
    model_choice: AnalysisModelChoice | None = None
    reasoning_effort: str | None = None
    error: str | None = None
    created_at: str
    finished_at: str | None = None
    task_id: str | None = None
    plan: Json | None = None
    report: Json | None = None
    answer: Json | None = None
    verification: Json | None = None
    steps: list[AnalysisStepOut] | None = None
    datasets: list[AnalysisDatasetOut] | None = None
    messages: list[AnalysisMessageOut] = Field(default_factory=list)
    parent_run_id: str | None = None
    confirmation_mode: Literal["auto", "always"] = "auto"
    proposal: AnalysisProposalOut | None = None
    pending: Json | None = None
    experience_candidates: list[ExperienceCandidate] = Field(default_factory=list)
    join_candidates: list[Json] = Field(default_factory=list)
    checkpoint_warnings: list[str] = Field(default_factory=list)
    confirmed_scope: Json = Field(default_factory=dict)
    confirmed_items: list[AnalysisProposalItem] = Field(default_factory=list)


class AnalysisConversationOut(Out):
    id: str
    title: str
    display_title: str | None = None
    question: str
    latest_run_id: str
    status: str
    turn_count: int
    archived_at: str | None = None
    created_at: str
    updated_at: str
    started_at: str | None = None
    finished_at: str | None = None


class AnalysisConversationDetailOut(AnalysisConversationOut):
    runs: list[AnalysisRunOut]


class CronPreviewOut(Out):
    expr: str
    timezone: str
    runs: list[str]
    error: str | None = None


class AnalysisTaskOut(Out):
    id: str
    title: str
    question: str
    status: str
    schedule_cron: str | None = None
    paused_reason: str | None = None
    next_run_at: str | None = None
    last_run_id: str | None = None
    window: Json = Field(default_factory=dict)


class NotificationOut(Out):
    id: str
    kind: str
    title: str
    body: str | None = None
    link: str | None = None
    read_at: str | None = None
    created_at: str
