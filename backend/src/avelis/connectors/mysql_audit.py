"""MySQL 的可选 DDL 候选来源；只读已绑定的 general_log 或同契约插件表。

general_log 记录服务器收到的请求，不证明执行成功。事件不标记 certain=True，交由现有归因规则
与采集到的结构变化、对象、列和时间窗口互相印证。没有当前库信息的语句不猜默认库。

general_log.event_time 是 TIMESTAMP，按读取会话的 time_zone 显示；log_timestamps 只控制文件日志。
读取会话固定为 +00:00，窗口参数和返回的无时区 datetime 均为 UTC，不依赖命名时区表。
兼容插件表也必须用 TIMESTAMP；DATETIME 没有足够的时区语义，不能擅自解释为 UTC。
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

import pymysql
import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError

from avelis.connectors.ddl import AuditUnavailable, DdlEvent, Token, lex
from avelis.i18n import gettext

_REQUIRED_COLUMNS = {"event_time", "user_host", "command_type", "argument"}
_TEXT_TYPES = {
    "char",
    "varchar",
    "tinytext",
    "text",
    "mediumtext",
    "longtext",
    "tinyblob",
    "blob",
    "mediumblob",
    "longblob",
    "binary",
    "varbinary",
    "enum",
    "set",
}
_USER_HOST = re.compile(r"^(?P<user>[^\[\]]+)\[[^\[\]]*\]\s+@\s+.*\[[^\[\]]*\]$")
_DDL_WORDS = ("CREATE", "ALTER", "DROP", "RENAME", "TRUNCATE")
_AUDIT_ROW_LIMIT = 10_000


def audit_source(options: dict[str, Any]) -> str | None:
    value = options.get("ddl_audit_table")
    return f"mysql:{value.strip()}" if isinstance(value, str) and value.strip() else None


def _table(options: dict[str, Any]) -> tuple[str, str, str]:
    value = options.get("ddl_audit_table")
    if value is None or isinstance(value, str) and not value.strip():
        raise AuditUnavailable(gettext("未绑定 DDL 审计表（options.ddl_audit_table），没有审计来源"), "not_enabled")
    parsed = lex(value) if isinstance(value, str) else None
    tokens = parsed.tokens if parsed else []
    if (
        parsed is None
        or not parsed.certain
        or len(tokens) != 3
        or [t.kind for t in tokens] != ["ident", "dot", "ident"]
        or any(not t.text or "\x00" in t.text for t in (tokens[0], tokens[2]))
    ):
        raise AuditUnavailable(gettext("DDL 审计表名称无效：请填写库.表"), "incompatible")
    db, name = tokens[0].text, tokens[2].text
    quoted = ".".join("`" + part.replace("`", "``") + "`" for part in (db, name))
    return db, name, quoted


def _check_structure(cur, quoted: str) -> None:
    cur.execute(f"SHOW COLUMNS FROM {quoted}")
    columns = {r["Field"].lower(): str(r["Type"]).lower().split("(", 1)[0].strip() for r in cur.fetchall()}
    missing = _REQUIRED_COLUMNS - columns.keys()
    if missing:
        raise AuditUnavailable(
            gettext("DDL 审计表 {table} 结构不符：缺少列 {columns}", table=quoted, columns=", ".join(sorted(missing))),
            "incompatible",
        )
    if columns["event_time"] != "timestamp":
        raise AuditUnavailable(
            gettext("DDL 审计表 {table} 的 event_time 必须为 TIMESTAMP；不能推测 DATETIME 的时区", table=quoted),
            "incompatible",
        )
    if any(columns[name] not in _TEXT_TYPES for name in _REQUIRED_COLUMNS - {"event_time"}):
        raise AuditUnavailable(
            gettext("DDL 审计表 {table} 的 user_host、command_type、argument 必须为文本或二进制列", table=quoted),
            "incompatible",
        )


def _utc(at: datetime) -> datetime:
    return at.astimezone(timezone.utc).replace(tzinfo=None) if at.tzinfo is not None else at


def _text(value: Any, column: str) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError:
            pass
    raise AuditUnavailable(gettext("DDL 审计表的 {column} 不是有效的 UTF-8 文本", column=column), "incompatible")


def _lexical_objects(tokens: list[Token]) -> tuple[str, list[tuple[str | None, str]]]:
    """只按 DDL 语法位置取目标；不搜索 SELECT / 子查询里的表名。"""

    def word(i: int, *values: str) -> bool:
        return (
            i < len(tokens) and tokens[i].kind == "ident" and not tokens[i].quoted and tokens[i].text.upper() in values
        )

    def name(i: int) -> tuple[tuple[str | None, str] | None, int]:
        parts = []
        while i < len(tokens) and tokens[i].kind == "ident":
            parts.append(tokens[i].text)
            i += 1
            if i < len(tokens) and tokens[i].kind == "dot":
                i += 1
            else:
                break
        if len(parts) == 2:
            return (parts[0], parts[1]), i
        return (None, parts[-1]) if parts else None, i

    head = tokens[0].text.upper() if word(0, *_DDL_WORDS) else ""
    if not head:
        return "", []
    i = 1
    if word(i, "OR") and word(i + 1, "REPLACE"):
        i += 2
    if word(i, "TEMPORARY"):
        return "TEMPORARY", []  # 临时表遮蔽同名持久表，不能用它解释持久元数据变化。
    if word(i, "UNIQUE", "FULLTEXT", "SPATIAL"):
        i += 1
    if word(i, "INDEX") and head in {"CREATE", "DROP"}:
        _, i = name(i + 1)
        while i < len(tokens) and tokens[i].kind != "lparen" and not word(i, "ON"):
            i += 1
        target, _ = name(i + 1) if word(i, "ON") else (None, i)
        return f"{head} INDEX", [target] if target else []
    if word(i, "TABLE", "VIEW"):
        i += 1
    elif not word(0, "TRUNCATE"):
        return head, []
    if word(i, "IF"):
        i += 1
        if word(i, "NOT"):
            i += 1
        if not word(i, "EXISTS"):
            return head, []
        i += 1
    target, i = name(i)
    objects = [target] if target else []
    if head == "DROP":
        while i < len(tokens) and tokens[i].kind == "comma":
            target, i = name(i + 1)
            if target:
                objects.append(target)
    elif head == "RENAME":
        while word(i, "TO"):
            target, i = name(i + 1)
            if target:
                objects.append(target)
            if i >= len(tokens) or tokens[i].kind != "comma":
                break
            target, i = name(i + 1)
            if target:
                objects.append(target)
    elif head == "ALTER":
        # 表改名可用 RENAME [TO|AS]；RENAME COLUMN / INDEX / KEY 不改表名。
        depth, action_start = 0, True
        while i < len(tokens):
            token = tokens[i]
            if depth == 0 and action_start and word(i, "RENAME"):
                j = i + 1
                if word(j, "TO", "AS"):
                    j += 1
                if not word(j, "COLUMN", "INDEX", "KEY", "PARTITION"):
                    target, _ = name(j)
                    if target:
                        objects.append(target)
            if token.kind == "lparen":
                depth += 1
            elif token.kind == "rparen":
                depth -= 1
            action_start = depth == 0 and token.kind == "comma"
            i += 1
    return head, objects


def _executable_comment(statement: str) -> bool:
    """只检查真正的 MySQL 可执行注释，跳过字符串、标识符和普通注释。"""
    i, size = 0, len(statement)
    while i < size:
        char = statement[i]
        if char in "'\"`":
            quote, i = char, i + 1
            while i < size:
                if statement[i] == "\\":
                    i += 2
                elif statement[i] == quote:
                    if i + 1 < size and statement[i + 1] == quote:
                        i += 2
                    else:
                        i += 1
                        break
                else:
                    i += 1
        elif char == "#" or statement.startswith("--", i) and (i + 2 == size or statement[i + 2].isspace()):
            end = statement.find("\n", i)
            i = size if end < 0 else end + 1
        elif statement.startswith("/*", i):
            if statement.startswith("/*!", i):
                return True
            end = statement.find("*/", i + 2)
            i = size if end < 0 else end + 2
        else:
            i += 1
    return False


def _objects(
    statement: str, databases: set[str] | None = None, *, insensitive: bool = False
) -> tuple[str, list[tuple[str, str]]]:
    """只取 DDL 的目标，不把 CREATE VIEW / TABLE AS 的上游表误当作被修改对象。

    SQLGlot 处理 MySQL 的反引号、DEFINER / ALGORITHM、多个 DROP / RENAME 目标。
    动作不识别时保留可确认的目标；缺当前库、多语句及可执行注释不能可靠确定候选时明确拒绝归因。
    """
    scope = {db.lower() if insensitive else db for db in databases or ()}

    def in_scope(db: str | None) -> bool:
        return db is None or not scope or (db.lower() if insensitive else db) in scope

    if _executable_comment(statement):
        raise AuditUnavailable(gettext("DDL 审计记录包含 MySQL 可执行注释，无法可靠确定候选"), "incompatible")
    lexed = lex(statement)
    multiple = any(token.kind == "semi" for token in lexed.tokens[:-1])
    single = lexed.certain and not multiple
    head, lexical = _lexical_objects(lexed.tokens) if single else ("", [])
    if head == "TEMPORARY":
        return "", []

    def checked(kind: str, objects: list[tuple[str | None, str]]) -> tuple[str, list[tuple[str, str]]]:
        if any(db is None for db, _ in objects):
            raise AuditUnavailable(
                gettext("DDL 审计语句缺少当前库上下文：存在未限定库名的对象，无法可靠确定候选"), "incompatible"
            )
        return kind, list(dict.fromkeys((db, name) for db, name in objects if db is not None))

    try:
        # Command 兜底会发出 warning；context=0 避免审计语句原文进入应用日志。
        trees = [
            tree
            for tree in sqlglot.parse(statement, read="mysql", error_message_context=0)
            if tree is not None and not isinstance(tree, exp.Semicolon)
        ]
    except SqlglotError:
        if multiple:
            chunks: list[list[Token]] = [[]]
            for token in lexed.tokens:
                if token.kind == "semi":
                    chunks.append([])
                else:
                    chunks[-1].append(token)
            candidates = [obj for chunk in chunks for obj in _lexical_objects(chunk)[1]]
            if candidates and any(in_scope(db) for db, _ in candidates):
                raise AuditUnavailable(gettext("DDL 审计记录包含多条语句，无法可靠确定候选"), "incompatible") from None
        return checked(head, lexical)
    if len(trees) > 1:
        # 按独立语句识别目标仅用于判断是否影响采集范围，绝不把请求拆成可靠的独立事件。
        candidates = [
            obj
            for tree in trees
            for obj in _objects(tree.sql(dialect="mysql", comments=False), databases, insensitive=insensitive)[1]
        ]
        if candidates and any(in_scope(db) for db, _ in candidates):
            raise AuditUnavailable(gettext("DDL 审计记录包含多条语句，无法可靠确定候选"), "incompatible")
        return "", []
    if not trees:
        return "", []
    tree = trees[0]
    if isinstance(tree, (exp.Create, exp.Drop)) and (
        tree.args.get("temporary")
        or any(
            isinstance(p, exp.TemporaryProperty) for p in (tree.args.get("properties") or exp.Properties()).expressions
        )
    ):
        return "", []
    objects = lexical
    targets: list[exp.Expression] = []
    if isinstance(tree, exp.RenameTable):
        head = "RENAME"
        for action in tree.expressions:
            if isinstance(action, exp.Alter):
                targets.append(action.this)
                targets.extend(a.this for a in action.args.get("actions", []) if isinstance(a, exp.AlterRename))
    elif isinstance(tree, (exp.Create, exp.Alter, exp.Drop)) and tree.args.get("kind") in {"TABLE", "VIEW"}:
        head = "CREATE" if isinstance(tree, exp.Create) else "ALTER" if isinstance(tree, exp.Alter) else "DROP"
        if tree.this is not None:
            targets.append(tree.this)
        targets.extend(tree.args.get("tables") or [])
        if isinstance(tree, exp.Alter):
            targets.extend(a.this for a in tree.args.get("actions", []) if isinstance(a, exp.AlterRename))
    elif isinstance(tree, exp.TruncateTable) and not tree.args.get("is_database"):
        head = "TRUNCATE"
        targets.extend(tree.expressions)
    elif isinstance(tree, exp.Create) and tree.args.get("kind") == "INDEX" and isinstance(tree.this, exp.Index):
        head = "CREATE INDEX"
        if tree.this.args.get("table") is not None:
            targets.append(tree.this.args["table"])
    elif isinstance(tree, exp.Drop) and tree.args.get("kind") == "INDEX":
        head = "DROP INDEX"
        on = tree.args.get("cluster")
        if isinstance(on, exp.OnProperty):
            targets.append(on.this)
    elif isinstance(tree, exp.Command) and str(tree.this).upper() in _DDL_WORDS:
        # Command 已由 MySQL 词法器分成单条请求；取其开头，兼容 # 注释与 MySQL 字符串转义。
        head, objects = _lexical_objects(lex(f"{tree.this} {tree.expression or ''}").tokens)
    for target in targets:
        if isinstance(target, exp.Schema):
            target = target.this
        if isinstance(target, exp.Table) and target.name and not target.catalog:
            obj = (target.db or None, target.name)
            if obj not in objects:
                objects.append(obj)
    return checked(head, objects)


def _audit_cause(exc: pymysql.MySQLError) -> str:
    code = exc.args[0] if exc.args else None
    message = str(exc.args[-1] if exc.args else exc).lower()
    if code in (1044, 1045, 1142, 1143, 1227, 1370) or "access denied" in message or "command denied" in message:
        return "no_permission"
    if code in (1049, 1109, 1146) or "unknown table" in message or "doesn't exist" in message:
        return "not_enabled"
    if code in (1054, 1064) or "unknown column" in message:
        return "incompatible"
    return "error"


def ddl_events(connector, since: datetime, until: datetime, cursors=None) -> list[DdlEvent]:
    db, name, quoted = _table(connector.options)
    databases = set(connector.options.get("databases", []))
    conn = None
    try:
        conn = connector._connect(read_timeout=60)
        cur = conn.cursor()
        if (db.lower(), name.lower()) == ("mysql", "general_log"):
            cur.execute("SELECT @@GLOBAL.general_log AS enabled, @@GLOBAL.log_output AS output")
            settings = cur.fetchone() or {}
            enabled = str(settings.get("enabled", "")).upper() in {"1", "ON", "TRUE"}
            outputs = {s.strip().upper() for s in str(settings.get("output", "")).split(",")}
            if not enabled or "TABLE" not in outputs or "NONE" in outputs:
                raise AuditUnavailable(
                    gettext("MySQL 通用查询日志未写入表：需要 general_log=ON 且 log_output 包含 TABLE"), "not_enabled"
                )
        _check_structure(cur, quoted)
        cur.execute("SELECT @@lower_case_table_names AS lower_case_table_names")
        mode = (cur.fetchone() or {}).get("lower_case_table_names")
        if str(mode) not in {"0", "1", "2"}:
            raise AuditUnavailable(gettext("无法确定 MySQL lower_case_table_names，不能可靠解析库表名"), "incompatible")
        insensitive = str(mode) in {"1", "2"}
        connector.ddl_object_fold = "insensitive" if insensitive else "exact"
        canonical_dbs, canonical_tables = {}, {}
        if insensitive:
            # 与 snapshot 同取系统目录的名称；模式 2 必须保留目录写法，不能把所有输出转成小写。
            params = tuple(sorted(database.lower() for database in databases))
            placeholders = ",".join("%s" for _ in params)
            db_where = f" WHERE LOWER(SCHEMA_NAME) IN ({placeholders})" if params else ""
            cur.execute(f"SELECT SCHEMA_NAME FROM information_schema.schemata{db_where}", params)
            canonical_dbs = {row["SCHEMA_NAME"].lower(): row["SCHEMA_NAME"] for row in cur.fetchall()}
            table_where = f" WHERE LOWER(TABLE_SCHEMA) IN ({placeholders})" if params else ""
            cur.execute(f"SELECT TABLE_SCHEMA, TABLE_NAME FROM information_schema.tables{table_where}", params)
            canonical_tables = {
                (row["TABLE_SCHEMA"].lower(), row["TABLE_NAME"].lower()): (row["TABLE_SCHEMA"], row["TABLE_NAME"])
                for row in cur.fetchall()
            }
        cur.execute("SET SESSION time_zone = '+00:00'")
        # 任意位置的关键词只作粗筛，保留前导注释；字符串中的噪声仍由客户端解析排除。
        ddl_filter = " OR ".join(f"UPPER(CONVERT(argument USING utf8mb4)) LIKE '%%{word}%%'" for word in _DDL_WORDS)
        cur.execute(
            f"SELECT event_time, user_host, command_type, argument FROM {quoted} "
            "WHERE event_time >= %s AND event_time <= %s AND command_type IN ('Query', 'Execute') "
            f"AND ({ddl_filter}) ORDER BY event_time LIMIT {_AUDIT_ROW_LIMIT + 1}",
            (_utc(since), _utc(until)),
        )
        rows = cur.fetchall()
        if len(rows) > _AUDIT_ROW_LIMIT:
            raise AuditUnavailable(
                gettext("DDL 审计候选超过读取上限（{limit} 条），无法完整归因", limit=_AUDIT_ROW_LIMIT), "error"
            )
    except pymysql.MySQLError as exc:
        message = str(exc.args[-1] if exc.args else exc).strip()[:500]
        raise AuditUnavailable(
            gettext("读取 DDL 审计表 {table} 失败：{error}", table=quoted, error=message), _audit_cause(exc)
        ) from exc
    finally:
        if conn is not None:
            conn.close()
    scope = {database.lower() if insensitive else database for database in databases}
    events = []
    for row in rows:
        # Prepare 只准备语句；Query / Execute 才是收到执行请求（仍不代表执行成功）。
        if _text(row["command_type"], "command_type").lower() not in {"query", "execute"}:
            continue
        statement = _text(row["argument"], "argument")
        kind, objects = _objects(statement, databases, insensitive=insensitive)
        if insensitive:
            # DROP / RENAME 的旧名可能已不在目录；模式 1 可确定小写，模式 2 留原文，
            # 归因索引按 ddl_object_fold 匹配历史快照，避免臆造已消失名称的目录写法。
            objects = [
                canonical_tables.get(
                    (database.lower(), table.lower()),
                    (
                        canonical_dbs.get(database.lower(), database.lower() if str(mode) == "1" else database),
                        table.lower() if str(mode) == "1" else table,
                    ),
                )
                for database, table in objects
            ]
        objects = list(
            dict.fromkeys(obj for obj in objects if not scope or (obj[0].lower() if insensitive else obj[0]) in scope)
        )
        if not objects:
            continue
        user_host = _text(row["user_host"], "user_host")
        matched_user = _USER_HOST.fullmatch(user_host)
        user = matched_user.group("user") if matched_user else ""
        if not user.strip():
            raise AuditUnavailable(gettext("DDL 审计表的 user_host 无法解析数据库用户名，不能归因"), "incompatible")
        at = row["event_time"]
        if not isinstance(at, datetime):
            raise AuditUnavailable(gettext("DDL 审计表的 event_time 不是有效的时间值"), "incompatible")
        at = _utc(at)
        if _utc(since) <= at <= _utc(until):
            events.append(
                DdlEvent(
                    at=at,
                    db_user=user,
                    objects=objects,
                    statement_type=kind,
                    statement=statement,
                    certain=False if kind in {"CREATE INDEX", "DROP INDEX"} else None,
                )
            )
    return events
