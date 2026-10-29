-- 经营分析域：事实表关联 MySQL 维表（经 JDBC Catalog shop_mysql）。
-- 若 StarRocks 不支持视图引用 Catalog 表，scripts/demo.py 改用 40-ads-fallback.sql。

CREATE DATABASE ads;

-- 粒度：明细 → 订单 N:1，明细 → 商品 N:1，商品 → 类目 N:1；按日期、类目聚合，不放大。
CREATE VIEW ads.sales_by_category COMMENT '日类目销售（明细金额）' AS
SELECT o.order_date, c.category_name, SUM(i.item_amount) AS item_sales
FROM shop.order_items i
JOIN shop.orders o ON i.order_id = o.order_id
JOIN shop_mysql.shop_dim.dim_product p ON i.product_id = p.product_id
JOIN shop_mysql.shop_dim.dim_category c ON p.category_id = c.category_id
GROUP BY o.order_date, c.category_name;

-- 粒度：订单 → 会员 N:1。
CREATE VIEW ads.member_sales COMMENT '日会员等级销售' AS
SELECT o.order_date, m.member_level, SUM(o.paid_amount) AS paid_sales
FROM shop.orders o
JOIN shop_mysql.shop_dim.dim_customer m ON o.customer_id = m.customer_id
GROUP BY o.order_date, m.member_level;
