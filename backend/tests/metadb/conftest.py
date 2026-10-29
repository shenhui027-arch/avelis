"""只用开发者自己的元数据库与 PostgreSQL 源库跑的测试（不 seed、不重置共用的演示库）。

运行：AVELIS_DATABASE_URL 指向库名以 _dev 或 _test 结尾的库（会被清空并迁移），例如
  AVELIS_DATABASE_URL=postgresql+psycopg://avelis:…@127.0.0.1:15432/avelis_hist_dev uv run pytest -m db tests/metadb
源库测试会在同一实例上建 <库名>_src（部署 DDL 审计的事件触发器），只影响这个库。未设置时整组跳过。
"""

from __future__ import annotations

import psycopg
import pytest
from sqlalchemy.engine import make_url


def _dev_url():
    from avelis.config import get_settings

    url = make_url(get_settings().database_url)
    if not (url.database or "").endswith(("_dev", "_test")):
        pytest.skip("AVELIS_DATABASE_URL 不是 *_dev / *_test 库：这组测试会清空它，只在开发者自己的库上运行")
    return url


@pytest.fixture(scope="module")
def metadb():
    """清空并迁移开发元数据库，返回 SQLAlchemy 引擎。"""
    _dev_url()
    from avelis.db import get_engine
    from avelis.devtools import reset_metadata_db

    reset_metadata_db()
    return get_engine()


@pytest.fixture(scope="module")
def source_db():
    """同一实例上的 PostgreSQL 源库 <库名>_src：重建 store 模式并部署 DDL 审计（去掉对 avelis_collector 的授权，
    采集用当前账号）。返回 (连接参数, 库名)。"""
    from avelis.config import REPO_ROOT

    url = _dev_url()
    name = f"{url.database}_src"
    args = dict(host=url.host, port=url.port or 5432, user=url.username, password=url.password)
    with psycopg.connect(**args, dbname=url.database, autocommit=True) as conn:
        if not conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,)).fetchone():
            conn.execute(f'CREATE DATABASE "{name}"')
    script = (REPO_ROOT / "deploy" / "demo" / "shop-pg" / "init" / "03-ddl-audit.sql").read_text(encoding="utf-8")
    script = "\n".join(line for line in script.splitlines() if "avelis_collector" not in line)
    with psycopg.connect(**args, dbname=name, autocommit=True) as conn:
        conn.execute("DROP EVENT TRIGGER IF EXISTS avelis_ddl_end")
        conn.execute("DROP EVENT TRIGGER IF EXISTS avelis_sql_drop")
        conn.execute("DROP SCHEMA IF EXISTS store CASCADE")
        conn.execute("DROP SCHEMA IF EXISTS avelis_audit CASCADE")
        conn.execute(script)
        conn.execute("CREATE SCHEMA store")
        conn.execute("CREATE TABLE store.sales (sale_id int PRIMARY KEY, amount numeric(18,2), notes text)")
        conn.execute("TRUNCATE avelis_audit.ddl_event")
    return args, name
