-- 演示线下门店库（Apache Doris 4.1）：与 PostgreSQL 门店库同一份合成数据，日期固定。
-- demo.py 用 root 每次删除并重建 store 库；账号与授权由 demo.py 按 .env 中的口令创建。
-- 唯一键模型（UNIQUE KEY）对应其他库的主键；单 BE，副本数 1。
DROP DATABASE IF EXISTS store FORCE;
CREATE DATABASE store;

CREATE TABLE store.shops (
  shop_id   INT         NOT NULL,
  shop_name VARCHAR(32) NOT NULL,
  city      VARCHAR(16) NOT NULL
) UNIQUE KEY (shop_id)
COMMENT '门店'
DISTRIBUTED BY HASH(shop_id) BUCKETS 1
PROPERTIES ("replication_num" = "1");

CREATE TABLE store.sales (
  sale_id   BIGINT        NOT NULL,
  sale_date DATE          NOT NULL,
  shop_id   INT           NOT NULL,
  status    VARCHAR(16)   NOT NULL,
  amount    DECIMAL(18, 2) NOT NULL COMMENT '成交金额'
) UNIQUE KEY (sale_id)
COMMENT '门店销售单'
DISTRIBUTED BY HASH(sale_id) BUCKETS 1
PROPERTIES ("replication_num" = "1");

CREATE TABLE store.returns (
  return_id   BIGINT        NOT NULL,
  sale_id     BIGINT        NOT NULL,
  return_date DATE          NOT NULL,
  amount      DECIMAL(18, 2) NOT NULL
) UNIQUE KEY (return_id)
COMMENT '门店退货单'
DISTRIBUTED BY HASH(return_id) BUCKETS 1
PROPERTIES ("replication_num" = "1");

INSERT INTO store.shops VALUES (1, '南京西路店', '上海'), (2, '徐家汇店', '上海'), (3, '三里屯店', '北京');
INSERT INTO store.sales VALUES
  (9001, '2026-09-15', 1, 'paid', 100.00),
  (9002, '2026-09-15', 2, 'paid', 150.00),
  (9003, '2026-09-15', 3, 'paid',  80.00),
  (9004, '2026-09-15', 1, 'void',  60.00),
  (9005, '2026-09-16', 1, 'paid',  40.00);
INSERT INTO store.returns VALUES (7001, 9002, '2026-09-15', 30.00), (7002, 9002, '2026-09-16', 20.00);

-- 城市日销售：上海 2026-09-15 = 250.00（作废单不计），北京 = 80.00
CREATE VIEW store.city_daily_sales COMMENT '城市日销售（不扣退货）' AS
SELECT s.sale_date, h.city, SUM(s.amount) AS sales_amount, COUNT(DISTINCT s.sale_id) AS sales_count
FROM store.sales s INNER JOIN store.shops h ON h.shop_id = s.shop_id
WHERE s.status = 'paid'
GROUP BY s.sale_date, h.city;
