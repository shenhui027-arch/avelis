"""执行前 SQL 校验共用的禁止清单：语句里出现就拒绝，不论哪个引擎、哪条入口（问数、探索查询、已核验查询）。

这些写法都是合法的 SELECT，却能读写服务器文件、拖延或终止会话、连到别的服务，或在数据库主机上执行代码，
单靠“只允许 SELECT”拦不住。名单按函数名匹配（不区分大小写），同时覆盖 SQLGlot 认识的函数节点与未知函数
（exp.Anonymous）；语法节点按类型匹配。
"""

from __future__ import annotations

from sqlglot import exp

BANNED_FUNCTIONS = frozenset(
    {
        # 拖延或终止会话
        "sleep",
        "pg_sleep",
        "pg_sleep_for",
        "pg_sleep_until",
        "benchmark",
        "pg_terminate_backend",
        "pg_cancel_backend",
        # 读写服务器文件、连到别的服务
        "load_file",
        "pg_read_file",
        "pg_read_binary_file",
        "pg_ls_dir",
        "lo_import",
        "lo_export",
        "dblink",
        "url",
        "file",
        "files",
        "s3",
        "hdfs",
        "remote",
        "mysql",
        "jdbc",
        "copy",
        # 会话参数
        "set_config",
        "current_setting",
        # 结果规模不可控
        "generate_series",
        # Hive：反射调用任意 Java 静态方法
        "reflect",
        "reflect2",
        "java_method",
    }
)

# Hive 的 SELECT TRANSFORM (...) USING '<脚本>'：在集群节点上执行外部脚本
BANNED_NODES: tuple[type[exp.Expression], ...] = (exp.QueryTransform,)


def function_name(node: exp.Expression) -> str:
    """函数节点的名字（小写）；不是函数时为空串。"""
    if isinstance(node, exp.Anonymous):
        return str(node.this).lower()
    if isinstance(node, exp.Func):
        return node.sql_name().lower()
    return ""


def banned_constructs(tree: exp.Expression) -> list[str]:
    """语句里出现的禁止写法，按出现顺序去重；为空表示没有。"""
    found: list[str] = []
    for node in tree.walk():
        if isinstance(node, BANNED_NODES):
            found.append(node.key.upper())
            continue
        name = function_name(node)
        if name in BANNED_FUNCTIONS:
            found.append(name.upper())
    return list(dict.fromkeys(found))
