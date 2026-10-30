"""合成电商数据集（ADR 0007 第 10 点）：覆盖多个业务域的电商系统表 + 数仓视图，灌入演示 StarRocks。

在仓库根目录运行：
  uv run scripts/ec_data.py load [--force]   生成并灌数（数据已一致时跳过；--force 强制重建）
  uv run scripts/ec_data.py truth            打印预埋场景的标准答案（由生成数据直接计算，不查数据库）
  uv run scripts/ec_data.py check            用只读账号核对库中数据与生成结果一致

确定性：固定随机种子与生成规则，同一 VERSION 生成的数据逐行相同，验收的期望值可以固定。
与验收基线库（shop / ads / dim）分开：demo.py seed 不重建这套数据，只重新授权。
时间范围 2026-06-01 至 2026-09-30（业务时区 Asia/Shanghai）。

预埋的业务事实（SCENARIOS，评测用标准答案，不提供给模型）：
  1. 深圳“福田中心店”2026-09-10 关店：华南 9 月门店订单下降。
  2. 家电“焕新季”2026-09-01 至 09-20 打 85 折：家电件数上升、客单价下降。
  3. 华东抖音投放 2026-09-01 起削减 60%：华东线上订单与次日门店客流下降。
  4. 中秋 2026-09-25 至 09-27：客流与订单峰值；食品礼盒 09-15 至 09-25 走高。
  5. 西南仓 2026-08-05 至 08-20 暴雨：配送时长增加，客服工单上升、满意度下降。
  6. 成都“太古里店”2026-08-01 新开：西南 8、9 月门店订单增长。
  7. 华北 3 家门店 2026-09-28 客流计数器故障：当天没有客流记录（数据缺口，订单正常）。
     ec_dw.dws_store_daily 以客流为主表，这 3 家店当天的订单在该视图中随之缺失，而 dws_region_daily
     （以订单为主表）不受影响：同一天两个口径的差异要靠血缘定位。
  8. 数码类部分商品 2026-08-15 起调价 +8%。
  9. 客流由商圈等级、楼层、面积决定；转化率社区店最高、核心商圈最低。
"""

from __future__ import annotations

import argparse
import bisect
import hashlib
import json
import math
import random
import re
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import pymysql

import demo

VERSION = "ec-1"
SEED = 20261029
START, END = date(2026, 6, 1), date(2026, 9, 30)
DATABASES = ("ec_org", "ec_product", "ec_member", "ec_trade", "ec_marketing", "ec_supply", "ec_logistics",
             "ec_service", "ec_dw")
TRUTH_FILE = demo.STATE / "ec-truth.json"
CENT = Decimal("0.01")


def money(value: float | Decimal) -> Decimal:
    return Decimal(str(value)).quantize(CENT, rounding=ROUND_HALF_UP)


def days() -> list[date]:
    return [START + timedelta(days=i) for i in range((END - START).days + 1)]


# --- 维度定义 -----------------------------------------------------------------------

REGIONS = [("EC", "华东"), ("NC", "华北"), ("SC", "华南"), ("SW", "西南")]
CITIES = [  # 城市编码、名称、区域、城市等级
    ("SH", "上海", "EC", "一线"), ("HZ", "杭州", "EC", "新一线"), ("NJ", "南京", "EC", "新一线"),
    ("SZH", "苏州", "EC", "新一线"), ("BJ", "北京", "NC", "一线"), ("TJ", "天津", "NC", "新一线"),
    ("GZ", "广州", "SC", "一线"), ("SZ", "深圳", "SC", "一线"), ("XM", "厦门", "SC", "二线"),
    ("CD", "成都", "SW", "新一线"), ("CQ", "重庆", "SW", "新一线"), ("KM", "昆明", "SW", "二线"),
]
STORES_PER_CITY = {"SH": 5, "HZ": 3, "NJ": 3, "SZH": 3, "BJ": 5, "TJ": 3, "GZ": 4, "SZ": 4, "XM": 2,
                   "CD": 3, "CQ": 3, "KM": 2}
DISTRICTS = {"核心商圈": (200, 0.055), "次级商圈": (130, 0.070), "社区": (80, 0.095)}  # 日均客流基数、转化率
FLOORS = {"B1": (1.00, 1.05), "1F": (1.15, 1.00), "2F": (0.85, 0.95), "3F": (0.70, 0.90), "街铺": (1.05, 1.00)}
CATEGORIES = {  # 一级类目 → (二级类目, 价格区间, 销量权重)
    "服饰": (["男装", "女装", "童装"], (59, 599), 1.6),
    "美妆": (["护肤", "彩妆"], (49, 899), 1.3),
    "食品": (["休闲零食", "礼盒", "饮品"], (9, 299), 2.0),
    "家电": (["厨房电器", "生活电器"], (199, 3999), 0.6),
    "数码": (["手机配件", "智能设备"], (29, 2999), 0.8),
    "家居": (["家纺", "收纳"], (19, 899), 1.0),
    "母婴": (["奶粉", "用品"], (39, 499), 0.7),
    "运动": (["运动鞋服", "户外"], (79, 1299), 0.9),
}
PAID_CHANNELS = [("douyin", "抖音", 45.0, 0.35), ("xiaohongshu", "小红书", 60.0, 0.25),
                 ("wechat_ads", "微信广告", 50.0, 0.25), ("search", "搜索广告", 40.0, 0.15)]  # 编码、名称、获客成本、预算占比
REGION_SPEND = {"EC": 8000, "NC": 5000, "SC": 6000, "SW": 4000}  # 日投放预算（元）
REGION_ORGANIC = {"EC": 90, "NC": 60, "SC": 70, "SW": 45}  # 日自然流量线上订单
HOLIDAYS = {date(2026, 6, 19): 1.3, date(2026, 6, 20): 1.3, date(2026, 6, 21): 1.25, date(2026, 6, 18): 1.4,
            date(2026, 8, 19): 1.2, date(2026, 9, 25): 1.6, date(2026, 9, 26): 1.6, date(2026, 9, 27): 1.5}
CAMPAIGNS = [  # 编号、名称、类型、开始、结束、限定一级类目、折扣率、预算
    (1, "618 年中大促", "大促", date(2026, 6, 1), date(2026, 6, 18), None, Decimal("0.90"), 600000),
    (2, "七夕美妆节", "品类节", date(2026, 8, 12), date(2026, 8, 19), "美妆", Decimal("0.92"), 120000),
    (3, "家电焕新季", "品类节", date(2026, 9, 1), date(2026, 9, 20), "家电", Decimal("0.85"), 300000),
    (4, "中秋礼盒季", "节日", date(2026, 9, 15), date(2026, 9, 25), "食品", Decimal("0.95"), 150000),
]
CLOSED_STORE = ("SZ", 2, date(2026, 9, 10), "福田中心店")  # 城市、序号、关店日期、店名
NEW_STORE = ("CD", 3, date(2026, 8, 1), "太古里店")
DOUYIN_CUT = ("EC", date(2026, 9, 1), 0.40)  # 区域、起始日、预算保留比例
STORM = ("SW", date(2026, 8, 5), date(2026, 8, 20))
TRAFFIC_GAP = ("BJ", date(2026, 9, 28))  # 北京 3 家门店当天无客流记录
PRICE_UP = (date(2026, 8, 15), Decimal("1.08"))


