"""演示环境初始化（只用于本地演示库）：经由 HTTP API 建立用户、业务域、数据源、端点别名并完成首次采集，
从而在界面上从故事 1 开始走查。指标、关联路径、复核等都留给界面操作。

  uv run python -m avelis.bootstrap demo           在现有元数据上补齐缺少的对象
  uv run python -m avelis.bootstrap demo --reset   先把演示 StarRocks 重置到基线、清空元数据库与 Redis 键
"""

from __future__ import annotations

import sys

from fastapi.testclient import TestClient

from avelis.config import REPO_ROOT, get_settings

USERS = {
    "zhang": ("张敏", ["steward"]),
    "zhao": ("赵磊", ["steward"]),
    "li": ("李娜", ["reviewer"]),
    "wang": ("王强", ["analyst"]),
    "sun": ("孙悦", ["analyst"]),
}
TRADE_ASSETS = ("default_catalog.shop.orders", "default_catalog.shop.order_items", "default_catalog.shop.refund_raw",
                "default_catalog.shop.daily_sales", "default_catalog.ads.region_board",
                "default_catalog.ads.sales_by_category", "default_catalog.ads.member_sales")
PRODUCT_ASSETS = ("shop_mysql.shop_dim.dim_product", "shop_mysql.shop_dim.dim_category",
                  "shop_mysql.shop_dim.dim_customer", "shop_mysql.shop_dim.dim_region",
                  "default_catalog.dim.ext_region", "def.shop_dim.dim_product", "def.shop_dim.dim_category",
                  "def.shop_dim.dim_customer", "def.shop_dim.dim_region")


class _Api:
    def __init__(self, client: TestClient, username: str, password: str):
        self.client = client
        resp = client.post("/api/auth/token", data={"username": username, "password": password})
        resp.raise_for_status()
        self.headers = {"Authorization": f"Bearer {resp.json()['access_token']}"}

    def __call__(self, method: str, path: str, **kwargs):
        resp = self.client.request(method, path, headers=self.headers, **kwargs)
        if resp.status_code >= 400:
            raise RuntimeError(f"{method} {path} → {resp.status_code}: {resp.text[:300]}")
        return resp.json() if resp.content else None


def reset_demo() -> None:
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    import demo  # scripts/demo.py

    from avelis import cache
    from avelis.devtools import reset_metadata_db

    env = demo.load_env()
    demo.seed(env)
    if not demo.check(env):
        raise SystemExit("演示库基线不符")
    reset_metadata_db()
    keys = list(cache.client().scan_iter("avelis:*"))
    if keys:
        cache.client().delete(*keys)


def setup(reset: bool) -> None:
    from avelis.api.app import create_app
    from avelis.bootstrap import ensure_admin

    if reset:
        reset_demo()
    ensure_admin()
    env = get_settings().env
    password = env["AVELIS_DEMO_USER_PASSWORD"]
    client = TestClient(create_app())
    admin = _Api(client, "admin", password)

    existing = {u["username"]: u["id"] for u in admin("GET", "/api/users")}
    for username, (name, roles) in USERS.items():
        if username not in existing:
            existing[username] = admin("POST", "/api/users", json={
                "username": username, "display_name": name, "password": password, "roles": roles})["id"]
    domains = {d["code"]: d for d in admin("GET", "/api/domains")}
    if "trade" not in domains:
        domains["trade"] = admin("POST", "/api/domains", json={"code": "trade", "name": "交易域",
                                                               "owner_id": existing["zhang"]})
    if "product" not in domains:
        domains["product"] = admin("POST", "/api/domains", json={"code": "product", "name": "商品域",
                                                                 "owner_id": existing["zhao"]})
    zhang, zhao = _Api(client, "zhang", password), _Api(client, "zhao", password)
    for member in ("wang", "li", "sun"):
        zhang("PUT", f"/api/domains/{domains['trade']['id']}/members",
              json={"user_id": existing[member], "role": "viewer"})
    for member in ("wang", "li", "zhang"):
        zhao("PUT", f"/api/domains/{domains['product']['id']}/members",
             json={"user_id": existing[member], "role": "viewer"})

    sources = {d["name"]: d for d in admin("GET", "/api/datasources")}
    mysql = sources.get("电商维表库（MySQL）") or admin("POST", "/api/datasources", json={
        "name": "电商维表库（MySQL）", "kind": "mysql", "host": "127.0.0.1", "port": int(env["SHOP_MYSQL_PORT"]),
        "options": {"databases": ["shop_dim"]},
        "collect": {"username": "avelis_collector", "password": env["SHOP_MYSQL_COLLECTOR_PASSWORD"]}})
    sr = sources.get("演示 StarRocks") or admin("POST", "/api/datasources", json={
        "name": "演示 StarRocks", "kind": "starrocks", "host": "127.0.0.1", "port": int(env["SR_QUERY_PORT"]),
        "options": {"databases": ["shop", "ads", "dim"], "catalogs": ["shop_mysql"],
                    "catalog_databases": {"shop_mysql": ["shop_dim"]}},
        "collect": {"username": "avelis_collector", "password": env["SR_COLLECTOR_PASSWORD"]},
        "query": {"username": "avelis_reader", "password": env["SR_READER_PASSWORD"]}})
    if not mysql["aliases"]:
        admin("POST", "/api/endpoint-aliases", json={"datasource_id": mysql["id"], "host": "shop-mysql",
                                                     "port": 3306, "note": "StarRocks 容器网络内的地址"})
    admin("POST", f"/api/datasources/{mysql['id']}/collect?wait=true")
    admin("POST", f"/api/datasources/{sr['id']}/collect?wait=true")

    assets = {a["qualified"]: a for a in admin("GET", "/api/assets")}
    for qualified in TRADE_ASSETS:
        if qualified in assets and assets[qualified]["domain_id"] != domains["trade"]["id"]:
            zhang("PATCH", f"/api/assets/{assets[qualified]['id']}", json={"domain_id": domains["trade"]["id"]})
    for qualified in PRODUCT_ASSETS:
        if qualified in assets and assets[qualified]["domain_id"] != domains["product"]["id"]:
            zhao("PATCH", f"/api/assets/{assets[qualified]['id']}", json={"domain_id": domains["product"]["id"]})
    client.close()
    print(f"演示环境已就绪：{len(existing)} 个用户、2 个业务域、2 个数据源、{len(assets)} 个资产（口令未打印）")
