-- 演示线下门店库（Oracle Database 23ai Free，服务名 FREEPDB1）：与 PostgreSQL 门店库同一份合成数据，日期固定。
-- demo.py 用 SYSTEM 先删除 STORE 与两个账号，再逐条执行本文件；账号与授权由 demo.py 按 .env 中的口令创建。
-- STORE 是无口令的模式账号（NO AUTHENTICATION），只用来存放对象，不能登录。
CREATE USER store NO AUTHENTICATION DEFAULT TABLESPACE users QUOTA UNLIMITED ON users;

CREATE TABLE store.shops (
  shop_id   NUMBER(10)     PRIMARY KEY,
  shop_name VARCHAR2(32)   NOT NULL UNIQUE,
  city      VARCHAR2(16)   NOT NULL
);

CREATE TABLE store.sales (
  sale_id   NUMBER(19)     PRIMARY KEY,
  sale_date DATE           NOT NULL,
  shop_id   NUMBER(10)     NOT NULL REFERENCES store.shops (shop_id),
  status    VARCHAR2(16)   NOT NULL,
  amount    NUMBER(18, 2)  NOT NULL
);

CREATE TABLE store.returns (
  return_id   NUMBER(19)    PRIMARY KEY,
  sale_id     NUMBER(19)    NOT NULL,
  return_date DATE          NOT NULL,
  amount      NUMBER(18, 2) NOT NULL
);

COMMENT ON TABLE store.shops IS '门店';
COMMENT ON TABLE store.sales IS '门店销售单';
COMMENT ON COLUMN store.sales.amount IS '成交金额';
COMMENT ON TABLE store.returns IS '门店退货单';

INSERT INTO store.shops VALUES (1, '南京西路店', '上海'), (2, '徐家汇店', '上海'), (3, '三里屯店', '北京');
-- 9003 的下单时间带时分：按日期过滤必须用 [当日 0 点, 次日 0 点) 区间，BETWEEN 两个日期会漏掉它
INSERT INTO store.sales VALUES
  (9001, DATE '2026-09-15', 1, 'paid', 100.00),
  (9002, DATE '2026-09-15', 2, 'paid', 150.00),
  (9003, TIMESTAMP '2026-09-15 10:30:00', 3, 'paid', 80.00),
  (9004, DATE '2026-09-15', 1, 'void', 60.00),
  (9005, DATE '2026-09-16', 1, 'paid', 40.00);
INSERT INTO store.returns VALUES (7001, 9002, DATE '2026-09-15', 30.00), (7002, 9002, DATE '2026-09-16', 20.00);
COMMIT;

-- 城市日销售：上海 2026-09-15 = 250.00（作废单不计），北京 = 80.00（其 sale_date 为 2026-09-15 10:30）
CREATE VIEW store.city_daily_sales AS
SELECT s.sale_date, h.city, SUM(s.amount) AS sales_amount, COUNT(DISTINCT s.sale_id) AS sales_count
FROM store.sales s INNER JOIN store.shops h ON h.shop_id = s.shop_id
WHERE s.status = 'paid'
GROUP BY s.sale_date, h.city;

COMMENT ON TABLE store.city_daily_sales IS '城市日销售（不扣退货）';
