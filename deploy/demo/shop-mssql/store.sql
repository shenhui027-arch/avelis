-- 演示线下门店库（SQL Server）：与 PostgreSQL 门店库同一份合成数据，日期固定。
-- demo.py 用 sa 先删除并重建 storedb，再在 storedb 中逐条执行本文件；账号与授权由 demo.py 按 .env 中的口令创建。
-- 中文列用 NVARCHAR、字面量用 N'...'（库的排序规则为默认的 SQL_Latin1_General_CP1_CI_AS）。
CREATE SCHEMA store;

CREATE TABLE store.shops (
  shop_id   INT           NOT NULL PRIMARY KEY,
  shop_name NVARCHAR(32)  NOT NULL UNIQUE,
  city      NVARCHAR(16)  NOT NULL
);

CREATE TABLE store.sales (
  sale_id   BIGINT         NOT NULL PRIMARY KEY,
  sale_date DATE           NOT NULL,
  shop_id   INT            NOT NULL REFERENCES store.shops (shop_id),
  status    NVARCHAR(16)   NOT NULL,
  amount    DECIMAL(18, 2) NOT NULL
);

CREATE TABLE store.returns (
  return_id   BIGINT         NOT NULL PRIMARY KEY,
  sale_id     BIGINT         NOT NULL,
  return_date DATE           NOT NULL,
  amount      DECIMAL(18, 2) NOT NULL
);

EXEC sp_addextendedproperty @name = N'MS_Description', @value = N'门店', @level0type = N'SCHEMA', @level0name = N'store', @level1type = N'TABLE', @level1name = N'shops';
EXEC sp_addextendedproperty @name = N'MS_Description', @value = N'门店销售单', @level0type = N'SCHEMA', @level0name = N'store', @level1type = N'TABLE', @level1name = N'sales';
EXEC sp_addextendedproperty @name = N'MS_Description', @value = N'成交金额', @level0type = N'SCHEMA', @level0name = N'store', @level1type = N'TABLE', @level1name = N'sales', @level2type = N'COLUMN', @level2name = N'amount';
EXEC sp_addextendedproperty @name = N'MS_Description', @value = N'门店退货单', @level0type = N'SCHEMA', @level0name = N'store', @level1type = N'TABLE', @level1name = N'returns';

INSERT INTO store.shops VALUES (1, N'南京西路店', N'上海'), (2, N'徐家汇店', N'上海'), (3, N'三里屯店', N'北京');
INSERT INTO store.sales VALUES
  (9001, '2026-09-15', 1, N'paid', 100.00),
  (9002, '2026-09-15', 2, N'paid', 150.00),
  (9003, '2026-09-15', 3, N'paid',  80.00),
  (9004, '2026-09-15', 1, N'void',  60.00),
  (9005, '2026-09-16', 1, N'paid',  40.00);
INSERT INTO store.returns VALUES (7001, 9002, '2026-09-15', 30.00), (7002, 9002, '2026-09-16', 20.00);

-- 城市日销售：上海 2026-09-15 = 250.00（作废单不计），北京 = 80.00
CREATE VIEW store.city_daily_sales AS
SELECT s.sale_date, h.city, SUM(s.amount) AS sales_amount, COUNT(DISTINCT s.sale_id) AS sales_count
FROM store.sales AS s INNER JOIN store.shops AS h ON h.shop_id = s.shop_id
WHERE s.status = N'paid'
GROUP BY s.sale_date, h.city;

EXEC sp_addextendedproperty @name = N'MS_Description', @value = N'城市日销售（不扣退货）', @level0type = N'SCHEMA', @level0name = N'store', @level1type = N'VIEW', @level1name = N'city_daily_sales';
