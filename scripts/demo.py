"""Avelis 演示环境：生成配置、校验驱动、起停、灌数、检查基线、实机核验。

在仓库根目录运行：
  uv run scripts/demo.py up            生成 .env、校验驱动、启动容器、等待就绪并灌数
  uv run scripts/demo.py up --engines  同时启动更多引擎的演示库（ClickHouse 等），并灌数
  uv run scripts/demo.py seed-engines  把更多引擎的演示库重置到验收基线
  uv run scripts/demo.py seed          把演示库重置到验收基线
  uv run scripts/demo.py check         核对验收基线数值
  uv run scripts/demo.py verify        StarRocks 实机核验，结果写入 .demo/sr-verify.json
  uv run scripts/demo.py down [--volumes]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import socket
import string
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import clickhouse_connect
import oracledb
import psycopg
import pymssql
import pymysql

ROOT = Path(__file__).resolve().parent.parent
DEMO = ROOT / "deploy" / "demo"
# AVELIS_ENV_FILE 的相对路径按仓库根目录解析（与后端 config.env_file 一致），子进程在任何工作目录下读到同一个文件
ENV_FILE = ROOT / Path(os.environ.get("AVELIS_ENV_FILE") or ".env")
ENV_EXAMPLE = ROOT / ".env.example"
# 演示库所在主机；在 Docker 部署的容器里灌演示数据时设为 host.docker.internal
SOURCE_HOST = os.environ.get("AVELIS_DEMO_SOURCE_HOST", "127.0.0.1")
STATE = ROOT / ".demo"
DRIVER_LOCK = DEMO / "drivers.lock"
SEED_DIR = DEMO / "starrocks" / "seed"
SHOP_MYSQL_SEED = DEMO / "shop-mysql" / "init" / "01-shop-dim.sql"
SHOP_PG_SEED = DEMO / "shop-pg" / "init" / "02-store.sql"
SHOP_PG_DDL_AUDIT = DEMO / "shop-pg" / "init" / "03-ddl-audit.sql"
SHOP_CH_SEED = DEMO / "shop-ch" / "store.sql"
SHOP_MSSQL_SEED = DEMO / "shop-mssql" / "store.sql"
SHOP_ORACLE_SEED = DEMO / "shop-oracle" / "store.sql"
SHOP_DORIS_SEED = DEMO / "shop-doris" / "store.sql"

DEMO_DATABASES = ("shop", "ads", "dim")
CATALOG = "shop_mysql"
SR_USERS = {"avelis_collector": "SR_COLLECTOR_PASSWORD", "avelis_reader": "SR_READER_PASSWORD"}
SHOP_MYSQL_INTERNAL = ("shop-mysql", 3306)


def log(msg: str) -> None:
    print(msg, flush=True)


# --- configuration -----------------------------------------------------------


def read_env_file(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            env[key.strip()] = value.strip()
    return env


def load_env() -> dict[str, str]:
    if ENV_FILE.exists():
        return read_env_file(ENV_FILE)
    # Docker 部署里给演示环境灌数据时没有 .env 文件，演示库配置经环境变量传入
    if os.environ.get("AVELIS_DEMO_USER_PASSWORD"):
        return dict(os.environ)
    sys.exit("缺少 .env，先运行：uv run scripts/demo.py init-env")


def init_env() -> None:
    """Write .env from .env.example, keeping existing values and generating empty passwords and secrets."""
    existing = read_env_file(ENV_FILE) if ENV_FILE.exists() else {}
    alphabet = string.ascii_letters + string.digits
    lines, generated = [], 0
    for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
        key, sep, value = line.partition("=")
        key = key.strip()
        if sep and not line.lstrip().startswith("#"):
            value = existing.get(key, value.strip())
            if key.endswith("_PASSWORD") and not value:
                value = _password(alphabet)
                generated += 1
            elif key.endswith("_SECRET") and not value:
                value = "".join(secrets.choice(alphabet) for _ in range(48))
                generated += 1
            line = f"{key}={value}"
        lines.append(line)
    ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    log(f".env 已就绪（新生成 {generated} 个口令，未打印）")


def _password(alphabet: str) -> str:
    """24 位随机口令，至少含大写、小写、数字各一（满足 SQL Server、Oracle 的复杂度要求）。"""
    while True:
        value = "".join(secrets.choice(alphabet) for _ in range(24))
        if any(c.isupper() for c in value) and any(c.islower() for c in value) and any(c.isdigit() for c in value):
            return value


def fetch_driver() -> None:
    lock = json.loads(DRIVER_LOCK.read_text(encoding="utf-8"))
    target_dir = DEMO / "drivers"
    target_dir.mkdir(parents=True, exist_ok=True)
    for name, spec in lock.items():
        target = target_dir / spec["file"]
        if target.exists() and _sha256(target) == spec["sha256"]:
            log(f"驱动已校验：{name} {spec['version']}")
            continue
        log(f"下载 {name} {spec['version']} ...")
        tmp = target.with_suffix(".part")
        urllib.request.urlretrieve(spec["url"], tmp)
        digest = _sha256(tmp)
        if digest != spec["sha256"]:
            tmp.unlink()
            sys.exit(f"{name} 校验失败：期望 {spec['sha256']}，实际 {digest}")
        tmp.replace(target)
        log(f"驱动已下载并校验：{target.name}")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# --- containers --------------------------------------------------------------


def compose(*args: str, engines: bool = False) -> None:
    profile = ["--profile", "engines"] if engines else []
    cmd = ["docker", "compose", "--env-file", str(ENV_FILE), "-f", str(DEMO / "compose.yaml"), *profile, *args]
    subprocess.run(cmd, check=True, cwd=ROOT)


def wait_for(label: str, probe, timeout: int) -> None:
    deadline = time.monotonic() + timeout
    last_error = ""
    while time.monotonic() < deadline:
        try:
            if probe():
                log(f"  就绪：{label}")
                return
        except Exception as exc:  # noqa: BLE001 - keep polling until the deadline
            last_error = f"{type(exc).__name__}: {exc}"
        time.sleep(3)
    sys.exit(f"等待 {label} 超时（{timeout}s）。最后错误：{last_error}")


def sr_connect(env: dict[str, str], user: str = "root", password: str | None = None) -> pymysql.Connection:
    if password is None:
        password = env["SR_ROOT_PASSWORD"] if user == "root" else env[SR_USERS[user]]
    return pymysql.connect(
        host=SOURCE_HOST,
        port=int(env["SR_QUERY_PORT"]),
        user=user,
        password=password,
        autocommit=True,
        charset="utf8mb4",
        connect_timeout=5,
        read_timeout=180,
        write_timeout=60,
    )


def mysql_connect(env: dict[str, str], user: str = "root") -> pymysql.Connection:
    """Connect to the demo dimension database (shop-mysql)."""
    password = env["SHOP_MYSQL_ROOT_PASSWORD"] if user == "root" else env["SHOP_MYSQL_COLLECTOR_PASSWORD"]
    return pymysql.connect(
        host=SOURCE_HOST,
        port=int(env["SHOP_MYSQL_PORT"]),
        user=user,
        password=password,
        autocommit=True,
        charset="utf8mb4",
        connect_timeout=5,
        read_timeout=60,
    )


def pg_connect(env: dict[str, str], user: str = "postgres") -> psycopg.Connection:
    """Connect to the demo store database (shop-pg). postgres only seeds; avelis_reader is read-only."""
    password = {
        "postgres": "SHOP_PG_PASSWORD",
        "avelis_collector": "SHOP_PG_COLLECTOR_PASSWORD",
        "avelis_reader": "SHOP_PG_READER_PASSWORD",
    }[user]
    return psycopg.connect(
        host=SOURCE_HOST,
        port=int(env["SHOP_PG_PORT"]),
        user=user,
        password=env[password],
        dbname="storedb",
        connect_timeout=5,
        autocommit=True,
    )


def ch_connect(env: dict[str, str], user: str = "default"):
    """Connect to the demo ClickHouse store (shop-ch). default only seeds; avelis_reader is readonly=2."""
    password = {
        "default": "SHOP_CH_PASSWORD",
        "avelis_collector": "SHOP_CH_COLLECTOR_PASSWORD",
        "avelis_reader": "SHOP_CH_READER_PASSWORD",
    }[user]
    return clickhouse_connect.get_client(
        host=SOURCE_HOST,
        port=int(env["SHOP_CH_HTTP_PORT"]),
        username=user,
        password=env[password],
        connect_timeout=5,
        autogenerate_session_id=False,
    )


def mssql_connect(env: dict[str, str], user: str = "sa", database: str = "storedb") -> pymssql.Connection:
    """Connect to the demo SQL Server store (shop-mssql). sa only seeds; avelis_reader only has SELECT."""
    password = {
        "sa": "SHOP_MSSQL_SA_PASSWORD",
        "avelis_collector": "SHOP_MSSQL_COLLECTOR_PASSWORD",
        "avelis_reader": "SHOP_MSSQL_READER_PASSWORD",
    }[user]
    return pymssql.connect(
        server=SOURCE_HOST,
        port=env["SHOP_MSSQL_PORT"],
        user=user,
        password=env[password],
        database=database,
        login_timeout=5,
        autocommit=True,
        charset="UTF-8",
    )


def oracle_connect(env: dict[str, str], user: str = "system") -> oracledb.Connection:
    """Connect to the demo Oracle store (shop-oracle, service FREEPDB1). system only seeds."""
    password = {
        "system": "SHOP_ORACLE_SYSTEM_PASSWORD",
        "avelis_collector": "SHOP_ORACLE_COLLECTOR_PASSWORD",
        "avelis_reader": "SHOP_ORACLE_READER_PASSWORD",
    }[user]
    return oracledb.connect(
        user=user,
        password=env[password],
        host=SOURCE_HOST,
        port=int(env["SHOP_ORACLE_PORT"]),
        service_name="FREEPDB1",
        tcp_connect_timeout=5,
    )


def doris_connect(env: dict[str, str], user: str = "root", password: str | None = None) -> pymysql.Connection:
    """Connect to the demo Doris store (shop-doris). root only seeds; avelis_reader only has SELECT."""
    key = {
        "root": "SHOP_DORIS_ROOT_PASSWORD",
        "avelis_collector": "SHOP_DORIS_COLLECTOR_PASSWORD",
        "avelis_reader": "SHOP_DORIS_READER_PASSWORD",
    }[user]
    return pymysql.connect(
        host=SOURCE_HOST,
        port=int(env["SHOP_DORIS_QUERY_PORT"]),
        user=user,
        password=env[key] if password is None else password,
        autocommit=True,
        charset="utf8mb4",
        connect_timeout=5,
    )


def sr_root(env: dict[str, str]) -> pymysql.Connection:
    return sr_connect(env)


def _backend_alive(conn: pymysql.Connection) -> bool:
    with conn.cursor(pymysql.cursors.DictCursor) as cur:
        cur.execute("SHOW BACKENDS")
        return any(str(row.get("Alive")).lower() == "true" for row in cur.fetchall())


def _sr_ready(env: dict[str, str]) -> bool:
    """Ready once the BE is alive. The image's own director registers the BE as
    root with an empty password on first start, so the root password is only
    set after that registration has finished."""
    with urllib.request.urlopen(f"http://{SOURCE_HOST}:{env['SR_HTTP_PORT']}/api/health", timeout=3) as resp:
        if resp.status != 200:
            return False
    try:
        conn = sr_connect(env)
        alive = _backend_alive(conn)
        conn.close()
        return alive
    except pymysql.err.OperationalError as exc:
        if exc.args[0] != 1045:
            raise
    conn = sr_connect(env, password="")
    if not _backend_alive(conn):
        conn.close()
        return False
    with conn.cursor() as cur:
        cur.execute(f"ALTER USER 'root' IDENTIFIED BY '{env['SR_ROOT_PASSWORD']}'")
    conn.close()
    log("  已为 StarRocks root 设置口令（来自 .env）")
    return True


def _meta_pg_ready(env: dict[str, str]) -> bool:
    with psycopg.connect(
        host=SOURCE_HOST,
        port=int(env["AVELIS_PG_PORT"]),
        user="avelis",
        password=env["AVELIS_PG_PASSWORD"],
        dbname="avelis",
        connect_timeout=3,
    ) as conn:
        return conn.execute("SELECT 1").fetchone() == (1,)


def _valkey_ready(env: dict[str, str]) -> bool:
    password = env["AVELIS_REDIS_PASSWORD"].encode()
    command = b"*2\r\n$4\r\nAUTH\r\n$%d\r\n%s\r\n*1\r\n$4\r\nPING\r\n" % (len(password), password)
    with socket.create_connection(("127.0.0.1", int(env["AVELIS_REDIS_PORT"])), timeout=3) as sock:
        sock.sendall(command)
        reply = b""
        while reply.count(b"\r\n") < 2:
            chunk = sock.recv(64)
            if not chunk:
                break
            reply += chunk
    return reply == b"+OK\r\n+PONG\r\n"


def _shop_pg_ready(env: dict[str, str]) -> bool:
    with pg_connect(env, "avelis_reader") as conn:
        return conn.execute("SELECT COUNT(*) FROM store.sales").fetchone()[0] > 0


def _shop_ch_ready(env: dict[str, str]) -> bool:
    client = ch_connect(env)
    try:
        return client.command("SELECT 1") == 1
    finally:
        client.close()


def _shop_mssql_ready(env: dict[str, str]) -> bool:
    conn = mssql_connect(env, database="master")
    try:
        cur = conn.cursor()
        cur.execute("SELECT 1")
        return cur.fetchone()[0] == 1
    finally:
        conn.close()


def _shop_oracle_ready(env: dict[str, str]) -> bool:
    with oracle_connect(env) as conn, conn.cursor() as cur:
        cur.execute("SELECT 1 FROM dual")
        return cur.fetchone()[0] == 1


def _doris_backend_alive(conn: pymysql.Connection) -> bool:
    with conn.cursor(pymysql.cursors.DictCursor) as cur:
        cur.execute("SHOW BACKENDS")
        return any(str(row.get("Alive")).lower() == "true" for row in cur.fetchall())


def _shop_doris_ready(env: dict[str, str]) -> bool:
    """Ready once the BE is alive. The container keeps no volume, so after every (re)start root has an empty
    password again; set it from .env the first time we see that."""
    try:
        conn = doris_connect(env)
    except pymysql.err.OperationalError as exc:
        if exc.args[0] != 1045:
            raise
        conn = doris_connect(env, password="")
        if not _doris_backend_alive(conn):
            conn.close()
            return False
        with conn.cursor() as cur:
            cur.execute(f"SET PASSWORD FOR 'root' = PASSWORD('{env['SHOP_DORIS_ROOT_PASSWORD']}')")
        conn.close()
        log("  已为 Doris root 设置口令（来自 .env）")
        return True
    try:
        return _doris_backend_alive(conn)
    finally:
        conn.close()


def _shop_mysql_ready(env: dict[str, str]) -> bool:
    conn = mysql_connect(env, "avelis_collector")
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM shop_dim.dim_product")
        ready = cur.fetchone()[0] > 0
    conn.close()
    return ready


def up(skip_seed: bool, engines: bool = False, no_recreate: bool = False) -> None:
    """no_recreate：容器已存在时不重建。演示容器由多个检出目录共用（compose 项目 avelis-demo），
    从另一个检出目录（如 git worktree）执行 up 时，挂载路径不同会让 Docker 重建正在被别人使用的容器。"""
    init_env()
    fetch_driver()
    env = load_env()
    compose("up", "-d", *(["--no-recreate"] if no_recreate else ["--remove-orphans"]), engines=engines)
    log("等待服务就绪 ...")
    wait_for("元数据库 PostgreSQL", lambda: _meta_pg_ready(env), 120)
    wait_for("Valkey（Redis 协议）", lambda: _valkey_ready(env), 60)
    wait_for("电商维表库 MySQL", lambda: _shop_mysql_ready(env), 240)
    wait_for("门店库 PostgreSQL", lambda: _shop_pg_ready(env), 120)
    wait_for("StarRocks FE + BE", lambda: _sr_ready(env), 420)
    if engines:
        wait_for("门店库 ClickHouse", lambda: _shop_ch_ready(env), 120)
        wait_for("门店库 SQL Server", lambda: _shop_mssql_ready(env), 180)
        wait_for("门店库 Oracle", lambda: _shop_oracle_ready(env), 600)
        wait_for("门店库 Doris", lambda: _shop_doris_ready(env), 300)
    if not skip_seed:
        seed(env)
        check(env)
        if engines:
            seed_engines(env)
            check_engines(env)


def down(volumes: bool) -> None:
    compose("down", "--remove-orphans", *(["--volumes"] if volumes else []), engines=True)


# --- seed --------------------------------------------------------------------


def sql_statements(path: Path) -> list[str]:
    statements, buffer = [], []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip().startswith("--") or not line.strip():
            continue
        buffer.append(line)
        if line.rstrip().endswith(";"):
            statements.append("\n".join(buffer).rstrip().rstrip(";"))
            buffer = []
    if buffer:
        statements.append("\n".join(buffer))
    return statements


def run_sql_file(conn: pymysql.Connection, path: Path) -> None:
    with conn.cursor() as cur:
        for statement in sql_statements(path):
            cur.execute(statement)


def seed(env: dict[str, str]) -> dict:
    log("把演示库重置到验收基线 ...")
    report: dict = {"seeded_at": datetime.now(timezone.utc).isoformat(), "grant_errors": []}
    # 维表库也回到基线（异常场景验收会改 MySQL 表结构）；账号与授权按库名授予，删库重建不受影响
    mconn = mysql_connect(env)
    with mconn.cursor() as cur:
        cur.execute("DROP DATABASE IF EXISTS shop_dim")
        cur.execute("CREATE DATABASE shop_dim DEFAULT CHARACTER SET utf8mb4")
    run_sql_file(mconn, SHOP_MYSQL_SEED)
    mconn.close()
    # 门店库（PostgreSQL）：重建 store 模式；账号由容器初始化脚本创建，授权在 SQL 文件里
    with pg_connect(env) as pconn:
        pconn.execute(SHOP_PG_SEED.read_text(encoding="utf-8"))
        # DDL 审计（事件触发器 → avelis_audit.ddl_event）：采集据此把结构变化归因到数据库用户（ADR 0008）
        pconn.execute(SHOP_PG_DDL_AUDIT.read_text(encoding="utf-8"))
    conn = sr_root(env)
    with conn.cursor() as cur:
        for db in DEMO_DATABASES:
            cur.execute(f"DROP DATABASE IF EXISTS {db} FORCE")
        cur.execute(f"DROP CATALOG IF EXISTS {CATALOG}")
        for user in SR_USERS:
            cur.execute(f"DROP USER IF EXISTS '{user}'@'%'")

    run_sql_file(conn, SEED_DIR / "10-shop.sql")

    host, port = SHOP_MYSQL_INTERNAL
    driver = json.loads(DRIVER_LOCK.read_text(encoding="utf-8"))["mysql-connector-j"]
    catalog_password = env["SHOP_MYSQL_CATALOG_PASSWORD"]
    with conn.cursor() as cur:
        cur.execute(
            f"""CREATE EXTERNAL CATALOG {CATALOG} COMMENT '演示电商维表库 MySQL'
            PROPERTIES (
              "type" = "jdbc",
              "user" = "sr_catalog",
              "password" = "{catalog_password}",
              "jdbc_uri" = "jdbc:mysql://{host}:{port}?allowPublicKeyRetrieval=true&useSSL=false",
              "driver_url" = "file:///opt/avelis/drivers/{driver["file"]}",
              "driver_class" = "com.mysql.cj.jdbc.Driver"
            )"""
        )
        cur.execute("CREATE DATABASE dim")
        try:
            cur.execute(
                f"""CREATE EXTERNAL TABLE dim.ext_region (
                  region_code VARCHAR(8),
                  region_name VARCHAR(16),
                  provinces   VARCHAR(64),
                  updated_at  DATETIME
                ) ENGINE = mysql
                COMMENT '销售区域（MySQL 外部表）'
                PROPERTIES (
                  "host" = "{host}",
                  "port" = "{port}",
                  "user" = "sr_legacy",
                  "password" = "{env["SHOP_MYSQL_LEGACY_PASSWORD"]}",
                  "database" = "shop_dim",
                  "table" = "dim_region"
                )"""
            )
            cur.execute("SELECT COUNT(*) FROM dim.ext_region")
            cur.fetchone()
            report["mysql_external_table"] = "supported"
        except pymysql.MySQLError as exc:
            report["mysql_external_table"] = "unsupported"
            report["mysql_external_table_error"] = str(exc).replace(env["SHOP_MYSQL_LEGACY_PASSWORD"], "<redacted>")
            log(f"  MySQL 外部表（ENGINE=mysql）不可用：{report['mysql_external_table_error']}")

    try:
        run_sql_file(conn, SEED_DIR / "40-ads.sql")
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM ads.sales_by_category")
            cur.fetchone()
        report["ads_mode"] = "view_over_catalog"
    except pymysql.MySQLError as exc:
        report["ads_mode"] = "etl_table"
        report["ads_view_error"] = str(exc)
        log(f"  视图引用 Catalog 表失败，改用 ETL 内表：{exc}")
        with conn.cursor() as cur:
            cur.execute("DROP DATABASE IF EXISTS ads FORCE")
        run_sql_file(conn, SEED_DIR / "40-ads-fallback.sql")

    region_source = (
        "dim.ext_region" if report["mysql_external_table"] == "supported" else f"{CATALOG}.shop_dim.dim_region"
    )
    with conn.cursor() as cur:
        cur.execute(
            f"""CREATE VIEW ads.region_board COMMENT '区域看板（下游依赖 shop.daily_sales）' AS
            SELECT r.region_code, r.region_name, s.order_date, s.sales_amount
            FROM {region_source} r
            LEFT JOIN shop.daily_sales s ON r.region_name = s.region"""
        )
    report["region_board_source"] = region_source

    with conn.cursor() as cur:
        for user, secret_key in SR_USERS.items():
            cur.execute(f"CREATE USER '{user}'@'%' IDENTIFIED BY '{env[secret_key]}'")
            grants = [f"GRANT USAGE ON CATALOG {CATALOG} TO USER '{user}'@'%'"]
            for db in DEMO_DATABASES:
                grants.append(f"GRANT SELECT ON ALL TABLES IN DATABASE {db} TO USER '{user}'@'%'")
                grants.append(f"GRANT SELECT ON ALL VIEWS IN DATABASE {db} TO USER '{user}'@'%'")
            for grant in grants:
                try:
                    cur.execute(grant)
                except pymysql.MySQLError as exc:
                    report["grant_errors"].append({"user": user, "grant": grant, "error": str(exc)})
            # Privileges on tables inside an external catalog are granted from within that catalog.
            grant = f"GRANT SELECT ON ALL TABLES IN ALL DATABASES TO USER '{user}'@'%'"
            try:
                cur.execute(f"SET CATALOG {CATALOG}")
                cur.execute(grant)
            except pymysql.MySQLError as exc:
                report["grant_errors"].append({"user": user, "grant": f"[{CATALOG}] {grant}", "error": str(exc)})
            finally:
                cur.execute("SET CATALOG default_catalog")
    # 电商数据集（scripts/ec_data.py）不在这里重建，但账号刚重建过：已灌入时重新授权
    import ec_data

    if ec_data.loaded_checksum(conn):
        ec_data.grant(conn, env)
        report["ec_dataset"] = ec_data.loaded_checksum(conn)
    # 制造数据集同样独立保留；演示账号重建后恢复只读授权。
    import mfg_data

    if mfg_data.loaded_checksum(conn):
        mfg_data.grant(conn, env)
        report["mfg_dataset"] = mfg_data.loaded_checksum(conn)
    conn.close()

    STATE.mkdir(exist_ok=True)
    (STATE / "seed.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    log(
        f"  灌数完成：分析层 = {report['ads_mode']}，MySQL 外部表 = {report['mysql_external_table']}，"
        f"授权失败 {len(report['grant_errors'])} 条"
    )
    return report


# --- more engines ------------------------------------------------------------


def engines_running(env: dict[str, str]) -> dict[str, bool]:
    """更多引擎的演示库是否可连接（未用 --engines 启动时为 False）。"""
    out = {}
    for name, probe in (
        ("clickhouse", _shop_ch_ready),
        ("sqlserver", _shop_mssql_ready),
        ("oracle", _shop_oracle_ready),
        ("doris", _shop_doris_ready),
    ):
        try:
            out[name] = bool(probe(env))
        except Exception:  # noqa: BLE001 - not started or not reachable
            out[name] = False
    return out


def seed_clickhouse(env: dict[str, str]) -> None:
    """重建 store 库与两个账号：采集账号只有 SHOW 权限（读系统表、不能读数据），查询账号 readonly=2 只能 SELECT。"""
    client = ch_connect(env)
    try:
        for statement in sql_statements(SHOP_CH_SEED):
            client.command(statement)
        collector, reader = env["SHOP_CH_COLLECTOR_PASSWORD"], env["SHOP_CH_READER_PASSWORD"]
        for user in ("avelis_collector", "avelis_reader"):
            client.command(f"DROP USER IF EXISTS {user}")
        client.command(
            "CREATE USER avelis_collector IDENTIFIED WITH sha256_password BY {p:String}", parameters={"p": collector}
        )
        client.command("GRANT SHOW TABLES, SHOW COLUMNS ON store.* TO avelis_collector")
        client.command(
            "CREATE USER avelis_reader IDENTIFIED WITH sha256_password BY {p:String} SETTINGS readonly = 2",
            parameters={"p": reader},
        )
        client.command("GRANT SELECT ON store.* TO avelis_reader")
        # 数据新鲜度只需分片的修改时间，不授予 system.parts 的其他列
        client.command("GRANT SELECT(database, table, active, modification_time) ON system.parts TO avelis_reader")
        # DDL 归因（ADR 0008）：采集账号只读 query_log 的六列，行策略把它限制为只见 DDL 记录（看不到查询语句）
        client.command(
            "GRANT SELECT(event_time, user, query_kind, type, tables, query) ON system.query_log TO avelis_collector"
        )
        client.command("DROP ROW POLICY IF EXISTS avelis_ddl_only ON system.query_log")
        client.command(
            "CREATE ROW POLICY avelis_ddl_only ON system.query_log "
            "USING query_kind IN ('Create', 'Alter', 'Drop', 'Rename') TO avelis_collector"
        )
    finally:
        client.close()


def seed_mssql(env: dict[str, str]) -> None:
    """重建 storedb 与两个账号：采集账号只有 store 模式的 VIEW DEFINITION（读目录与视图定义、不能读数据），
    查询账号有 store 模式的 SELECT 与 VIEW DEFINITION（执行前核对要读视图定义）。"""
    users = {
        "avelis_collector": env["SHOP_MSSQL_COLLECTOR_PASSWORD"],
        "avelis_reader": env["SHOP_MSSQL_READER_PASSWORD"],
    }
    conn = mssql_connect(env, database="master")
    try:
        cur = conn.cursor()
        cur.execute(
            "IF DB_ID('storedb') IS NOT NULL BEGIN "
            "ALTER DATABASE storedb SET SINGLE_USER WITH ROLLBACK IMMEDIATE; DROP DATABASE storedb; END"
        )
        cur.execute("CREATE DATABASE storedb")
        for user, password in users.items():  # 口令为字母数字（init-env 生成），可直接写入 DDL
            assert password.isalnum()
            cur.execute(
                f"IF SUSER_ID('{user}') IS NULL CREATE LOGIN {user} WITH PASSWORD = '{password}' "
                f"ELSE ALTER LOGIN {user} WITH PASSWORD = '{password}'"
            )
    finally:
        conn.close()
    conn = mssql_connect(env)
    try:
        cur = conn.cursor()
        for statement in sql_statements(SHOP_MSSQL_SEED):
            cur.execute(statement)
        for user in users:
            cur.execute(f"CREATE USER {user} FOR LOGIN {user}")
        cur.execute("GRANT VIEW DEFINITION ON SCHEMA::store TO avelis_collector")
        cur.execute("GRANT SELECT ON SCHEMA::store TO avelis_reader")
        # 执行前核对要读视图定义：只授予元数据，不额外授予数据权限
        cur.execute("GRANT VIEW DEFINITION ON SCHEMA::store TO avelis_reader")
    finally:
        conn.close()


def seed_oracle(env: dict[str, str]) -> None:
    """重建 STORE 模式与两个账号：采集账号只有 CREATE SESSION + SELECT_CATALOG_ROLE（读数据字典、不能读数据），
    查询账号只有 STORE 各对象的 SELECT。"""
    users = {
        "avelis_collector": env["SHOP_ORACLE_COLLECTOR_PASSWORD"],
        "avelis_reader": env["SHOP_ORACLE_READER_PASSWORD"],
    }
    with oracle_connect(env) as conn, conn.cursor() as cur:
        for user in ("STORE", *(u.upper() for u in users)):
            cur.execute(
                "BEGIN EXECUTE IMMEDIATE 'DROP USER " + user + " CASCADE'; "
                "EXCEPTION WHEN OTHERS THEN IF SQLCODE != -1918 THEN RAISE; END IF; END;"
            )
        for statement in sql_statements(SHOP_ORACLE_SEED):
            cur.execute(statement)
        for user, password in users.items():  # 口令为字母数字（init-env 生成），可直接写入 DDL
            assert password.isalnum()
            cur.execute(f'CREATE USER {user} IDENTIFIED BY "{password}"')
            cur.execute(f"GRANT CREATE SESSION TO {user}")
        cur.execute("GRANT SELECT_CATALOG_ROLE TO avelis_collector")
        for table in ("shops", "sales", "returns", "city_daily_sales"):
            cur.execute(f"GRANT SELECT ON store.{table} TO avelis_reader")


def seed_doris(env: dict[str, str]) -> None:
    """重建 store 库与两个账号：采集账号只有 SHOW_VIEW_PRIV（元数据可见、读不到数据），
    查询账号有 SELECT_PRIV 与 SHOW_VIEW_PRIV（执行前核对要读视图定义）。"""
    conn = doris_connect(env)
    try:
        with conn.cursor() as cur:
            for statement in sql_statements(SHOP_DORIS_SEED):
                cur.execute(statement)
            for user, key, privileges in (
                ("avelis_collector", "SHOP_DORIS_COLLECTOR_PASSWORD", "SHOW_VIEW_PRIV"),
                ("avelis_reader", "SHOP_DORIS_READER_PASSWORD", "SELECT_PRIV, SHOW_VIEW_PRIV"),
            ):
                assert env[key].isalnum()  # init-env 生成的口令为字母数字，可直接写入 DDL
                cur.execute(f"DROP USER IF EXISTS '{user}'@'%'")
                cur.execute(f"CREATE USER '{user}'@'%' IDENTIFIED BY '{env[key]}'")
                cur.execute(f"GRANT {privileges} ON internal.store.* TO '{user}'@'%'")
            # DDL 归因（ADR 0008）：采集账号读内置审计表（Doris 不支持行级限制，能看到全部语句）
            cur.execute("GRANT SELECT_PRIV ON internal.__internal_schema.audit_log TO 'avelis_collector'@'%'")
    finally:
        conn.close()


def seed_engines(env: dict[str, str]) -> None:
    log("把更多引擎的演示库重置到验收基线 ...")
    running = engines_running(env)
    missing = [k for k, ok in running.items() if not ok]
    if missing:
        sys.exit(f"引擎演示库未启动：{', '.join(missing)}；先运行 uv run scripts/demo.py up --engines")
    seed_clickhouse(env)
    seed_mssql(env)
    seed_oracle(env)
    seed_doris(env)
    log("  灌数完成：ClickHouse store、SQL Server storedb.store、Oracle FREEPDB1.STORE、Doris store")


ENGINE_BASELINE = [
    (
        "clickhouse",
        "门店库 city_daily_sales 2026-09-15 上海（ClickHouse）",
        "SELECT sales_amount FROM store.city_daily_sales WHERE sale_date = '2026-09-15' AND city = '上海'",
        Decimal("250.00"),
    ),
    (
        "clickhouse",
        "门店库 city_daily_sales 2026-09-15 北京（ClickHouse）",
        "SELECT sales_amount FROM store.city_daily_sales WHERE sale_date = '2026-09-15' AND city = '北京'",
        Decimal("80.00"),
    ),
    (
        "sqlserver",
        "门店库 city_daily_sales 2026-09-15 上海（SQL Server）",
        "SELECT sales_amount FROM store.city_daily_sales WHERE sale_date = '2026-09-15' AND city = N'上海'",
        Decimal("250.00"),
    ),
    (
        "sqlserver",
        "门店库 city_daily_sales 2026-09-15 北京（SQL Server）",
        "SELECT sales_amount FROM store.city_daily_sales WHERE sale_date = '2026-09-15' AND city = N'北京'",
        Decimal("80.00"),
    ),
    (
        "doris",
        "门店库 city_daily_sales 2026-09-15 上海（Doris）",
        "SELECT sales_amount FROM store.city_daily_sales WHERE sale_date = '2026-09-15' AND city = '上海'",
        Decimal("250.00"),
    ),
    (
        "doris",
        "门店库 city_daily_sales 2026-09-15 北京（Doris）",
        "SELECT sales_amount FROM store.city_daily_sales WHERE sale_date = '2026-09-15' AND city = '北京'",
        Decimal("80.00"),
    ),
    (
        "oracle",
        "门店库 city_daily_sales 2026-09-15 上海（Oracle）",
        "SELECT TO_CHAR(SUM(sales_amount), 'FM999990.00') FROM store.city_daily_sales "
        "WHERE sale_date >= DATE '2026-09-15' AND sale_date < DATE '2026-09-16' AND city = '上海'",
        Decimal("250.00"),
    ),
    (
        "oracle",
        "门店库 city_daily_sales 2026-09-15 北京（Oracle；下单时间 10:30）",
        "SELECT TO_CHAR(SUM(sales_amount), 'FM999990.00') FROM store.city_daily_sales "
        "WHERE sale_date >= DATE '2026-09-15' AND sale_date < DATE '2026-09-16' AND city = '北京'",
        Decimal("80.00"),
    ),
]


def _engine_value(env: dict[str, str], engine: str, sql: str):
    if engine == "clickhouse":
        client = ch_connect(env, "avelis_reader")
        try:
            rows = client.query(sql).result_rows
        finally:
            client.close()
    elif engine == "doris":
        conn = doris_connect(env, "avelis_reader")
        try:
            with conn.cursor() as cur:
                cur.execute(sql)
                rows = cur.fetchall()
        finally:
            conn.close()
    elif engine == "oracle":
        with oracle_connect(env, "avelis_reader") as conn, conn.cursor() as cur:
            cur.execute(sql)
            rows = cur.fetchall()
    else:
        conn = mssql_connect(env, "avelis_reader")
        try:
            cur = conn.cursor()
            cur.execute(sql)
            rows = cur.fetchall()
        finally:
            conn.close()
    return rows[0][0] if rows else "无数据"


def engine_baseline_values(env: dict[str, str]) -> list[dict]:
    results = []
    for engine, label, sql, expected in ENGINE_BASELINE:
        try:
            actual = _engine_value(env, engine, sql)
        except Exception as exc:  # noqa: BLE001 - reported as a failed baseline line
            actual = f"错误：{exc}"
        results.append(
            {"label": label, "expected": str(expected), "actual": str(actual), "passed": str(actual) == str(expected)}
        )
    return results


def check_engines(env: dict[str, str]) -> bool:
    log("核对更多引擎的验收基线（avelis_reader 只读账号）：")
    results = engine_baseline_values(env)
    for r in results:
        log(f"  {'通过' if r['passed'] else '不符'}  {r['label']}：期望 {r['expected']}，实际 {r['actual']}")
    return all(r["passed"] for r in results)


# --- baseline check ----------------------------------------------------------

BASELINE = [
    (
        "daily_sales 2026-09-15 华东（实付）",
        "SELECT sales_amount FROM shop.daily_sales WHERE order_date = '2026-09-15' AND region = '华东'",
        Decimal("300.00"),
    ),
    (
        "2026-09-15 华东 退款后净额",
        "SELECT SUM(paid_amount - refund_amount) FROM shop.orders WHERE order_date = '2026-09-15' AND region = '华东'",
        Decimal("270.00"),
    ),
    (
        "订单直接关联退款明细后求和（粒度放大）",
        "SELECT SUM(o.paid_amount) FROM shop.orders o JOIN shop.refund_raw r ON o.order_id = r.order_id "
        "WHERE o.order_date = '2026-09-15' AND o.region = '华东'",
        Decimal("400.00"),
    ),
    (
        "订单直接关联订单明细后求和（粒度放大）",
        "SELECT SUM(o.paid_amount) FROM shop.orders o JOIN shop.order_items i ON o.order_id = i.order_id "
        "WHERE o.order_date = '2026-09-15' AND o.region = '华东'",
        Decimal("400.00"),
    ),
    (
        "ads.sales_by_category 2026-09-15 厨具",
        "SELECT item_sales FROM ads.sales_by_category WHERE order_date = '2026-09-15' AND category_name = '厨具'",
        Decimal("270.00"),
    ),
    (
        "ads.member_sales 2026-09-15 gold",
        "SELECT paid_sales FROM ads.member_sales WHERE order_date = '2026-09-15' AND member_level = 'gold'",
        Decimal("250.00"),
    ),
    (
        "ads.region_board 华东 2026-09-15",
        "SELECT sales_amount FROM ads.region_board WHERE region_code = 'EC' AND order_date = '2026-09-15'",
        Decimal("300.00"),
    ),
    ("Catalog 中商品维表行数", f"SELECT COUNT(*) FROM {CATALOG}.shop_dim.dim_product", 4),
]


PG_BASELINE = [
    (
        "门店库 city_daily_sales 2026-09-15 上海（PostgreSQL）",
        "SELECT sales_amount FROM store.city_daily_sales WHERE sale_date = '2026-09-15' AND city = '上海'",
        Decimal("250.00"),
    ),
    (
        "门店库 city_daily_sales 2026-09-15 北京（PostgreSQL）",
        "SELECT sales_amount FROM store.city_daily_sales WHERE sale_date = '2026-09-15' AND city = '北京'",
        Decimal("80.00"),
    ),
]


def baseline_values(env: dict[str, str]) -> list[dict]:
    """用只读账号逐项查询验收基线，返回 期望 / 实际 / 是否通过。"""
    results = []
    with pg_connect(env, "avelis_reader") as pconn:
        for label, sql, expected in PG_BASELINE:
            try:
                row = pconn.execute(sql).fetchone()
                actual = row[0] if row else "无数据"
            except psycopg.Error as exc:
                actual = f"错误：{exc}"
            results.append(
                {
                    "label": label,
                    "expected": str(expected),
                    "actual": str(actual),
                    "passed": str(actual) == str(expected),
                }
            )
    conn = sr_connect(env, "avelis_reader")
    with conn.cursor() as cur:
        for label, sql, expected in BASELINE:
            try:
                cur.execute(sql)
                row = cur.fetchone()
                actual = row[0] if row else "无数据"
            except pymysql.MySQLError as exc:
                actual = f"错误：{exc}"
            results.append(
                {
                    "label": label,
                    "expected": str(expected),
                    "actual": str(actual),
                    "passed": str(actual) == str(expected),
                }
            )
    conn.close()
    return results


def check(env: dict[str, str]) -> bool:
    log("核对验收基线（avelis_reader 只读账号）：")
    results = baseline_values(env)
    for r in results:
        log(f"  {'通过' if r['passed'] else '不符'}  {r['label']}：期望 {r['expected']}，实际 {r['actual']}")
    return all(r["passed"] for r in results)


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init-env")
    sub.add_parser("fetch-driver")
    p_up = sub.add_parser("up")
    p_up.add_argument("--skip-seed", action="store_true")
    p_up.add_argument("--engines", action="store_true", help="同时启动更多引擎的演示库（ClickHouse 等）")
    p_up.add_argument(
        "--no-recreate",
        action="store_true",
        help="容器已存在时不重建（从 worktree 等其他检出目录运行时使用，避免打断共用的演示容器）",
    )
    sub.add_parser("seed")
    sub.add_parser("seed-engines")
    sub.add_parser("check")
    sub.add_parser("verify")
    p_down = sub.add_parser("down")
    p_down.add_argument("--volumes", action="store_true", help="同时删除数据卷")
    args = parser.parse_args()

    if args.cmd == "init-env":
        init_env()
    elif args.cmd == "fetch-driver":
        fetch_driver()
    elif args.cmd == "up":
        up(args.skip_seed, args.engines, args.no_recreate)
    elif args.cmd == "seed-engines":
        env = load_env()
        seed_engines(env)
        sys.exit(0 if check_engines(env) else 1)
    elif args.cmd == "seed":
        env = load_env()
        seed(env)
        sys.exit(0 if check(env) else 1)
    elif args.cmd == "check":
        sys.exit(0 if check(load_env()) else 1)
    elif args.cmd == "verify":
        from sr_probe import run_probes

        report = run_probes(load_env(), sr_connect, mysql_connect)
        STATE.mkdir(exist_ok=True)
        out = STATE / "sr-verify.json"
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        log(f"核验结果已写入 {out.relative_to(ROOT)}")
    elif args.cmd == "down":
        down(args.volumes)


if __name__ == "__main__":
    main()