# --- 生成 ---------------------------------------------------------------------------

@dataclass
class Dataset:
    tables: dict[str, list[tuple]] = field(default_factory=dict)
    truth: dict = field(default_factory=dict)

    def add(self, table: str, row: tuple) -> None:
        self.tables.setdefault(table, []).append(row)


def generate() -> Dataset:
    rng = random.Random(SEED)
    ds = Dataset()
    for code, name in REGIONS:
        ds.add("ec_org.region", (code, name))
    for code, name, region, tier in CITIES:
        ds.add("ec_org.city", (code, name, region, tier))

    # 门店
    stores = []
    store_id = 100
    for code, city_name, region, tier in CITIES:
        for i in range(1, STORES_PER_CITY[code] + 1):
            store_id += 1
            district = rng.choices(list(DISTRICTS), weights=[3, 4, 3])[0]
            floor = rng.choice(list(FLOORS))
            area = rng.randrange(120, 620, 20)
            open_date = date(2019, 1, 1) + timedelta(days=rng.randrange(0, 2000))
            close_date = None
            name = f"{city_name}{['一', '二', '三', '四', '五'][i - 1]}号店"
            if (code, i) == CLOSED_STORE[:2]:
                name, close_date, district, floor = CLOSED_STORE[3], CLOSED_STORE[2], "核心商圈", "1F"
            if (code, i) == NEW_STORE[:2]:
                name, open_date, district, floor = NEW_STORE[3], NEW_STORE[2], "核心商圈", "街铺"
            store_type = "旗舰店" if area >= 480 else ("标准店" if area >= 220 else "社区店")
            stores.append(dict(id=store_id, name=f"{city_name}{name}" if not name.startswith(city_name) else name,
                               city=code, region=region, district=district, floor=floor, area=area,
                               open=open_date, close=close_date, type=store_type))
    for s in stores:
        ds.add("ec_org.store", (s["id"], s["name"], s["city"], f"{s['name'][:2]}{s['district']}", s["district"],
                                s["floor"], s["area"], s["type"], s["open"], s["close"],
                                "closed" if s["close"] else "open"))

    # 商品
    category_id, product_id, brand_names = 0, 100000, {}
    products = []
    leaf_of: dict[int, str] = {}
    for l1, (l2s, (low, high), weight) in CATEGORIES.items():
        category_id += 1
        l1_id = category_id
        ds.add("ec_product.category", (l1_id, l1, None, 1))
        for l2 in l2s:
            category_id += 1
            ds.add("ec_product.category", (category_id, l2, l1_id, 2))
            leaf_of[category_id] = l1
            for b in range(3):
                brand = f"{l2}{'甲乙丙'[b]}牌"
                brand_names.setdefault(brand, len(brand_names) + 1)
            for _ in range(rng.randint(14, 22)):
                product_id += 1
                price = money(math.exp(rng.uniform(math.log(low), math.log(high))))
                price = money(int(price) + Decimal("0.90")) if price > 10 else price
                brand = f"{l2}{'甲乙丙'[rng.randrange(3)]}牌"
                products.append(dict(id=product_id, l1=l1, l2_id=category_id, brand=brand_names[brand], price=price,
                                     cost=money(price * Decimal(str(rng.uniform(0.55, 0.75)))), weight=weight))
    for name, bid in brand_names.items():
        ds.add("ec_product.brand", (bid, name))
    digital = [p for p in products if p["l1"] == "数码"]
    raised = {p["id"] for p in rng.sample(digital, k=len(digital) // 3)}
    for p in products:
        ds.add("ec_product.product", (p["id"], f"{CATEGORIES[p['l1']][0][0]}商品{p['id']}", p["l2_id"], p["brand"],
                                      p["price"], p["cost"], date(2025, 1, 1) + timedelta(days=rng.randrange(400)),
                                      "on_sale"))
        if p["id"] in raised:
            ds.add("ec_product.price_change", (p["id"], PRICE_UP[0], p["price"], money(p["price"] * PRICE_UP[1]),
                                               "成本上涨"))

    # 会员
    city_weights = {"一线": 3, "新一线": 2, "二线": 1}
    customers = []
    for cid in range(1, 30001):
        city = rng.choices(CITIES, weights=[city_weights[c[3]] for c in CITIES])[0]
        reg = date(2023, 1, 1) + timedelta(days=int(rng.triangular(0, (END - date(2023, 1, 1)).days, 900)))
        level = rng.choices(["普通", "银卡", "金卡", "黑金"], weights=[60, 25, 12, 3])[0]
        customers.append((cid, city[0], city[2], reg))
        ds.add("ec_member.customer", (cid, reg, level, rng.choice(["男", "女"]), rng.randint(1965, 2006), city[0],
                                      rng.choice(["app", "mini_program", "store"])))
        if rng.random() < 0.1:
            ds.add("ec_member.member_level_change", (cid, START + timedelta(days=rng.randrange(122)), "普通", level))
    by_region: dict[str, list[tuple]] = {}
    for c in sorted(customers, key=lambda c: (c[3], c[0])):
        by_region.setdefault(c[2], []).append(c)
    reg_dates = {r: [c[3] for c in cs] for r, cs in by_region.items()}

    def member(region: str, d: date) -> tuple:
        """该区域在 d 当天及以前注册的会员中随机取一位。"""
        k = bisect.bisect_right(reg_dates[region], d) or len(by_region[region])
        return by_region[region][rng.randrange(k)]

    # 营销：渠道、活动、优惠券、投放
    for code, name, _, _ in PAID_CHANNELS:
        ds.add("ec_marketing.channel", (code, name, "付费"))
    ds.add("ec_marketing.channel", ("organic", "自然流量", "免费"))
    ds.add("ec_marketing.channel", ("store", "门店", "线下"))
    for c in CAMPAIGNS:
        ds.add("ec_marketing.campaign", (c[0], c[1], c[2], c[3], c[4], c[5], c[6], c[7]))
    coupons = []
    for cid, (face, threshold) in enumerate([(10, 99), (20, 199), (50, 399)], start=1):
        coupons.append((cid, Decimal(face), Decimal(threshold)))
        ds.add("ec_marketing.coupon", (cid, None, Decimal(face), Decimal(threshold), START, END))
    spend: dict[tuple[date, str, str], Decimal] = {}
    for d in days():
        for region, budget in REGION_SPEND.items():
            promo = 1.6 if d <= date(2026, 6, 18) else 1.0
            for code, _, cpa, share in PAID_CHANNELS:
                amount = budget * share * promo * rng.uniform(0.85, 1.15)
                if code == "douyin" and region == DOUYIN_CUT[0] and d >= DOUYIN_CUT[1]:
                    amount *= DOUYIN_CUT[2]
                amount = money(amount)
                spend[(d, region, code)] = amount
                clicks = int(float(amount) / rng.uniform(0.8, 1.6))
                ds.add("ec_marketing.ad_spend_daily", (d, code, region, int(clicks * rng.uniform(18, 40)), clicks,
                                                       amount))

    # 客流与订单
    order_id, item_id, payment_id, refund_id, shipment_id, ticket_id = 10_000_000, 50_000_000, 0, 0, 0, 0
    warehouses = {"EC": 1, "NC": 2, "SC": 3, "SW": 4}
    for code, name in REGIONS:
        ds.add("ec_supply.warehouse", (warehouses[code], f"{name}中心仓", code))
    base_spend = {r: sum(REGION_SPEND[r] * c[3] for c in PAID_CHANNELS) for r in REGION_SPEND}
    gap_stores = [s["id"] for s in stores if s["city"] == TRAFFIC_GAP[0]][:3]

    day_weights: dict[date, tuple[list, list[float]]] = {}

    def weights_on(d: date) -> tuple[list, list[float]]:
        if d not in day_weights:
            active = [c for c in CAMPAIGNS if c[3] <= d <= c[4]]
            cum, total_w = [], 0.0
            for p in products:
                w = p["weight"]
                for c in active:
                    if c[5] is None:
                        w *= 1.1
                    elif c[5] == p["l1"]:
                        w *= {"家电": 2.2, "美妆": 1.8, "食品": 2.0}.get(p["l1"], 1.5)
                total_w += w
                cum.append(total_w)
            day_weights[d] = (active, cum)
        return day_weights[d]

    def pick_items(d: date) -> list[tuple[dict, int, Decimal, Decimal]]:
        """一单的商品：类目权重随活动变化；返回（商品，件数，单价，折扣额）。"""
        n = rng.choices([1, 2, 3, 4], weights=[55, 28, 12, 5])[0]
        active, cum = weights_on(d)
        out = []
        for p in rng.choices(products, cum_weights=cum, k=n):
            qty = rng.choices([1, 2, 3], weights=[80, 15, 5])[0]
            price = p["price"] * PRICE_UP[1] if p["id"] in raised and d >= PRICE_UP[0] else p["price"]
            price = money(price)
            rate = Decimal("1")
            for c in active:
                if c[5] is None or c[5] == p["l1"]:
                    rate = min(rate, c[6])
            discount = money(price * qty * (Decimal("1") - rate))
            out.append((p, qty, price, discount))
        return out

    def add_order(d: date, region: str, city: str | None, store: dict | None, channel: str, platform: str,
                  customer: int | None) -> None:
        nonlocal order_id, item_id, payment_id, refund_id, shipment_id, ticket_id
        order_id += 1
        items = pick_items(d)
        hour = rng.choices(range(8, 23), weights=[1, 2, 3, 4, 5, 5, 4, 4, 5, 6, 7, 8, 8, 6, 3])[0]
        at = datetime(d.year, d.month, d.day, hour, rng.randrange(60), rng.randrange(60))
        goods = sum((price * qty for _, qty, price, _ in items), Decimal("0"))
        discount = sum((disc for *_, disc in items), Decimal("0"))
        coupon_amount, coupon_id = Decimal("0"), None
        if customer and rng.random() < 0.12:
            cid, face, threshold = rng.choice(coupons)
            if goods - discount >= threshold:
                coupon_amount, coupon_id = face, cid
        shipping = Decimal("8") if store is None and goods - discount < 99 else Decimal("0")
        paid = goods - discount - coupon_amount + shipping
        r = rng.random()
        status = "cancelled" if r < 0.025 else ("refunded" if r < 0.055 else "paid")
        ds.add("ec_trade.orders", (order_id, at, d, customer, store["id"] if store else None, region, city,
                                   "门店" if store else "线上", platform, channel, status, goods, discount,
                                   coupon_amount, shipping, paid, rng.choice(["微信支付", "支付宝", "银行卡"])))
        for p, qty, price, disc in items:
            item_id += 1
            ds.add("ec_trade.order_item", (item_id, order_id, p["id"], qty, price, disc, price * qty - disc))
        if coupon_id:
            ds.add("ec_marketing.coupon_usage", (coupon_id, customer, order_id, at))
        if status == "cancelled":
            return
        payment_id += 1
        ds.add("ec_trade.payment", (payment_id, order_id, at + timedelta(seconds=rng.randrange(5, 120)),
                                    paid, "success"))
        if status == "refunded" or rng.random() < 0.03:
            refund_id += 1
            amount = paid if status == "refunded" else money(paid * Decimal(str(rng.uniform(0.1, 0.5))))
            ds.add("ec_trade.refund", (refund_id, order_id, at + timedelta(days=rng.randint(1, 7)), amount,
                                       rng.choice(["质量问题", "不想要了", "发错货", "物流太慢"])))
        storm = region == STORM[0] and STORM[1] <= d <= STORM[2]
        if store is None:
            shipment_id += 1
            ship_at = at + timedelta(hours=rng.uniform(2, 20))
            hours = rng.uniform(24, 60) + (rng.uniform(36, 72) if storm else 0)
            ds.add("ec_logistics.shipment", (shipment_id, order_id, warehouses[region],
                                             rng.choice(["顺丰", "京东物流", "中通", "圆通"]), ship_at,
                                             ship_at + timedelta(hours=hours), "delivered"))
        if customer and rng.random() < (0.05 if storm and store is None else 0.012):
            ticket_id += 1
            satisfaction = rng.choices([1, 2, 3, 4, 5], weights=[3, 3, 3, 1, 1] if storm else [1, 1, 2, 4, 6])[0]
            ds.add("ec_service.service_ticket", (ticket_id, order_id, customer, at + timedelta(days=rng.randint(1, 4)),
                                                 rng.choice(["物流", "退换货", "商品咨询", "发票"]) if not storm else "物流",
                                                 satisfaction))

    for d in days():
        weekday = {4: 1.1, 5: 1.4, 6: 1.35}.get(d.weekday(), 1.0)
        holiday = HOLIDAYS.get(d, 1.0)
        prev = d - timedelta(days=1)
        for s in stores:
            if d < s["open"] or (s["close"] and d >= s["close"]):
                continue
            base, conv = DISTRICTS[s["district"]]
            floor_traffic, floor_conv = FLOORS[s["floor"]]
            paid_prev = sum(spend.get((prev, s["region"], c[0]), Decimal("0")) for c in PAID_CHANNELS)
            lift = 1 + 0.3 * (float(paid_prev) / base_spend[s["region"]] - 1) if paid_prev else 1
            visitors = int(base * floor_traffic * math.sqrt(s["area"] / 300) * weekday * holiday * lift
                           * rng.gauss(1, 0.05))
            orders_n = max(0, round(visitors * conv * floor_conv * rng.gauss(1, 0.08)))
            if not (s["id"] in gap_stores and d == TRAFFIC_GAP[1]):
                ds.add("ec_org.store_daily_traffic", (s["id"], d, visitors, int(visitors * rng.uniform(0.25, 0.35))))
            for _ in range(orders_n):
                customer = member(s["region"], d)[0] if rng.random() < 0.65 else None
                add_order(d, s["region"], s["city"], s, "store", "门店", customer)
        for region in REGION_SPEND:
            for code, _, cpa, _ in PAID_CHANNELS:
                n = round(float(spend[(d, region, code)]) / cpa * weekday * rng.gauss(1, 0.06))
                for _ in range(n):
                    c = member(region, d)
                    add_order(d, region, c[1], None, code, rng.choice(["app", "mini_program", "web"]), c[0])
            for _ in range(round(REGION_ORGANIC[region] * weekday * holiday * rng.gauss(1, 0.06))):
                c = member(region, d)
                add_order(d, region, c[1], None, "organic", rng.choice(["app", "mini_program", "web"]), c[0])

    # 供应链：每周库存快照与采购
    po_id = 0
    for week_start in days()[::7]:
        for region, wid in warehouses.items():
            for p in products:
                on_hand = rng.randint(0, 600)
                ds.add("ec_supply.inventory_daily", (week_start, wid, p["id"], on_hand, rng.randint(0, on_hand // 5)))
                if on_hand < 60:
                    po_id += 1
                    ds.add("ec_supply.purchase_order", (po_id, p["id"], wid, week_start,
                                                        week_start + timedelta(days=rng.randint(3, 10)),
                                                        rng.randrange(200, 800, 50), p["cost"], "arrived"))
    ds.truth = truth(ds)
    return ds


# --- 标准答案（由生成数据直接计算）-----------------------------------------------------

def truth(ds: Dataset) -> dict:
    orders = ds.tables["ec_trade.orders"]
    month = lambda d: d.strftime("%Y-%m")  # noqa: E731
    paid = [o for o in orders if o[10] != "cancelled"]

    def total(rows, key):
        out: dict = {}
        for o in rows:
            k = key(o)
            cnt, amt = out.get(k, (0, Decimal("0")))
            out[k] = (cnt + 1, amt + o[15])
        return {str(k): {"orders": v[0], "paid_amount": str(v[1])} for k, v in sorted(out.items())}

    traffic_rows = ds.tables["ec_org.store_daily_traffic"]
    gap = [t for t in traffic_rows if t[1] == TRAFFIC_GAP[1]]
    by_id = {o[0]: o for o in orders}
    store_name = {s[0]: s[1] for s in ds.tables["ec_org.store"]}
    warehouse_region = {w[0]: w[2] for w in ds.tables["ec_supply.warehouse"]}
    category = {c[0]: c for c in ds.tables["ec_product.category"]}
    l1_of = {p[0]: category[category[p[2]][2]][1] for p in ds.tables["ec_product.product"]}

    def count(rows, key, value=lambda r: 1) -> dict:
        out: dict = {}
        for r in rows:
            out[str(key(r))] = out.get(str(key(r)), 0) + value(r)
        return dict(sorted(out.items()))

    def hours(s) -> int:  # 与 dws_logistics_daily 相同：DATETIME 存到秒，TIMESTAMPDIFF(HOUR) 向下截断
        return int((s[5].replace(microsecond=0) - s[4].replace(microsecond=0)).total_seconds() // 3600)
    shipments = ds.tables["ec_logistics.shipment"]
    ship_key = lambda s: (warehouse_region[s[2]], month(s[4]))  # noqa: E731
    shipment_cnt, delivery_hours = count(shipments, ship_key), count(shipments, ship_key, hours)
    live_items = [i for i in ds.tables["ec_trade.order_item"] if by_id[i[1]][10] != "cancelled"]
    tickets = ds.tables["ec_service.service_ticket"]
    ticket_key = lambda t: (by_id[t[1]][5], month(t[3]))  # noqa: E731
    sat_sum = count(tickets, ticket_key, lambda t: t[5] or 0)
    rated = count(tickets, ticket_key, lambda t: 1 if t[5] is not None else 0)
    paid_channels = {c[0] for c in PAID_CHANNELS}
    ad_paid = count([o for o in paid if o[9] in paid_channels], lambda o: (o[5], month(o[2])), lambda o: o[15])
    return {
        "version": VERSION,
        "rows": {t: len(r) for t, r in sorted(ds.tables.items())},
        "region_month": total(paid, lambda o: (o[5], month(o[2]))),
        "region_channel_month": total(paid, lambda o: (o[5], o[7], month(o[2]))),
        "channel_month": total(paid, lambda o: (o[7], month(o[2]))),  # 全部区域按渠道类型
        "store_orders_day": total([o for o in paid if o[7] == "门店"], lambda o: str(o[2])),  # 门店渠道，按下单日期
        "store_month_sz": total([o for o in paid if o[6] == "SZ" and o[7] == "门店"], lambda o: (o[4], month(o[2]))),
        "ec_douyin_month": total([o for o in paid if o[5] == "EC" and o[9] == "douyin"], lambda o: month(o[2])),
        # 以下按数仓视图的口径计算（评测题的关键数值从这里取，不手填）
        "store_month": total([o for o in paid if o[7] == "门店"], lambda o: (store_name[o[4]], month(o[2]))),
        "logistics_region_month": {k: {"shipments": n, "delivery_hours": delivery_hours[k]}
                                   for k, n in shipment_cnt.items()},  # 按发货仓所在区域、发货日期
        "ticket_region_month": {k: {"tickets": n, "satisfaction_sum": sat_sum[k], "rated": rated[k]}
                                for k, n in count(tickets, ticket_key).items()},  # 按订单区域、工单创建日期
        "ticket_region_category_month": {k: {"tickets": n} for k, n in count(
            tickets, lambda t: (by_id[t[1]][5], t[4], month(t[3]))).items()},
        "store_traffic_month": {k: {"visitors": n} for k, n in count(  # dws_store_daily：以门店日客流为主表
            ds.tables["ec_org.store_daily_traffic"], lambda t: (store_name[t[0]], month(t[1])), lambda t: t[2]).items()},
        "orders_day": total(paid, lambda o: str(o[2])),  # 按下单日期，不含取消单
        "ad_region_month": {k: {"spend": str(v), "paid_amount": str(ad_paid.get(k, Decimal("0")))}
                            for k, v in count(ds.tables["ec_marketing.ad_spend_daily"], lambda a: (a[2], month(a[0])),
                                              lambda a: a[5]).items()},  # dws_marketing_daily：付费渠道花费与归因成交
        "category_month": {k: {"units": n} for k, n in count(  # 一级类目、下单日期，不含取消单
            live_items, lambda i: (l1_of[i[2]], month(by_id[i[1]][2])), lambda i: i[3]).items()},
        "region_category_month": {k: {"units": n} for k, n in count(
            live_items, lambda i: (by_id[i[1]][5], l1_of[i[2]], month(by_id[i[1]][2])), lambda i: i[3]).items()},
        "traffic_rows_on_gap_day": len(gap),
        "views_daily": views_daily(ds),
        "scenarios": {
            "closed_store": {"city": CLOSED_STORE[0], "name": CLOSED_STORE[3], "close_date": str(CLOSED_STORE[2])},
            "appliance_promo": {"campaign": "家电焕新季", "start": "2026-09-01", "end": "2026-09-20", "rate": "0.85"},
            "douyin_cut": {"region": "EC", "from": "2026-09-01", "keep": DOUYIN_CUT[2]},
            "mid_autumn": ["2026-09-25", "2026-09-27"],
            "storm": {"region": "SW", "from": str(STORM[1]), "to": str(STORM[2])},
            "new_store": {"city": NEW_STORE[0], "name": NEW_STORE[3], "open_date": str(NEW_STORE[2])},
            "traffic_gap": {"city": TRAFFIC_GAP[0], "date": str(TRAFFIC_GAP[1]), "stores": 3},
            "price_up": {"category": "数码", "from": str(PRICE_UP[0]), "factor": str(PRICE_UP[1])},
        },
    }


def views_daily(ds: Dataset) -> dict:
    """数仓日汇总视图按定义算出的日明细（列名与视图一致，维度取值与口径维度一致）：评测题的期望结论由它按产品的
    判定规则算出（tests/eval/scoring.py），不手填。只按生成数据计算，不查数据库。"""
    region = {r[0]: r[1] for r in ds.tables["ec_org.region"]}
    city = {c[0]: c for c in ds.tables["ec_org.city"]}
    store = {s[0]: s for s in ds.tables["ec_org.store"]}
    category = {c[0]: c for c in ds.tables["ec_product.category"]}
    l1_of = {p[0]: category[category[p[2]][2]][1] for p in ds.tables["ec_product.product"]}
    channel = {c[0]: c[1] for c in ds.tables["ec_marketing.channel"]}
    warehouse_region = {w[0]: w[2] for w in ds.tables["ec_supply.warehouse"]}
    orders = ds.tables["ec_trade.orders"]
    live = [o for o in orders if o[10] != "cancelled"]
    by_id = {o[0]: o for o in orders}

    def grouped(rows, key, values) -> list[list]:
        out: dict = {}
        for r in rows:
            k = key(r)
            out[k] = [a + b for a, b in zip(out.get(k) or [0] * len(values(r)), values(r))]
        return [[*map(str, k), *map(str, v)] for k, v in sorted(out.items())]

    store_orders: dict = {}
    for o in live:
        if o[7] == "门店":
            n, amount = store_orders.get((o[4], o[2]), (0, Decimal("0")))
            store_orders[(o[4], o[2])] = (n + 1, amount + o[15])
    store_rows = []
    for t in ds.tables["ec_org.store_daily_traffic"]:  # 以门店日客流为主表：没有客流记录的那天，订单随之缺失
        s = store[t[0]]
        n, amount = store_orders.get((t[0], t[1]), (0, Decimal("0")))
        store_rows.append([str(t[1]), region[city[s[2]][2]], s[1], city[s[2]][1], s[4], str(s[5]), s[7],
                           str(t[2]), str(n), str(amount)])
    ad_orders: dict = {}
    for o in live:
        n, amount = ad_orders.get((o[2], o[5], o[9]), (0, Decimal("0")))
        ad_orders[(o[2], o[5], o[9])] = (n + 1, amount + o[15])
    marketing_rows = []
    for a in ds.tables["ec_marketing.ad_spend_daily"]:
        n, amount = ad_orders.get((a[0], a[2], a[1]), (0, Decimal("0")))
        marketing_rows.append([str(a[0]), region[a[2]], channel[a[1]], str(a[5]), str(n), str(amount)])
    tickets = ds.tables["ec_service.service_ticket"]
    return {
        "dws_region_daily": {
            "columns": ["order_date", "region_name", "channel_type", "order_cnt", "paid_amount"],
            "rows": grouped(live, lambda o: (o[2], region[o[5]], o[7]), lambda o: (1, o[15]))},
        "dws_store_daily": {
            "columns": ["stat_date", "region_name", "store_name", "city_name", "district_level", "floor", "store_type",
                        "visitors", "order_cnt", "paid_amount"],
            "rows": store_rows},
        "dws_category_daily": {
            "columns": ["order_date", "region_name", "category_l1", "units", "item_amount"],
            "rows": grouped([i for i in ds.tables["ec_trade.order_item"] if by_id[i[1]][10] != "cancelled"],
                            lambda i: (by_id[i[1]][2], region[by_id[i[1]][5]], l1_of[i[2]]), lambda i: (i[3], i[6]))},
        "dws_marketing_daily": {
            "columns": ["spend_date", "region_name", "channel_name", "spend", "order_cnt", "paid_amount"],
            "rows": marketing_rows},
        "dws_logistics_daily": {  # 与视图相同：DATETIME 存到秒，TIMESTAMPDIFF(HOUR) 向下截断
            "columns": ["ship_date", "region_name", "shipment_cnt", "delivery_hours"],
            "rows": grouped(ds.tables["ec_logistics.shipment"], lambda s: (s[4].date(), region[warehouse_region[s[2]]]),
                            lambda s: (1, int((s[5].replace(microsecond=0) - s[4].replace(microsecond=0))
                                              .total_seconds() // 3600)))},
        "dws_service_daily": {
            "columns": ["ticket_date", "region_name", "category", "ticket_cnt", "rated_cnt", "satisfaction_sum"],
            "rows": grouped(tickets, lambda t: (t[3].date(), region[by_id[t[1]][5]], t[4]),
                            lambda t: (1, 1 if t[5] is not None else 0, t[5] or 0))},
    }


def ddl_checksum() -> str:
    """建表与视图语句的摘要：视图改了也要重建（数据摘要不变时）。"""
    return hashlib.sha256(repr((TABLES, VIEWS)).encode()).hexdigest()


def checksum(ds: Dataset) -> str:
    h = hashlib.sha256()
    for table in sorted(ds.tables):
        h.update(table.encode())
        for row in ds.tables[table]:
            h.update(repr(row).encode())
    return h.hexdigest()


# --- 建表与视图 ------------------------------------------------------------------------

def _t(name: str, comment: str, columns: list[tuple[str, str, str | None]], key: list[str],
       model: str = "PRIMARY KEY") -> str:
    # StarRocks 的 VARCHAR 长度按字节计（中文每字 3 字节）：按字符数声明，建表时放大 4 倍
    sized = [(c, re.sub(r"VARCHAR\((\d+)\)", lambda m: f"VARCHAR({int(m.group(1)) * 4})", t), m)
             for c, t, m in columns]
    cols = ",\n  ".join(f"{c} {t}" + (f" COMMENT '{m}'" if m else "") for c, t, m in sized)
    return (f"CREATE TABLE {name} (\n  {cols}\n) {model} ({', '.join(key)})\nCOMMENT '{comment}'\n"
            f"DISTRIBUTED BY HASH({key[0]}) BUCKETS 2\nPROPERTIES (\"replication_num\" = \"1\")")


DB_COMMENTS = {"ec_org": "组织与门店", "ec_product": "商品", "ec_member": "会员", "ec_trade": "交易",
               "ec_marketing": "营销", "ec_supply": "供应链", "ec_logistics": "物流", "ec_service": "客服",
               "ec_dw": "电商数仓（视图层）"}

# 少数列刻意不写注释，用来覆盖 ADR 0006 的注释缺口提示
TABLES = [
    _t("ec_org.region", "销售区域", [("region_code", "VARCHAR(8) NOT NULL", "区域编码"),
                                  ("region_name", "VARCHAR(16) NOT NULL", "区域名称")], ["region_code"]),
    _t("ec_org.city", "城市", [("city_code", "VARCHAR(8) NOT NULL", "城市编码"), ("city_name", "VARCHAR(16)", "城市名称"),
                             ("region_code", "VARCHAR(8)", "所属区域编码"), ("city_tier", "VARCHAR(8)", "城市等级")],
       ["city_code"]),
    _t("ec_org.store", "门店", [
        ("store_id", "INT NOT NULL", "门店编号"), ("store_name", "VARCHAR(32)", "门店名称"),
        ("city_code", "VARCHAR(8)", "所在城市编码"), ("business_district", "VARCHAR(32)", "所在商圈"),
        ("district_level", "VARCHAR(8)", "商圈等级：核心商圈 / 次级商圈 / 社区"), ("floor", "VARCHAR(8)", "所在楼层"),
        ("area_sqm", "INT", "营业面积（平方米）"), ("store_type", "VARCHAR(8)", "门店类型"),
        ("open_date", "DATE", "开业日期"), ("close_date", "DATE", "关店日期；营业中为空"),
        ("status", "VARCHAR(8)", "状态：open / closed")], ["store_id"]),
    _t("ec_org.store_daily_traffic", "门店日客流（客流计数器）", [
        ("store_id", "INT NOT NULL", "门店编号"), ("traffic_date", "DATE NOT NULL", "日期"),
        ("visitors", "INT", "进店人数"), ("peak_hour_visitors", "INT", "高峰小时进店人数")],
       ["store_id", "traffic_date"]),
    _t("ec_product.category", "商品类目", [("category_id", "INT NOT NULL", "类目编号"),
                                       ("category_name", "VARCHAR(16)", "类目名称"),
                                       ("parent_id", "INT", "上级类目；一级类目为空"), ("level", "TINYINT", "层级")],
       ["category_id"]),
    _t("ec_product.brand", "品牌", [("brand_id", "INT NOT NULL", "品牌编号"), ("brand_name", "VARCHAR(32)", "品牌名称")],
       ["brand_id"]),
    _t("ec_product.product", "商品", [
        ("product_id", "BIGINT NOT NULL", "商品编号"), ("product_name", "VARCHAR(64)", "商品名称"),
        ("category_id", "INT", "末级类目编号"), ("brand_id", "INT", "品牌编号"), ("list_price", "DECIMAL(18,2)", "标价"),
        ("cost_price", "DECIMAL(18,2)", "成本价"), ("launch_date", "DATE", "上架日期"), ("status", "VARCHAR(16)", "状态")],
       ["product_id"]),
    _t("ec_product.price_change", "商品调价记录", [
        ("product_id", "BIGINT NOT NULL", "商品编号"), ("effective_date", "DATE NOT NULL", "生效日期"),
        ("old_price", "DECIMAL(18,2)", "调整前标价"), ("new_price", "DECIMAL(18,2)", "调整后标价"),
        ("reason", "VARCHAR(32)", "调价原因")], ["product_id", "effective_date"]),
    _t("ec_member.customer", "会员", [
        ("customer_id", "BIGINT NOT NULL", "会员编号（不含个人信息）"), ("register_date", "DATE", "注册日期"),
        ("member_level", "VARCHAR(8)", "会员等级"), ("gender", "VARCHAR(4)", "性别"), ("birth_year", "INT", "出生年份"),
        ("city_code", "VARCHAR(8)", "常住城市编码"), ("register_channel", "VARCHAR(16)", "注册渠道")], ["customer_id"]),
    _t("ec_member.member_level_change", "会员等级变更", [
        ("customer_id", "BIGINT NOT NULL", "会员编号"), ("change_date", "DATE NOT NULL", "变更日期"),
        ("from_level", "VARCHAR(8)", "原等级"), ("to_level", "VARCHAR(8)", "新等级")], ["customer_id", "change_date"]),
    _t("ec_trade.orders", "订单", [
        ("order_id", "BIGINT NOT NULL", "订单号"), ("order_time", "DATETIME", "下单时间（Asia/Shanghai）"),
        ("order_date", "DATE", "下单日期（Asia/Shanghai）"), ("customer_id", "BIGINT", "会员编号；非会员为空"),
        ("store_id", "INT", "门店编号；线上订单为空"), ("region_code", "VARCHAR(8)", "区域编码（门店所在或收货地）"),
        ("city_code", "VARCHAR(8)", "城市编码（门店所在或收货地）"), ("channel_type", "VARCHAR(8)", "渠道类型：门店 / 线上"),
        ("platform", "VARCHAR(16)", "下单端：门店 / app / mini_program / web"),
        ("source_channel", "VARCHAR(16)", "来源渠道编码（营销归因）"), ("status", "VARCHAR(16)", "订单状态：paid / refunded / cancelled"),
        ("goods_amount", "DECIMAL(18,2)", "商品原价金额"), ("discount_amount", "DECIMAL(18,2)", "活动优惠金额"),
        ("coupon_amount", "DECIMAL(18,2)", "优惠券抵扣金额"), ("shipping_fee", "DECIMAL(18,2)", "运费"),
        ("paid_amount", "DECIMAL(18,2)", "实付金额"), ("pay_method", "VARCHAR(16)", "支付方式")], ["order_id"]),
    _t("ec_trade.order_item", "订单明细", [
        ("order_item_id", "BIGINT NOT NULL", "订单明细号"), ("order_id", "BIGINT", "订单号"),
        ("product_id", "BIGINT", "商品编号"), ("quantity", "INT", "件数"), ("unit_price", "DECIMAL(18,2)", "成交单价"),
        ("discount_amount", "DECIMAL(18,2)", "活动优惠金额"), ("item_amount", "DECIMAL(18,2)", "明细金额（扣活动优惠）")],
       ["order_item_id"]),
    _t("ec_trade.payment", "支付流水", [
        ("payment_id", "BIGINT NOT NULL", "支付流水号"), ("order_id", "BIGINT", "订单号"), ("pay_time", "DATETIME", "支付时间"),
        ("amount", "DECIMAL(18,2)", "支付金额"), ("status", "VARCHAR(16)", "支付状态")], ["payment_id"]),
    _t("ec_trade.refund", "退款", [
        ("refund_id", "BIGINT NOT NULL", "退款单号"), ("order_id", "BIGINT", "订单号"), ("refund_time", "DATETIME", "退款时间"),
        ("refund_amount", "DECIMAL(18,2)", "退款金额"), ("reason", "VARCHAR(32)", "退款原因")], ["refund_id"]),
    _t("ec_marketing.channel", "营销渠道", [("channel_code", "VARCHAR(16) NOT NULL", "渠道编码"),
                                        ("channel_name", "VARCHAR(16)", "渠道名称"),
                                        ("channel_type", "VARCHAR(8)", "渠道类型：付费 / 免费 / 线下")], ["channel_code"]),
    _t("ec_marketing.campaign", "营销活动", [
        ("campaign_id", "INT NOT NULL", "活动编号"), ("campaign_name", "VARCHAR(32)", "活动名称"),
        ("campaign_type", "VARCHAR(8)", "活动类型"), ("start_date", "DATE", "开始日期"), ("end_date", "DATE", "结束日期"),
        ("category_name", "VARCHAR(16)", "限定一级类目；全场为空"), ("discount_rate", "DECIMAL(4,2)", "折扣率"),
        ("budget", "DECIMAL(18,2)", "活动预算")], ["campaign_id"]),
    _t("ec_marketing.ad_spend_daily", "日投放", [
        ("spend_date", "DATE NOT NULL", "投放日期"), ("channel_code", "VARCHAR(16) NOT NULL", "渠道编码"),
        ("region_code", "VARCHAR(8) NOT NULL", "区域编码"), ("impressions", "BIGINT", None), ("clicks", "BIGINT", None),
        ("spend", "DECIMAL(18,2)", "花费（元）")], ["spend_date", "channel_code", "region_code"]),
    _t("ec_marketing.coupon", "优惠券", [
        ("coupon_id", "INT NOT NULL", "优惠券编号"), ("campaign_id", "INT", "所属活动"), ("face_value", "DECIMAL(18,2)", "面额"),
        ("threshold", "DECIMAL(18,2)", "使用门槛"), ("valid_from", "DATE", "生效日期"), ("valid_to", "DATE", "失效日期")],
       ["coupon_id"]),
    _t("ec_marketing.coupon_usage", "优惠券核销", [
        ("coupon_id", "INT NOT NULL", "优惠券编号"), ("customer_id", "BIGINT NOT NULL", "会员编号"),
        ("order_id", "BIGINT NOT NULL", "订单号"), ("used_time", "DATETIME", "核销时间")],
       ["coupon_id", "customer_id", "order_id"]),
    _t("ec_supply.warehouse", "仓库", [("warehouse_id", "INT NOT NULL", "仓库编号"), ("warehouse_name", "VARCHAR(32)", "仓库名称"),
                                    ("region_code", "VARCHAR(8)", "服务区域")], ["warehouse_id"]),
    _t("ec_supply.inventory_daily", "库存快照（每周）", [
        ("snapshot_date", "DATE NOT NULL", "快照日期"), ("warehouse_id", "INT NOT NULL", "仓库编号"),
        ("product_id", "BIGINT NOT NULL", "商品编号"), ("on_hand_qty", "INT", "在库数量"), ("reserved_qty", "INT", None)],
       ["snapshot_date", "warehouse_id", "product_id"]),
    _t("ec_supply.purchase_order", "采购单", [
        ("po_id", "BIGINT NOT NULL", "采购单号"), ("product_id", "BIGINT", "商品编号"), ("warehouse_id", "INT", "入库仓库"),
        ("order_date", "DATE", "下单日期"), ("arrive_date", "DATE", "到货日期"), ("quantity", "INT", "采购数量"),
        ("unit_cost", "DECIMAL(18,2)", "采购单价"), ("status", "VARCHAR(16)", "状态")], ["po_id"]),
    _t("ec_logistics.shipment", "发货", [
        ("shipment_id", "BIGINT NOT NULL", "运单号"), ("order_id", "BIGINT", "订单号"), ("warehouse_id", "INT", "发货仓库"),
        ("carrier", "VARCHAR(16)", None), ("ship_time", "DATETIME", "发货时间"), ("deliver_time", "DATETIME", "签收时间"),
        ("status", "VARCHAR(16)", "运单状态")], ["shipment_id"]),
    _t("ec_service.service_ticket", "客服工单", [
        ("ticket_id", "BIGINT NOT NULL", "工单号"), ("order_id", "BIGINT", "订单号"), ("customer_id", "BIGINT", "会员编号"),
        ("created_time", "DATETIME", "创建时间"), ("category", "VARCHAR(16)", "问题分类"), ("satisfaction", "TINYINT", None)],
       ["ticket_id"]),
]

# 数仓视图：明细宽表 → 日汇总 → 主题汇总，形成多层列级血缘
VIEWS = [
    ("ec_dw.dwd_order", "订单明细宽表（含区域、城市、门店）", """
SELECT o.order_id, o.order_date, o.order_time, o.channel_type, o.platform, o.source_channel, o.region_code,
       r.region_name, o.city_code, c.city_name, o.store_id, s.store_name, o.customer_id, o.status,
       o.goods_amount, o.discount_amount, o.coupon_amount, o.shipping_fee, o.paid_amount
FROM ec_trade.orders o
JOIN ec_org.region r ON r.region_code = o.region_code
LEFT JOIN ec_org.city c ON c.city_code = o.city_code
LEFT JOIN ec_org.store s ON s.store_id = o.store_id"""),
    ("ec_dw.dwd_order_item", "订单商品明细宽表（含类目、品牌）", """
SELECT i.order_item_id, i.order_id, o.order_date, o.region_code, o.channel_type, o.status, i.product_id,
       p.product_name, l1.category_name AS category_l1, l2.category_name AS category_l2, b.brand_name,
       i.quantity, i.unit_price, i.discount_amount, i.item_amount
FROM ec_trade.order_item i
JOIN ec_trade.orders o ON o.order_id = i.order_id
JOIN ec_product.product p ON p.product_id = i.product_id
JOIN ec_product.category l2 ON l2.category_id = p.category_id
JOIN ec_product.category l1 ON l1.category_id = l2.parent_id
JOIN ec_product.brand b ON b.brand_id = p.brand_id"""),
    ("ec_dw.dws_region_daily", "区域日交易汇总（不含取消单）", """
SELECT o.order_date, o.region_code, o.region_name, o.channel_type, COUNT(DISTINCT o.order_id) AS order_cnt,
       SUM(o.goods_amount) AS goods_amount, SUM(o.discount_amount + o.coupon_amount) AS discount_amount,
       SUM(o.paid_amount) AS paid_amount, COUNT(DISTINCT o.customer_id) AS buyer_cnt
FROM ec_dw.dwd_order o
WHERE o.status <> 'cancelled'
GROUP BY o.order_date, o.region_code, o.region_name, o.channel_type"""),
    ("ec_dw.dws_store_daily", "门店日交易与客流（不含取消单）", """
SELECT t.traffic_date AS stat_date, s.store_id, s.store_name, s.city_code, c.city_name, c.region_code, r.region_name,
       s.district_level, s.floor, s.area_sqm, s.store_type, t.visitors, COALESCE(d.order_cnt, 0) AS order_cnt,
       COALESCE(d.paid_amount, 0) AS paid_amount
FROM ec_org.store_daily_traffic t
JOIN ec_org.store s ON s.store_id = t.store_id
JOIN ec_org.city c ON c.city_code = s.city_code
JOIN ec_org.region r ON r.region_code = c.region_code
LEFT JOIN (
  SELECT store_id, order_date, COUNT(*) AS order_cnt, SUM(paid_amount) AS paid_amount
  FROM ec_trade.orders WHERE status <> 'cancelled' AND channel_type = '门店'
  GROUP BY store_id, order_date
) d ON d.store_id = t.store_id AND d.order_date = t.traffic_date"""),
    ("ec_dw.dws_category_daily", "类目日销售（不含取消单）", """
SELECT i.order_date, i.region_code, r.region_name, i.category_l1, SUM(i.quantity) AS units,
       SUM(i.item_amount) AS item_amount, SUM(i.discount_amount) AS discount_amount
FROM ec_dw.dwd_order_item i
JOIN ec_org.region r ON r.region_code = i.region_code
WHERE i.status <> 'cancelled'
GROUP BY i.order_date, i.region_code, r.region_name, i.category_l1"""),
    ("ec_dw.dws_marketing_daily", "渠道日投放与线上订单", """
SELECT a.spend_date, a.region_code, r.region_name, a.channel_code, ch.channel_name, a.impressions, a.clicks, a.spend,
       COALESCE(o.order_cnt, 0) AS order_cnt, COALESCE(o.paid_amount, 0) AS paid_amount
FROM ec_marketing.ad_spend_daily a
JOIN ec_marketing.channel ch ON ch.channel_code = a.channel_code
JOIN ec_org.region r ON r.region_code = a.region_code
LEFT JOIN (
  SELECT order_date, region_code, source_channel, COUNT(*) AS order_cnt, SUM(paid_amount) AS paid_amount
  FROM ec_trade.orders WHERE status <> 'cancelled'
  GROUP BY order_date, region_code, source_channel
) o ON o.order_date = a.spend_date AND o.region_code = a.region_code AND o.source_channel = a.channel_code"""),
    ("ec_dw.dws_refund_daily", "区域日退款", """
SELECT CAST(f.refund_time AS DATE) AS refund_date, o.region_code, r.region_name, COUNT(*) AS refund_cnt,
       SUM(f.refund_amount) AS refund_amount
FROM ec_trade.refund f
JOIN ec_trade.orders o ON o.order_id = f.order_id
JOIN ec_org.region r ON r.region_code = o.region_code
GROUP BY CAST(f.refund_time AS DATE), o.region_code, r.region_name"""),
    ("ec_dw.dws_logistics_daily", "区域日配送时效", """
SELECT CAST(s.ship_time AS DATE) AS ship_date, w.region_code, r.region_name, COUNT(*) AS shipment_cnt,
       SUM(TIMESTAMPDIFF(HOUR, s.ship_time, s.deliver_time)) AS delivery_hours
FROM ec_logistics.shipment s
JOIN ec_supply.warehouse w ON w.warehouse_id = s.warehouse_id
JOIN ec_org.region r ON r.region_code = w.region_code
GROUP BY CAST(s.ship_time AS DATE), w.region_code, r.region_name"""),
    ("ec_dw.dws_service_daily", "区域日客服工单", """
SELECT CAST(t.created_time AS DATE) AS ticket_date, o.region_code, r.region_name, t.category, COUNT(*) AS ticket_cnt,
       COUNT(t.satisfaction) AS rated_cnt, SUM(t.satisfaction) AS satisfaction_sum
FROM ec_service.service_ticket t
JOIN ec_trade.orders o ON o.order_id = t.order_id
JOIN ec_org.region r ON r.region_code = o.region_code
GROUP BY CAST(t.created_time AS DATE), o.region_code, r.region_name, t.category"""),
]


# --- 灌数 ---------------------------------------------------------------------------

def _literal(value) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, (int, Decimal)) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, datetime):
        return f"'{value:%Y-%m-%d %H:%M:%S}'"
    if isinstance(value, date):
        return f"'{value.isoformat()}'"
    return "'" + str(value).replace("\\", "\\\\").replace("'", "''") + "'"


def loaded_checksum(conn) -> str | None:
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM information_schema.tables WHERE TABLE_SCHEMA = 'ec_org' "
                    "AND TABLE_NAME = 'region'")
        if not cur.fetchone()[0]:
            return None
        cur.execute("SELECT TABLE_COMMENT FROM information_schema.tables WHERE TABLE_SCHEMA = 'ec_org' "
                    "AND TABLE_NAME = 'region'")
        comment = cur.fetchone()[0] or ""
    return comment.split("#", 1)[1] if "#" in comment else None


def load(env: dict[str, str], force: bool = False) -> dict:
    ds = generate()
    digest = f"{VERSION}:{checksum(ds)[:12]}{ddl_checksum()[:4]}"
    conn = demo.sr_root(env)
    try:
        loaded = loaded_checksum(conn)
        if not force and loaded == digest:
            demo.log(f"电商数据集已是 {digest}，跳过灌数（--force 可强制重建）")
        elif not force and loaded and loaded[:-4] == digest[:-4]:
            demo.log(f"电商数据集的数据未变，只重建视图：{loaded} → {digest}")
            with conn.cursor() as cur:
                for name, comment, sql in VIEWS:
                    cur.execute(f"DROP VIEW IF EXISTS {name}")
                    cur.execute(f"CREATE VIEW {name} COMMENT '{comment}' AS {sql.strip()}")
                cur.execute(f"ALTER TABLE ec_org.region COMMENT = '销售区域#{digest}'")
        else:
            demo.log(f"灌入电商数据集 {digest} ...")
            with conn.cursor() as cur:
                for db in DATABASES:
                    cur.execute(f"DROP DATABASE IF EXISTS {db} FORCE")
                    cur.execute(f"CREATE DATABASE {db}")
                for ddl in TABLES:
                    cur.execute(ddl)
                for table, rows in ds.tables.items():
                    for i in range(0, len(rows), 4000):
                        values = ",".join("(" + ",".join(_literal(v) for v in row) + ")" for row in rows[i:i + 4000])
                        cur.execute(f"INSERT INTO {table} VALUES {values}")
                    demo.log(f"  {table}：{len(rows)} 行")
                for name, comment, sql in VIEWS:
                    cur.execute(f"CREATE VIEW {name} COMMENT '{comment}' AS {sql.strip()}")
                # 版本标记写在区域表的注释里（# 之后），用于判断是否需要重建
                cur.execute(f"ALTER TABLE ec_org.region COMMENT = '销售区域#{digest}'")
        grant(conn, env)
    finally:
        conn.close()
    TRUTH_FILE.parent.mkdir(exist_ok=True)
    TRUTH_FILE.write_text(json.dumps(ds.truth, ensure_ascii=False, indent=2), encoding="utf-8")
    return ds.truth


def grant(conn, env: dict[str, str]) -> None:
    """演示库的采集 / 查询账号对电商数据集只读（demo.py seed 重建账号后也会调用）。"""
    with conn.cursor() as cur:
        for user in demo.SR_USERS:
            for db in DATABASES:
                cur.execute(f"GRANT SELECT ON ALL TABLES IN DATABASE {db} TO USER '{user}'@'%'")
                cur.execute(f"GRANT SELECT ON ALL VIEWS IN DATABASE {db} TO USER '{user}'@'%'")


def check(env: dict[str, str]) -> bool:
    expected = generate().truth
    conn = demo.sr_connect(env, "avelis_reader")
    ok = True
    try:
        with conn.cursor() as cur:
            for table, n in expected["rows"].items():
                cur.execute(f"SELECT COUNT(*) FROM {table}")
                actual = cur.fetchone()[0]
                ok &= actual == n
                demo.log(f"  {'通过' if actual == n else '不符'}  {table}：期望 {n}，实际 {actual}")
            cur.execute("SELECT region_code, DATE_FORMAT(order_date, '%Y-%m'), COUNT(*), SUM(paid_amount) "
                        "FROM ec_trade.orders WHERE status <> 'cancelled' GROUP BY 1, 2")
            for region, month, cnt, amount in cur.fetchall():
                want = expected["region_month"][str((region, month))]
                same = want["orders"] == cnt and want["paid_amount"] == str(amount)
                ok &= same
                if not same:
                    demo.log(f"  不符  {region} {month}：期望 {want}，实际 {cnt} / {amount}")
    finally:
        conn.close()
    return ok


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_load = sub.add_parser("load")
    p_load.add_argument("--force", action="store_true")
    sub.add_parser("truth")
    sub.add_parser("check")
    args = parser.parse_args()
    if args.cmd == "truth":
        print(json.dumps(generate().truth, ensure_ascii=False, indent=2))
    elif args.cmd == "load":
        env = demo.load_env()
        load(env, args.force)
        sys.exit(0 if check(env) else 1)
    elif args.cmd == "check":
        sys.exit(0 if check(demo.load_env()) else 1)


if __name__ == "__main__":
    main()
