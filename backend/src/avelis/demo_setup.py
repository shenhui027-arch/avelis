"""演示环境初始化（只用于本地演示库）：经由 HTTP API 建立用户、业务域、数据源、端点别名并完成首次采集，
从而在界面上从故事 1 开始走查。指标、关联路径、复核等都留给界面操作。

  uv run python -m avelis.bootstrap demo           在现有元数据上补齐缺少的对象
  uv run python -m avelis.bootstrap demo --reset   先把演示 StarRocks 重置到基线、清空元数据库与 Redis 键
  加 --metrics：再发布异常场景走查（M4）所需的三个指标与关联路径——实付销售额、订单数、类目销售额（跨域）
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


def setup(reset: bool, metrics: bool = False) -> None:
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
    if metrics:
        _publish_metrics(client, password, {q: a["id"] for q, a in assets.items()},
                         {code: d["id"] for code, d in domains.items()})
    client.close()
    print(f"演示环境已就绪：{len(existing)} 个用户、2 个业务域、2 个数据源、{len(assets)} 个资产（口令未打印）")


def _publish_metrics(client: TestClient, password: str, assets: dict[str, str], domains: dict[str, str]) -> None:
    """经 API 由负责人创建、复核者批准；关联路径由各业务域负责人批准。已存在的指标跳过。"""
    zhang, zhao, li = (_Api(client, u, password) for u in ("zhang", "zhao", "li"))
    existing = {m["code"] for m in zhang("GET", "/api/metrics")}

    def publish(code: str, name: str, spec: dict) -> None:
        if code in existing:
            return
        metric = zhang("POST", "/api/metrics", json={"code": code, "name": name, "domain_id": domains["trade"]})
        version = zhang("POST", f"/api/metrics/{metric['id']}/versions", json=spec)
        zhang("POST", f"/api/metric-versions/{version['id']}/submit")
        li("POST", f"/api/metric-versions/{version['id']}/approve", json={"note": "演示走查准备"})

    region = [{"name": "region", "column": "region", "label": "区域"}]
    publish("paid_sales", "实付销售额", {
        "description": "订单实付金额按下单日期、区域汇总（不扣退款）", "expression": "SUM(sales_amount)",
        "base_asset_id": assets["default_catalog.shop.daily_sales"], "time_column": "order_date",
        "dimensions": region, "grain": ["order_date", "region"]})
    publish("orders_cnt", "订单数", {
        "description": "订单笔数", "base_asset_id": assets["default_catalog.shop.orders"],
        "expression": "COUNT(DISTINCT order_id)", "time_column": "order_date", "dimensions": region,
        "grain": ["order_date", "region"]})
    if "category_sales" in existing:
        return
    paths = {p["code"]: p for p in zhang("GET", "/api/join-paths")}

    def path(api: _Api, code: str, name: str, left: str, lcols: list[str], right: str, rcols: list[str]) -> dict:
        return paths.get(code) or api("POST", "/api/join-paths", json={
            "code": code, "name": name, "left_asset_id": assets[left], "left_columns": lcols,
            "right_asset_id": assets[right], "right_columns": rcols})

    i_o = path(zhang, "items_orders", "订单明细 → 订单", "default_catalog.shop.order_items", ["order_id"],
               "default_catalog.shop.orders", ["order_id"])
    i_p = path(zhang, "items_product", "订单明细 → 商品（MySQL）", "default_catalog.shop.order_items", ["product_id"],
               "shop_mysql.shop_dim.dim_product", ["product_id"])
    p_c = path(zhao, "product_category", "商品 → 类目", "shop_mysql.shop_dim.dim_product", ["category_id"],
               "shop_mysql.shop_dim.dim_category", ["category_id"])
    for p in (i_o, i_p):
        zhang("POST", f"/api/join-paths/{p['id']}/decisions", json={"domain_id": domains["trade"],
                                                                     "decision": "approved"})
    for p in (i_p, p_c):
        zhao("POST", f"/api/join-paths/{p['id']}/decisions", json={"domain_id": domains["product"],
                                                                    "decision": "approved"})
    publish("category_sales", "类目销售额", {
        "description": "订单明细实付金额按类目汇总", "base_asset_id": assets["default_catalog.shop.order_items"],
        "expression": "SUM(item_amount)", "time_column": "o.order_date",
        "dimensions": [{"name": "category", "column": "c.category_name", "label": "类目"},
                       {"name": "region", "column": "o.region", "label": "区域"}],
        "joins": [{"alias": "o", "join_path_id": i_o["id"]}, {"alias": "p", "join_path_id": i_p["id"]},
                  {"alias": "c", "from": "p", "join_path_id": p_c["id"]}],
        "grain": ["order_date", "category"], "dedupe_basis": "明细 → 订单、商品、类目均为 N:1"})
