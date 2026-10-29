-- 每日净销售与类目汇总（演示 ETL，多语句）
USE shop;

-- 先筛出已支付订单；注释中的分号；不会切断语句
CREATE TEMPORARY TABLE tmp_paid AS
SELECT order_id, order_date, region, paid_amount, refund_amount
FROM shop.orders
WHERE status = 'paid';

CREATE TABLE ads.order_daily_net AS
SELECT t.order_date, t.region,
       SUM(t.paid_amount - t.refund_amount) AS net_amount,
       COUNT(DISTINCT t.order_id) AS order_cnt
FROM tmp_paid t
GROUP BY t.order_date, t.region;

INSERT INTO ads.category_daily (order_date, category_name, item_sales)
SELECT o.order_date, c.category_name, SUM(i.item_amount)
FROM shop.order_items i
JOIN shop.orders o ON i.order_id = o.order_id
JOIN shop_mysql.shop_dim.dim_product p ON i.product_id = p.product_id
JOIN shop_mysql.shop_dim.dim_category c ON p.category_id = c.category_id
GROUP BY o.order_date, c.category_name;

DROP TABLE tmp_paid;
