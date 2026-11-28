"""TiDB 连接器：MySQL 协议，复用 MySQL 连接器的采集、执行前核对与问数流程，只覆盖 TiDB 不同的地方。

2026-11-28 在 TiDB v8.5.4（unistore 单机）上实测的差异，决定了下面每个钩子：
- @@server_uuid 恒为全零，不能作实例身份；TiDB 没有对外可读的集群 ID，instance_id 留空（采集不做实例变化检测）。
- START TRANSACTION READ ONLY / transaction_read_only 只有空实现（报 1235），会话级无法让引擎拒绝写入。
  历史读事务（START TRANSACTION READ ONLY AS OF TIMESTAMP …）虽然只读，但读的是过去时间点的表结构：
  刚执行过 DDL 或刚建的表在其中报 1146（表不存在），实测 AS OF NOW(6) 也只有 3/5 次成功，不能用于问数。
  只读因此依赖 Avelis 的单条 SELECT 校验与只读问数账号（只授予 SELECT 与 SHOW VIEW），与 Hive 相同。
- 每查询内存上限有等价参数 tidb_mem_quota_query，mem_limit_bytes 映射到它。
- information_schema.tables.TIDB_TABLE_ID 在 TRUNCATE 后会换新值，不能作物理身份（否则清空数据被当成删表重建）。
- information_schema.ddl_jobs 有语句与时间但没有执行用户，暂不声明 DDL 归因。
- 视图定义（information_schema.views.VIEW_DEFINITION）是补全库名、带反引号的规范文本，交 MySQL 方言解析。
"""

from __future__ import annotations

from typing import Any

import pymysql

from avelis.connectors.base import QueryError
from avelis.connectors.mysql import MySQLMetaConnector, _query_error
from avelis.i18n import gettext

# TiDB 特有错误码：8175 单条查询超出内存上限（tidb_mem_quota_query）；8121 权限检查失败（如只读账号执行 UPDATE）
TIDB_MEMORY_QUOTA_ERRNO = 8175
TIDB_PRIVILEGE_ERRNO = 8121


class TiDBConnector(MySQLMetaConnector):
    kind = "tidb"
    dialect = "mysql"
    is_test_adapter = False

    # DDL 审计来源未接入：不提供 ddl_audit_source / ddl_events（父类的 MySQL 审计表实现不适用于 TiDB）
    @property
    def ddl_audit_source(self) -> str | None:
        return None

    @property
    def ddl_audit_unbound_reason(self) -> str | None:
        return gettext("TiDB 的 DDL 作业记录不含执行用户，暂不支持 DDL 归因")

    def ddl_events(self, since, until, cursors=None):  # noqa: ANN001 - 签名与父类一致
        raise NotImplementedError("TiDB 未声明 DDL 归因能力")

    def _session_limits(self, cur, *, timeout_seconds: int, max_rows: int, mem_limit_bytes: int) -> None:
        """MAX_EXECUTION_TIME 与 SQL_SELECT_LIMIT 同 MySQL；mem_limit_bytes > 0 时再设 tidb_mem_quota_query。"""
        super()._session_limits(
            cur, timeout_seconds=timeout_seconds, max_rows=max_rows, mem_limit_bytes=mem_limit_bytes
        )
        if mem_limit_bytes > 0:
            cur.execute(f"SET SESSION tidb_mem_quota_query = {int(mem_limit_bytes)}")

    def _begin_read_only(self, cur) -> None:
        """不开启事务：TiDB 的 READ ONLY 只有空实现，历史读事务读不到刚变更的表结构，见模块说明。"""
        return None

    def _query_error(self, exc: pymysql.MySQLError) -> QueryError:
        """在 MySQL 归类基础上补 TiDB 错误码：8175（超出内存上限）归为 other，8121（权限检查失败）归为 permission。"""
        error = _query_error(exc)
        if error.db_errno == TIDB_MEMORY_QUOTA_ERRNO:
            return QueryError("other", error.db_errno, error.message)
        if error.db_errno == TIDB_PRIVILEGE_ERRNO:
            return QueryError("permission", error.db_errno, error.message)
        return error

    def _missing_view_definition(self, name: str) -> str:
        return gettext("TiDB 视图 {name} 未读取到定义（需要 SHOW VIEW 权限）；列级血缘不可用", name=name)

    @staticmethod
    def _server_identity(cur) -> dict[str, Any]:
        """实例信息：instance_id 留空；记录 hostname、port、version（VERSION() 原文，如 8.0.11-TiDB-v8.5.4）。"""
        cur.execute("SELECT @@hostname AS hostname, @@port AS port, VERSION() AS version")
        row = cur.fetchone()
        return {
            "instance_id": None,
            "hostname": row["hostname"],
            "port": int(row["port"]),
            "version": row["version"],
        }

    def _physical_id(self, table_row: dict[str, Any]) -> str | None:
        return None  # TIDB_TABLE_ID 在 TRUNCATE 后会变，见模块说明


__all__ = ["TIDB_MEMORY_QUOTA_ERRNO", "TIDB_PRIVILEGE_ERRNO", "TiDBConnector", "_query_error"]
