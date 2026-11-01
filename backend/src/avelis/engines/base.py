"""引擎适配器：一种数据库在 Avelis 中的全部差异集中在这里。

借鉴 DataHub（按平台注册 source、平台 → SQL 方言映射）、OpenMetadata（service type + 连接配置 + 能力）、
OpenLineage（数据集 = 命名空间 + 层级名）的做法：采集、解析、问数、治理只通过 Engine 访问数据库差异，
不按 kind 分支。新增数据库 = 新增一个适配器模块并在注册表登记，核心模块不改（见 ADR 0004）。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Any

from avelis.connectors.base import Connector, QueryConnector
from avelis.lineage.sql import SqlDialect

# 能力：适配器只声明在真实环境联调过的能力，未声明的能力由核心模块明确报“不支持”，不做降级猜测。
METADATA = "metadata"  # 采集表、视图、列、键
VIEW_LINEAGE = "view_lineage"  # 解析视图定义得到列级血缘
QUERY = "query"  # 用只读账号执行已校验的 SELECT（问数、粒度核验、维度取值）
EXTERNAL_TABLES = "external_tables"  # 外部表 / Catalog 表，可追到另一个数据源的真实表
IDENTITY_TARGET = "identity_target"  # 能作为外部表的追溯目标（有实例身份，如 server_uuid）
LIVE_CHECK = "live_check"  # 执行前用只读账号核对来源对象的列与视图定义是否与上次采集一致
DDL_AUDIT = "ddl_audit"  # 采集账号可读数据库的 DDL 审计来源，变更可归因到数据库用户（ADR 0008）
CAPABILITIES = (METADATA, VIEW_LINEAGE, QUERY, EXTERNAL_TABLES, IDENTITY_TARGET, LIVE_CHECK, DDL_AUDIT)

CollectorFactory = Callable[[str, int, str, str, dict[str, Any]], Connector]
QuerierFactory = Callable[[str, int, str, str, dict[str, Any]], QueryConnector]


@dataclass(frozen=True)
class OptionField:
    """数据源连接配置中的一项（前端按它渲染表单，不按数据库类型写死）。"""
    key: str
    label: str
    type: str = "list"  # list：逗号分隔的名称列表；text：单个名称
    required: bool = False
    hint: str = ""


@dataclass(frozen=True)
class Engine:
    kind: str
    label: str
    sql: SqlDialect
    # 资产名的层级（OpenLineage 的 name 部分）。资产统一存为 catalog.db.name 三段：
    # 只有 库.表 两级的引擎用固定 Catalog（sql.default_catalog）补齐；库.模式.表 三级的引擎把库放在 catalog 段。
    name_levels: tuple[str, ...]
    capabilities: frozenset[str]
    collector: CollectorFactory | None
    querier: QuerierFactory | None = None
    default_port: int | None = None
    is_test_adapter: bool = False
    needs_credentials: bool = True
    note: str = ""
    options: tuple[OptionField, ...] = field(default_factory=tuple)
    # catalog 段取自数据源配置中的哪一项（如 PostgreSQL 的库名）；为空时用方言的固定 default_catalog
    catalog_option: str | None = None
    # 命令行客户端的脚本分隔符，不属于 SQL 方言语法。
    script_separators: tuple[str, ...] = field(default_factory=tuple)

    def supports(self, capability: str) -> bool:
        return capability in self.capabilities

    def dialect_for(self, options: dict[str, Any] | None) -> SqlDialect:
        """某个数据源的解析方言：catalog 段因数据源而异的引擎（库名作 catalog）在这里补齐。"""
        if self.catalog_option and (options or {}).get(self.catalog_option):
            return replace(self.sql, default_catalog=str(options[self.catalog_option]))
        return self.sql

    def describe(self) -> dict[str, Any]:
        return {"kind": self.kind, "label": self.label, "dialect": self.sql.name,
                "default_catalog": self.sql.default_catalog, "name_levels": list(self.name_levels),
                "capabilities": sorted(self.capabilities), "default_port": self.default_port,
                "is_test_adapter": self.is_test_adapter, "note": self.note,
                "options": [{"key": o.key, "label": o.label, "type": o.type, "required": o.required, "hint": o.hint}
                            for o in self.options]}


class UnsupportedEngine(Exception):
    pass


class CapabilityMissing(Exception):
    """数据源的引擎不具备所需能力（例如在只采元数据的引擎上执行问数）。"""

    def __init__(self, engine: Engine, capability: str, datasource: str | None = None):
        where = f"数据源 {datasource}（{engine.label}）" if datasource else engine.label
        super().__init__(f"{where}不支持{CAPABILITY_LABEL.get(capability, capability)}")
        self.engine = engine
        self.capability = capability


CAPABILITY_LABEL = {METADATA: "元数据采集", VIEW_LINEAGE: "视图血缘解析", QUERY: "问数执行",
                    EXTERNAL_TABLES: "外部表追溯", IDENTITY_TARGET: "作为外部表的追溯目标",
                    LIVE_CHECK: "执行前核对来源结构", DDL_AUDIT: "DDL 审计归因"}
