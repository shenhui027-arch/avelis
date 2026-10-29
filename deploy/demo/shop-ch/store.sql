-- 演示线下门店库（ClickHouse）：与 PostgreSQL 门店库同一份合成数据，日期固定。demo.py 用 default 用户执行，
-- 每次删除并重建 store 库。账号（avelis_collector / avelis_reader）由 demo.py 按 .env 中的口令创建。
DROP DATABASE IF EXISTS store SYNC;
CREATE DATABASE store ENGINE = Atomic COMMENT '演示线下门店库（ClickHouse）';

CREATE TABLE store.shops (
  shop_id   UInt32,
  shop_name String,
  city      String
) ENGINE = MergeTree ORDER BY shop_id COMMENT '门店';

CREATE TABLE store.sales (
  sale_id   UInt64,
  sale_date Date,
  shop_id   UInt32,
  status    String,
  amount    Decimal(18, 2) COMMENT '成交金额'
) ENGINE = MergeTree ORDER BY sale_id COMMENT '门店销售单';

CREATE TABLE store.returns (
  return_id   UInt64,
  sale_id     UInt64,
  return_date Date,
  amount      Decimal(18, 2)
) ENGINE = MergeTree ORDER BY return_id COMMENT '门店退货单';

INSERT INTO store.shops VALUES (1, '南京西路店', '上海'), (2, '徐家汇店', '上海'), (3, '三里屯店', '北京');
INSERT INTO store.sales VALUES
  (9001, '2026-09-15', 1, 'paid', 100.00),
  (9002, '2026-09-15', 2, 'paid', 150.00),
  (9003, '2026-09-15', 3, 'paid',  80.00),
  (9004, '2026-09-15', 1, 'void',  60.00),
  (9005, '2026-09-16', 1, 'paid',  40.00);
INSERT INTO store.returns VALUES (7001, 9002, '2026-09-15', 30.00), (7002, 9002, '2026-09-16', 20.00);

-- 城市日销售：上海 2026-09-15 = 250.00（作废单不计），北京 = 80.00
CREATE VIEW store.city_daily_sales AS
SELECT s.sale_date, h.city, SUM(s.amount) AS sales_amount, COUNT(DISTINCT s.sale_id) AS sales_count
FROM store.sales AS s INNER JOIN store.shops AS h ON h.shop_id = s.shop_id
WHERE s.status = 'paid'
GROUP BY s.sale_date, h.city
COMMENT '城市日销售（不扣退货）';
