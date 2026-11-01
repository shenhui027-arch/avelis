"""演示环境初始化（只用于本地演示库）：经由 HTTP API 建立用户、业务域、数据源、端点别名并完成首次采集，
从而在界面上从故事 1 开始走查。指标、关联路径、复核等都留给界面操作。

  uv run python -m avelis.bootstrap demo           在现有元数据上补齐缺少的对象
  uv run python -m avelis.bootstrap demo --reset   先把演示 StarRocks 重置到基线、清空元数据库与 Redis 键
  加 --metrics：再发布异常场景走查（M4）所需的三个指标与关联路径——实付销售额、订单数、类目销售额（跨域）
  合成电商数据集（scripts/ec_data.py）已灌入时：再建“电商经营”业务域与数据源“电商数仓（StarRocks）”并采集；
  加 --metrics 时同时发布电商数仓视图上的口径（分析智能体 M8 的数据基础）
  合成制造数据集（scripts/mfg_data.py）已灌入时：同样接入“生产运营”域及制造数仓，并按 --metrics 发布口径。
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
TRADE_ASSETS = (
    "default_catalog.shop.orders",
    "default_catalog.shop.order_items",
    "default_catalog.shop.refund_raw",
    "default_catalog.shop.daily_sales",
    "default_catalog.ads.region_board",
    "default_catalog.ads.sales_by_category",
    "default_catalog.ads.member_sales",
)
STORE_ASSETS = ("storedb.store.shops", "storedb.store.sales", "storedb.store.returns", "storedb.store.city_daily_sales")
PRODUCT_ASSETS = (
    "shop_mysql.shop_dim.dim_product",
    "shop_mysql.shop_dim.dim_category",
    "shop_mysql.shop_dim.dim_customer",
    "shop_mysql.shop_dim.dim_region",
    "default_catalog.dim.ext_region",
    "def.shop_dim.dim_product",
    "def.shop_dim.dim_category",
    "def.shop_dim.dim_customer",
    "def.shop_dim.dim_region",
)


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


def ec_dataset_loaded() -> bool:
    """合成电商数据集是否已灌入演示 StarRocks（看数据集版本标记，不读业务数据）。"""
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    import demo  # scripts/demo.py
    import ec_data

    try:
        conn = demo.sr_root(demo.load_env())
    except Exception:  # noqa: BLE001 - 演示库未启动
        return False
    try:
        return bool(ec_data.loaded_checksum(conn))
    finally:
        conn.close()


def mfg_dataset_loaded() -> bool:
    """制造数据集是否已灌入（仅检查版本标记，不读业务数据）。"""
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    import demo
    import mfg_data

    try:
        conn = demo.sr_root(demo.load_env())
    except Exception:  # noqa: BLE001 - 演示库未启动
        return False
    try:
        return bool(mfg_data.loaded_checksum(conn))
    finally:
        conn.close()


def setup(reset: bool, metrics: bool = False, ec: bool | None = False, mfg: bool | None = None) -> None:
    """ec=None 是命令行的自动接入模式，也检查制造数据集；mfg=True 可单独显式接入。
    验收调用默认 ec=False，不自动接入额外数据集，避免改变既有用例的数据源与资产。
    """
    from avelis.api.app import create_app
    from avelis.bootstrap import AUTOMATION_ADMIN, ensure_admin

    if reset:
        reset_demo()
    ensure_admin()
    env = get_settings().env
    password = env["AVELIS_DEMO_USER_PASSWORD"]
    client = TestClient(create_app())
    admin = _Api(client, AUTOMATION_ADMIN, password)

    existing = {u["username"]: u["id"] for u in admin("GET", "/api/users")}
    for username, (name, roles) in USERS.items():
        if username not in existing:
            existing[username] = admin(
                "POST",
                "/api/users",
                json={
                    "username": username,
                    "display_name": name,
                    "password": password,
                    "roles": roles,
                    "must_change_password": False,
                },
            )["id"]  # 演示账号共用 .env 中的口令
    domains = {d["code"]: d for d in admin("GET", "/api/domains")}
    if "trade" not in domains:
        domains["trade"] = admin(
            "POST", "/api/domains", json={"code": "trade", "name": "交易域", "owner_id": existing["zhang"]}
        )
    if "product" not in domains:
        domains["product"] = admin(
            "POST", "/api/domains", json={"code": "product", "name": "商品域", "owner_id": existing["zhao"]}
        )
    zhang, zhao = _Api(client, "zhang", password), _Api(client, "zhao", password)
    if not domains["trade"].get("example_questions"):  # 演示业务域的示例问题（ADR 0010：只写在演示种子里）
        zhang(
            "PUT",
            f"/api/domains/{domains['trade']['id']}/example-questions",
            json={
                "questions": [
                    {"text": t, "mode": "ask"}
                    for t in (
                        "2026-09-15 各区域实付销售额前 2",
                        "2026-09-16 华东区实付销售额 环比",
                        "2026-09-15 华东区实付销售额和订单数",
                    )
                ]
            },
        )
    for member in ("wang", "li", "sun"):
        zhang(
            "PUT",
            f"/api/domains/{domains['trade']['id']}/members",
            json={"user_id": existing[member], "role": "viewer"},
        )
    for member in ("wang", "li", "zhang"):
        zhao(
            "PUT",
            f"/api/domains/{domains['product']['id']}/members",
            json={"user_id": existing[member], "role": "viewer"},
        )

    sources = {d["name"]: d for d in admin("GET", "/api/datasources")}
    mysql = sources.get("电商维表库（MySQL）") or admin(
        "POST",
        "/api/datasources",
        json={
            "name": "电商维表库（MySQL）",
            "kind": "mysql",
            "host": "127.0.0.1",
            "port": int(env["SHOP_MYSQL_PORT"]),
            "options": {"databases": ["shop_dim"]},
            "collect": {"username": "avelis_collector", "password": env["SHOP_MYSQL_COLLECTOR_PASSWORD"]},
        },
    )
    sr = sources.get("演示 StarRocks") or admin(
        "POST",
        "/api/datasources",
        json={
            "name": "演示 StarRocks",
            "kind": "starrocks",
            "host": "127.0.0.1",
            "port": int(env["SR_QUERY_PORT"]),
            "options": {
                "databases": ["shop", "ads", "dim"],
                "catalogs": ["shop_mysql"],
                "catalog_databases": {"shop_mysql": ["shop_dim"]},
            },
            "collect": {"username": "avelis_collector", "password": env["SR_COLLECTOR_PASSWORD"]},
            "query": {"username": "avelis_reader", "password": env["SR_READER_PASSWORD"]},
        },
    )
    if "门店库（PostgreSQL）" not in sources and env.get("SHOP_PG_PORT"):
        admin(
            "POST",
            "/api/datasources",
            json={
                "name": "门店库（PostgreSQL）",
                "kind": "postgres",
                "host": "127.0.0.1",
                "port": int(env["SHOP_PG_PORT"]),
                "options": {"database": "storedb", "schemas": ["store"]},
                "collect": {"username": "avelis_collector", "password": env["SHOP_PG_COLLECTOR_PASSWORD"]},
                "query": {"username": "avelis_reader", "password": env["SHOP_PG_READER_PASSWORD"]},
            },
        )
    if not mysql["aliases"]:
        admin(
            "POST",
            "/api/endpoint-aliases",
            json={
                "datasource_id": mysql["id"],
                "host": "shop-mysql",
                "port": 3306,
                "note": "StarRocks 容器网络内的地址",
            },
        )
    admin("POST", f"/api/datasources/{mysql['id']}/collect?wait=true")
    admin("POST", f"/api/datasources/{sr['id']}/collect?wait=true")
    for d in admin("GET", "/api/datasources"):
        if d["kind"] == "postgres":
            admin("POST", f"/api/datasources/{d['id']}/collect?wait=true")

    assets = {a["qualified"]: a for a in admin("GET", "/api/assets")}
    for qualified in TRADE_ASSETS:
        if qualified in assets and assets[qualified]["domain_id"] != domains["trade"]["id"]:
            zhang("PATCH", f"/api/assets/{assets[qualified]['id']}", json={"domain_id": domains["trade"]["id"]})
    for qualified in STORE_ASSETS:  # 门店库归交易域
        if qualified in assets and assets[qualified]["domain_id"] != domains["trade"]["id"]:
            zhang("PATCH", f"/api/assets/{assets[qualified]['id']}", json={"domain_id": domains["trade"]["id"]})
    for qualified in PRODUCT_ASSETS:
        if qualified in assets and assets[qualified]["domain_id"] != domains["product"]["id"]:
            zhao("PATCH", f"/api/assets/{assets[qualified]['id']}", json={"domain_id": domains["product"]["id"]})
    if metrics:
        _publish_metrics(
            client, password, {q: a["id"] for q, a in assets.items()}, {code: d["id"] for code, d in domains.items()}
        )
    if ec or (ec is None and ec_dataset_loaded()):
        _setup_ec(client, admin, password, existing, env, metrics)
    if mfg or (mfg is None and ec is None and mfg_dataset_loaded()):
        _setup_mfg(client, admin, password, existing, env, metrics)
    assets = admin("GET", "/api/assets")
    print(
        f"演示环境已就绪：{len(existing)} 个用户、{len(admin('GET', '/api/domains'))} 个业务域、"
        f"{len(admin('GET', '/api/datasources'))} 个数据源、{len(assets)} 个资产（密码未打印）"
    )
    client.close()


EC_SOURCE = "电商数仓（StarRocks）"
MFG_SOURCE = "制造数仓（StarRocks）"


def _setup_mfg(
    client: TestClient, admin: _Api, password: str, users: dict[str, str], env: dict[str, str], metrics: bool
) -> None:
    """生产运营域：张敏负责，王强、李娜、赵磊为成员，孙悦不在该域（越权验收）。"""
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    import mfg_data

    zhang = _Api(client, "zhang", password)
    domains = {d["code"]: d for d in admin("GET", "/api/domains")}
    domain = domains.get("mfg") or admin(
        "POST",
        "/api/domains",
        json={
            "code": "mfg",
            "name": "生产运营",
            "owner_id": users["zhang"],
            "description": "合成制造数据集：生产执行、质量、设备维护、物料采购与日汇总数仓",
        },
    )
    for member in ("wang", "li", "zhao"):
        zhang("PUT", f"/api/domains/{domain['id']}/members", json={"user_id": users[member], "role": "viewer"})
    if not domain.get("example_questions"):
        zhang(
            "PUT",
            f"/api/domains/{domain['id']}/example-questions",
            json={
                "questions": [
                    {"text": t, "mode": "analysis"}
                    for t in (
                        "为什么 2026 年 9 月华南工厂的良率比 8 月低？",
                        "华南3号线 2026 年 9 月产量为什么比 8 月少？",
                        "设备型号和单位能耗有什么关系？",
                    )
                ]
            },
        )
    sources = {d["name"]: d for d in admin("GET", "/api/datasources")}
    ds = sources.get(MFG_SOURCE) or admin(
        "POST",
        "/api/datasources",
        json={
            "name": MFG_SOURCE,
            "kind": "starrocks",
            "host": "127.0.0.1",
            "port": int(env["SR_QUERY_PORT"]),
            "options": {"databases": list(mfg_data.DATABASES)},
            "collect": {"username": "avelis_collector", "password": env["SR_COLLECTOR_PASSWORD"]},
            "query": {"username": "avelis_reader", "password": env["SR_READER_PASSWORD"]},
        },
    )
    admin("POST", f"/api/datasources/{ds['id']}/collect?wait=true")
    assets = {a["qualified"]: a for a in admin("GET", "/api/assets", params={"datasource_id": ds["id"]})}
    for asset in assets.values():
        if asset["domain_id"] != domain["id"]:
            zhang("PATCH", f"/api/assets/{asset['id']}", json={"domain_id": domain["id"]})
    if metrics:
        _publish_mfg_metrics(client, password, {q: a["id"] for q, a in assets.items()}, domain["id"])


def _setup_ec(
    client: TestClient, admin: _Api, password: str, users: dict[str, str], env: dict[str, str], metrics: bool
) -> None:
    """电商经营域：负责人张敏，王强、李娜、赵磊可查询（孙悦不在该域，用于越权验收）。"""
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    import ec_data

    zhang = _Api(client, "zhang", password)
    domains = {d["code"]: d for d in admin("GET", "/api/domains")}
    domain = domains.get("ec") or admin(
        "POST",
        "/api/domains",
        json={
            "code": "ec",
            "name": "电商经营",
            "owner_id": users["zhang"],
            "description": "合成电商数据集：门店与客流、交易、商品、会员、营销、供应链、物流、客服",
        },
    )
    for member in ("wang", "li", "zhao"):
        zhang("PUT", f"/api/domains/{domain['id']}/members", json={"user_id": users[member], "role": "viewer"})
    if not domain.get("example_questions"):
        zhang(
            "PUT",
            f"/api/domains/{domain['id']}/example-questions",
            json={
                "questions": [
                    {"text": t, "mode": "analysis"}
                    for t in (
                        "为什么 2026 年 9 月华南区域门店成交额比 8 月低？",
                        "华东区域 9 月线上订单数为什么比 8 月少？",
                        "门店客流和商圈等级、楼层有什么关系？哪些门店值得加投引流？",
                    )
                ]
            },
        )
    sources = {d["name"]: d for d in admin("GET", "/api/datasources")}
    ds = sources.get(EC_SOURCE) or admin(
        "POST",
        "/api/datasources",
        json={
            "name": EC_SOURCE,
            "kind": "starrocks",
            "host": "127.0.0.1",
            "port": int(env["SR_QUERY_PORT"]),
            "options": {"databases": list(ec_data.DATABASES)},
            "collect": {"username": "avelis_collector", "password": env["SR_COLLECTOR_PASSWORD"]},
            "query": {"username": "avelis_reader", "password": env["SR_READER_PASSWORD"]},
        },
    )
    admin("POST", f"/api/datasources/{ds['id']}/collect?wait=true")
    assets = {a["qualified"]: a for a in admin("GET", "/api/assets", params={"datasource_id": ds["id"]})}
    for a in assets.values():
        if a["domain_id"] != domain["id"]:
            zhang("PATCH", f"/api/assets/{a['id']}", json={"domain_id": domain["id"]})
    if metrics:
        _publish_ec_metrics(client, password, {q: a["id"] for q, a in assets.items()}, domain["id"])


def _dims(*pairs: tuple[str, str, str]) -> list[dict]:
    return [{"name": n, "column": c, "label": label} for n, c, label in pairs]


REGION = ("region", "region_name", "区域")
EC_METRICS = [  # 编码、名称、视图、表达式、时间列、维度、粒度、说明
    (
        "ec_gmv",
        "成交额",
        "dws_region_daily",
        "SUM(paid_amount)",
        "order_date",
        _dims(REGION, ("channel", "channel_type", "渠道类型")),
        ["order_date", "region_code", "region_name", "channel_type"],
        "实付金额（不含取消单，未扣退款），按下单日期",
    ),
    (
        "ec_orders",
        "订单数",
        "dws_region_daily",
        "SUM(order_cnt)",
        "order_date",
        _dims(REGION, ("channel", "channel_type", "渠道类型")),
        ["order_date", "region_code", "region_name", "channel_type"],
        "订单笔数（不含取消单）",
    ),
    (
        "ec_store_sales",
        "门店成交额",
        "dws_store_daily",
        "SUM(paid_amount)",
        "stat_date",
        _dims(
            REGION,
            ("store", "store_name", "门店"),
            ("city", "city_name", "城市"),
            ("district", "district_level", "商圈等级"),
            ("floor", "floor", "楼层"),
            ("store_type", "store_type", "门店类型"),
        ),
        ["stat_date", "store_id"],
        "门店渠道实付金额，按客流日期（以门店日客流为主表）",
    ),
    (
        "ec_store_orders",
        "门店订单数",
        "dws_store_daily",
        "SUM(order_cnt)",
        "stat_date",
        _dims(
            REGION,
            ("store", "store_name", "门店"),
            ("city", "city_name", "城市"),
            ("district", "district_level", "商圈等级"),
            ("floor", "floor", "楼层"),
            ("store_type", "store_type", "门店类型"),
        ),
        ["stat_date", "store_id"],
        "门店渠道订单笔数",
    ),
    (
        "ec_visitors",
        "进店客流",
        "dws_store_daily",
        "SUM(visitors)",
        "stat_date",
        _dims(
            REGION,
            ("store", "store_name", "门店"),
            ("city", "city_name", "城市"),
            ("district", "district_level", "商圈等级"),
            ("floor", "floor", "楼层"),
            ("store_type", "store_type", "门店类型"),
        ),
        ["stat_date", "store_id"],
        "客流计数器记录的进店人数",
    ),
    (
        "ec_category_sales",
        "类目销售额",
        "dws_category_daily",
        "SUM(item_amount)",
        "order_date",
        _dims(REGION, ("category", "category_l1", "一级类目")),
        ["order_date", "region_code", "region_name", "category_l1"],
        "订单明细金额（扣活动优惠，不含取消单）",
    ),
    (
        "ec_units",
        "类目销量",
        "dws_category_daily",
        "SUM(units)",
        "order_date",
        _dims(REGION, ("category", "category_l1", "一级类目")),
        ["order_date", "region_code", "region_name", "category_l1"],
        "售出件数（不含取消单）",
    ),
    (
        "ec_ad_spend",
        "投放花费",
        "dws_marketing_daily",
        "SUM(spend)",
        "spend_date",
        _dims(REGION, ("channel", "channel_name", "投放渠道")),
        ["spend_date", "region_code", "channel_code"],
        "付费渠道花费（元）",
    ),
    (
        "ec_ad_orders",
        "投放订单数",
        "dws_marketing_daily",
        "SUM(order_cnt)",
        "spend_date",
        _dims(REGION, ("channel", "channel_name", "投放渠道")),
        ["spend_date", "region_code", "channel_code"],
        "来源渠道归因到该付费渠道的订单（不含取消单）",
    ),
    (
        "ec_ad_gmv",
        "投放成交额",
        "dws_marketing_daily",
        "SUM(paid_amount)",
        "spend_date",
        _dims(REGION, ("channel", "channel_name", "投放渠道")),
        ["spend_date", "region_code", "channel_code"],
        "归因到该付费渠道的实付金额",
    ),
    (
        "ec_refund",
        "退款金额",
        "dws_refund_daily",
        "SUM(refund_amount)",
        "refund_date",
        _dims(REGION),
        ["refund_date", "region_code", "region_name"],
        "退款金额，按退款日期",
    ),
    (
        "ec_delivery_total_hours",
        "配送总时长",
        "dws_logistics_daily",
        "SUM(delivery_hours)",
        "ship_date",
        _dims(REGION),
        ["ship_date", "region_code", "region_name"],
        "发货到签收的小时数合计，按发货日期",
    ),
    (
        "ec_shipments",
        "发货单数",
        "dws_logistics_daily",
        "SUM(shipment_cnt)",
        "ship_date",
        _dims(REGION),
        ["ship_date", "region_code", "region_name"],
        "线上订单发货单数，按发货日期",
    ),
    (
        "ec_tickets",
        "客服工单数",
        "dws_service_daily",
        "SUM(ticket_cnt)",
        "ticket_date",
        _dims(REGION, ("ticket_category", "category", "问题分类")),
        ["ticket_date", "region_code", "region_name", "category"],
        "客服工单数，按创建日期",
    ),
    (
        "ec_satisfaction_sum",
        "满意度评分合计",
        "dws_service_daily",
        "SUM(satisfaction_sum)",
        "ticket_date",
        _dims(REGION, ("ticket_category", "category", "问题分类")),
        ["ticket_date", "region_code", "region_name", "category"],
        "已评价工单的满意度评分之和（1–5 分）",
    ),
    (
        "ec_rated",
        "已评价工单数",
        "dws_service_daily",
        "SUM(rated_cnt)",
        "ticket_date",
        _dims(REGION, ("ticket_category", "category", "问题分类")),
        ["ticket_date", "region_code", "region_name", "category"],
        "有满意度评分的工单数",
    ),
]
EC_UNITS = {  # 口径单位（ADR 0009）：报告里的数字由格式器带上单位
    "ec_gmv": "元",
    "ec_orders": "单",
    "ec_store_sales": "元",
    "ec_store_orders": "单",
    "ec_visitors": "人",
    "ec_category_sales": "元",
    "ec_units": "件",
    "ec_ad_spend": "元",
    "ec_ad_orders": "单",
    "ec_ad_gmv": "元",
    "ec_refund": "元",
    "ec_delivery_total_hours": "小时",
    "ec_shipments": "单",
    "ec_tickets": "单",
    "ec_satisfaction_sum": "分",
    "ec_rated": "单",
    "ec_aov": "元",
    "ec_delivery_hours": "小时",
    "ec_satisfaction": "分",
}
EC_DERIVED = [  # 编码、名称、公式、说明
    ("ec_aov", "客单价", "ec_gmv / ec_orders", "成交额 / 订单数"),
    ("ec_conversion", "进店转化率", "ec_store_orders / ec_visitors", "门店订单数 / 进店客流"),
    ("ec_roi", "投放 ROI", "ec_ad_gmv / ec_ad_spend", "投放成交额 / 投放花费"),
    ("ec_delivery_hours", "平均配送时长", "ec_delivery_total_hours / ec_shipments", "配送总时长 / 发货单数（小时）"),
    ("ec_satisfaction", "平均满意度", "ec_satisfaction_sum / ec_rated", "满意度评分合计 / 已评价工单数（1–5 分）"),
]


MFG_DIMS = _dims(
    ("factory", "factory_name", "工厂"),
    ("region", "region_name", "区域"),
    ("line", "line_name", "产线"),
    ("equipment_model", "model_name", "设备型号"),
    ("product_family", "product_family", "产品族"),
)
MFG_GRAIN = ["stat_date", "line_id"]
MFG_METRICS = [  # 编码、名称、视图、表达式、时间列、维度、粒度、说明；与电商演示口径同一结构
    (
        "mfg_production",
        "产量",
        "dws_production_daily",
        "SUM(production_qty)",
        "stat_date",
        MFG_DIMS,
        MFG_GRAIN,
        "报工总产量，包含良品与不良品；排班停产日为零，按业务日期",
    ),
    (
        "mfg_good",
        "良品数",
        "dws_production_daily",
        "SUM(good_qty)",
        "stat_date",
        MFG_DIMS,
        MFG_GRAIN,
        "报工良品件数，不按质检抽样数代替",
    ),
    (
        "mfg_defects",
        "不良数",
        "dws_production_daily",
        "SUM(defect_qty)",
        "stat_date",
        MFG_DIMS,
        MFG_GRAIN,
        "报工不良件数，产量等于良品数加不良数",
    ),
    (
        "mfg_work_hours",
        "生产工时",
        "dws_production_daily",
        "SUM(work_hours)",
        "stat_date",
        MFG_DIMS,
        MFG_GRAIN,
        "产线有效生产小时数，不是人员工时",
    ),
    (
        "mfg_downtime_minutes",
        "停机分钟",
        "dws_production_daily",
        "SUM(downtime_minutes)",
        "stat_date",
        MFG_DIMS,
        MFG_GRAIN,
        "设备停机记录时长合计，含计划检修；按发生日期",
    ),
    (
        "mfg_downtime_count",
        "停机次数",
        "dws_production_daily",
        "SUM(downtime_count)",
        "stat_date",
        MFG_DIMS,
        MFG_GRAIN,
        "设备停机记录条数，按发生日期",
    ),
    (
        "mfg_energy",
        "能耗",
        "dws_production_daily",
        "SUM(energy_kwh)",
        "stat_date",
        MFG_DIMS,
        MFG_GRAIN,
        "计量表记录的耗电量；缺读为 NULL，需结合实收与应收读数检查完整性",
    ),
    (
        "mfg_energy_readings",
        "能耗实收读数",
        "dws_production_daily",
        "SUM(energy_reading_count)",
        "stat_date",
        MFG_DIMS,
        MFG_GRAIN,
        "每日实际收到的班次能耗读数条数",
    ),
    (
        "mfg_expected_readings",
        "能耗应收读数",
        "dws_production_daily",
        "SUM(expected_reading_count)",
        "stat_date",
        MFG_DIMS,
        MFG_GRAIN,
        "按排班确定的应收能耗读数条数",
    ),
    (
        "mfg_inbound_batches",
        "入库批次数",
        "dws_material_daily",
        "SUM(batch_count)",
        "stat_date",
        _dims(("supplier", "supplier_name", "供应商"), ("material", "material_name", "物料")),
        ["stat_date", "supplier_id", "material_id"],
        "物料入库批次数，按入库日期",
    ),
    (
        "mfg_maintenance_orders",
        "维护工单数",
        "dws_maintenance_daily",
        "SUM(order_count)",
        "stat_date",
        _dims(("factory", "factory_name", "工厂"), ("region", "region_name", "区域")),
        ["stat_date", "factory_id"],
        "计划性与故障性维护工单数，按开始日期，一张工单计一次",
    ),
]
MFG_DERIVED = [
    ("mfg_yield", "良率", "mfg_good / mfg_production", "良品数 / 产量，按期间加总后相除；零产量时无定义"),
    ("mfg_unit_energy", "单位能耗", "mfg_energy / mfg_production", "能耗 / 产量；缺读期间不可视为完整的单位能耗"),
    ("mfg_avg_downtime", "平均停机时长", "mfg_downtime_minutes / mfg_downtime_count", "停机分钟 / 停机次数"),
]
MFG_UNITS = {
    "mfg_production": "件",
    "mfg_good": "件",
    "mfg_defects": "件",
    "mfg_work_hours": "小时",
    "mfg_downtime_minutes": "分钟",
    "mfg_downtime_count": "次",
    "mfg_energy": "千瓦时",
    "mfg_energy_readings": "条",
    "mfg_expected_readings": "条",
    "mfg_inbound_batches": "批",
    "mfg_maintenance_orders": "单",
    "mfg_yield": None,
    "mfg_unit_energy": "千瓦时/件",
    "mfg_avg_downtime": "分钟/次",
}


def _publish_mfg_metrics(client: TestClient, password: str, assets: dict[str, str], domain_id: str) -> None:
    """制造口径：张敏创建、李娜批准，先发布基础口径再锁定派生口径依赖版本；已有编码跳过。"""
    zhang, li = (_Api(client, u, password) for u in ("zhang", "li"))
    existing = {m["code"] for m in zhang("GET", "/api/metrics")}

    def publish(code: str, name: str, spec: dict) -> None:
        if code in existing:
            return
        metric = zhang("POST", "/api/metrics", json={"code": code, "name": name, "domain_id": domain_id})
        version = zhang("POST", f"/api/metrics/{metric['id']}/versions", json=spec)
        zhang("POST", f"/api/metric-versions/{version['id']}/submit")
        li("POST", f"/api/metric-versions/{version['id']}/approve", json={"note": "制造数据集演示口径"})

    for code, name, view, expression, time_column, dims, grain, description in MFG_METRICS:
        publish(
            code,
            name,
            {
                "description": description,
                "base_asset_id": assets[f"default_catalog.mfg_dw.{view}"],
                "expression": expression,
                "time_column": time_column,
                "dimensions": dims,
                "grain": grain,
                "timezone": "Asia/Shanghai",
                "unit": MFG_UNITS[code],
            },
        )
    for code, name, formula, description in MFG_DERIVED:
        publish(
            code,
            name,
            {
                "kind": "derived",
                "description": description,
                "expression": formula,
                "timezone": "Asia/Shanghai",
                "unit": MFG_UNITS[code],
            },
        )


def _publish_ec_metrics(client: TestClient, password: str, assets: dict[str, str], domain_id: str) -> None:
    """电商经营域的口径：张敏创建、李娜批准；派生指标在组成指标发布后提交（锁定其版本）。已存在的跳过。"""
    zhang, li = (_Api(client, u, password) for u in ("zhang", "li"))
    existing = {m["code"] for m in zhang("GET", "/api/metrics")}

    def publish(code: str, name: str, spec: dict) -> None:
        if code in existing:
            return
        metric = zhang("POST", "/api/metrics", json={"code": code, "name": name, "domain_id": domain_id})
        version = zhang("POST", f"/api/metrics/{metric['id']}/versions", json=spec)
        zhang("POST", f"/api/metric-versions/{version['id']}/submit")
        li("POST", f"/api/metric-versions/{version['id']}/approve", json={"note": "电商数据集演示口径"})

    for code, name, view, expression, time_column, dims, grain, description in EC_METRICS:
        publish(
            code,
            name,
            {
                "description": description,
                "base_asset_id": assets[f"default_catalog.ec_dw.{view}"],
                "expression": expression,
                "time_column": time_column,
                "dimensions": dims,
                "grain": grain,
                "unit": EC_UNITS.get(code),
            },
        )
    for code, name, formula, description in EC_DERIVED:
        publish(
            code,
            name,
            {"kind": "derived", "description": description, "expression": formula, "unit": EC_UNITS.get(code)},
        )


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
    publish(
        "paid_sales",
        "实付销售额",
        {
            "description": "订单实付金额按下单日期、区域汇总（不扣退款）",
            "expression": "SUM(sales_amount)",
            "base_asset_id": assets["default_catalog.shop.daily_sales"],
            "time_column": "order_date",
            "dimensions": region,
            "grain": ["order_date", "region"],
            "unit": "元",
        },
    )
    publish(
        "orders_cnt",
        "订单数",
        {
            "description": "订单笔数",
            "base_asset_id": assets["default_catalog.shop.orders"],
            "expression": "COUNT(DISTINCT order_id)",
            "time_column": "order_date",
            "dimensions": region,
            "grain": ["order_date", "region"],
            "unit": "单",
        },
    )
    if "aov" not in existing:  # 派生指标：客单价 = 实付销售额 / 订单数
        metric = zhang("POST", "/api/metrics", json={"code": "aov", "name": "客单价", "domain_id": domains["trade"]})
        version = zhang(
            "POST",
            f"/api/metrics/{metric['id']}/versions",
            json={
                "kind": "derived",
                "description": "实付销售额 / 订单数",
                "expression": "paid_sales / orders_cnt",
                "unit": "元",
            },
        )
        zhang("POST", f"/api/metric-versions/{version['id']}/submit")
        li("POST", f"/api/metric-versions/{version['id']}/approve", json={"note": "演示走查准备"})
    if "category_sales" in existing:
        return
    paths = {p["code"]: p for p in zhang("GET", "/api/join-paths")}

    def path(api: _Api, code: str, name: str, left: str, lcols: list[str], right: str, rcols: list[str]) -> dict:
        return paths.get(code) or api(
            "POST",
            "/api/join-paths",
            json={
                "code": code,
                "name": name,
                "left_asset_id": assets[left],
                "left_columns": lcols,
                "right_asset_id": assets[right],
                "right_columns": rcols,
            },
        )

    i_o = path(
        zhang,
        "items_orders",
        "订单明细 → 订单",
        "default_catalog.shop.order_items",
        ["order_id"],
        "default_catalog.shop.orders",
        ["order_id"],
    )
    i_p = path(
        zhang,
        "items_product",
        "订单明细 → 商品（MySQL）",
        "default_catalog.shop.order_items",
        ["product_id"],
        "shop_mysql.shop_dim.dim_product",
        ["product_id"],
    )
    p_c = path(
        zhao,
        "product_category",
        "商品 → 类目",
        "shop_mysql.shop_dim.dim_product",
        ["category_id"],
        "shop_mysql.shop_dim.dim_category",
        ["category_id"],
    )
    for p in (i_o, i_p):
        zhang(
            "POST", f"/api/join-paths/{p['id']}/decisions", json={"domain_id": domains["trade"], "decision": "approved"}
        )
    for p in (i_p, p_c):
        zhao(
            "POST",
            f"/api/join-paths/{p['id']}/decisions",
            json={"domain_id": domains["product"], "decision": "approved"},
        )
    publish(
        "category_sales",
        "类目销售额",
        {
            "description": "订单明细实付金额按类目汇总",
            "base_asset_id": assets["default_catalog.shop.order_items"],
            "expression": "SUM(item_amount)",
            "time_column": "o.order_date",
            "dimensions": [
                {"name": "category", "column": "c.category_name", "label": "类目"},
                {"name": "region", "column": "o.region", "label": "区域"},
            ],
            "joins": [
                {"alias": "o", "join_path_id": i_o["id"]},
                {"alias": "p", "join_path_id": i_p["id"]},
                {"alias": "c", "from": "p", "join_path_id": p_c["id"]},
            ],
            "grain": ["order_date", "category"],
            "dedupe_basis": "明细 → 订单、商品、类目均为 N:1",
        },
    )
