-- 演示线下门店库 storedb.store：合成数据，日期固定。demo.py seed 会删除并重建 store 模式。
SET client_encoding = 'UTF8';
DROP SCHEMA IF EXISTS store CASCADE;
CREATE SCHEMA store;

CREATE TABLE store.shops (
  shop_id   integer PRIMARY KEY,
  shop_name varchar(32) NOT NULL UNIQUE,
  city      varchar(16) NOT NULL
);
COMMENT ON TABLE store.shops IS '门店';

CREATE TABLE store.sales (
  sale_id   bigint PRIMARY KEY,
  sale_date date NOT NULL,
  shop_id   integer NOT NULL REFERENCES store.shops (shop_id),
  status    varchar(16) NOT NULL,
  amount    numeric(18, 2) NOT NULL
);
COMMENT ON TABLE store.sales IS '门店销售单';
COMMENT ON COLUMN store.sales.amount IS '成交金额';

CREATE TABLE store.returns (
  return_id   bigint PRIMARY KEY,
  sale_id     bigint NOT NULL,
  return_date date NOT NULL,
  amount      numeric(18, 2) NOT NULL
);
COMMENT ON TABLE store.returns IS '门店退货单';

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
FROM store.sales s JOIN store.shops h ON h.shop_id = s.shop_id
WHERE s.status = 'paid'
GROUP BY s.sale_date, h.city;
COMMENT ON VIEW store.city_daily_sales IS '城市日销售（不扣退货）';

GRANT USAGE ON SCHEMA store TO avelis_collector, avelis_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA store TO avelis_reader;
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA store GRANT SELECT ON TABLES TO avelis_reader;
