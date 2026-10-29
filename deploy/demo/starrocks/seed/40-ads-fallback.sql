-- 备用方案：视图不能引用 Catalog 表时，用 ETL 方式把关联结果写入内表。

CREATE DATABASE IF NOT EXISTS ads;

CREATE TABLE ads.sales_by_category (
  order_date    DATE          NOT NULL,
  category_name VARCHAR(32)   NOT NULL,
  item_sales    DECIMAL(38,2) NOT NULL
) PRIMARY KEY (order_date, category_name)
COMMENT '日类目销售（明细金额；由 Catalog 维表关联写入）'
DISTRIBUTED BY HASH(order_date) BUCKETS 1
PROPERTIES ("replication_num" = "1");

INSERT INTO ads.sales_by_category
SELECT o.order_date, c.category_name, SUM(i.item_amount)
FROM shop.order_items i
JOIN shop.orders o ON i.order_id = o.order_id
JOIN shop_mysql.shop_dim.dim_product p ON i.product_id = p.product_id
JOIN shop_mysql.shop_dim.dim_category c ON p.category_id = c.category_id
GROUP BY o.order_date, c.category_name;

CREATE TABLE ads.member_sales (
  order_date   DATE          NOT NULL,
  member_level VARCHAR(16)   NOT NULL,
  paid_sales   DECIMAL(38,2) NOT NULL
) PRIMARY KEY (order_date, member_level)
COMMENT '日会员等级销售（由 Catalog 维表关联写入）'
DISTRIBUTED BY HASH(order_date) BUCKETS 1
PROPERTIES ("replication_num" = "1");

INSERT INTO ads.member_sales
SELECT o.order_date, m.member_level, SUM(o.paid_amount)
FROM shop.orders o
JOIN shop_mysql.shop_dim.dim_customer m ON o.customer_id = m.customer_id
GROUP BY o.order_date, m.member_level;
