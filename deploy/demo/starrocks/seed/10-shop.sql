-- 交易域：电商事实表。固定日期的合成数据。
-- 验收故事：2026-09-15 华东区两笔订单，实付 100、200，退款 10、20。
-- 粒度：orders 为 order_id；order_items 为 order_item_id（订单 1001 有两行）；
--       refund_raw 为 refund_id（订单 1001 有两笔退款）。

CREATE DATABASE shop;

CREATE TABLE shop.orders (
  order_id      BIGINT        NOT NULL COMMENT '订单号',
  order_date    DATE          NOT NULL COMMENT '下单日期（Asia/Shanghai）',
  region        VARCHAR(16)   NOT NULL COMMENT '销售区域名称',
  customer_id   BIGINT        NOT NULL COMMENT '客户编号',
  status        VARCHAR(16)   NOT NULL COMMENT '订单状态',
  paid_amount   DECIMAL(18,2) NOT NULL COMMENT '实付金额',
  refund_amount DECIMAL(18,2) NOT NULL DEFAULT "0" COMMENT '退款金额（由 refund_raw 汇总）'
) PRIMARY KEY (order_id)
COMMENT '订单'
DISTRIBUTED BY HASH(order_id) BUCKETS 1
PROPERTIES ("replication_num" = "1");

INSERT INTO shop.orders VALUES
  (1001, '2026-09-15', '华东', 501, 'paid', 100.00, 10.00),
  (1002, '2026-09-15', '华东', 502, 'paid', 200.00, 20.00),
  (1003, '2026-09-15', '华北', 503, 'paid', 150.00,  0.00),
  (1004, '2026-09-16', '华东', 504, 'paid',  80.00,  0.00),
  (1005, '2026-09-15', '华南', 505, 'paid',  60.00,  5.00),
  (1006, '2026-09-14', '华东', 506, 'paid',  90.00,  0.00);

CREATE TABLE shop.order_items (
  order_item_id BIGINT        NOT NULL COMMENT '订单明细号',
  order_id      BIGINT        NOT NULL COMMENT '订单号',
  product_id    BIGINT        NOT NULL COMMENT '商品编号',
  quantity      INT           NOT NULL COMMENT '数量',
  item_amount   DECIMAL(18,2) NOT NULL COMMENT '明细实付金额'
) PRIMARY KEY (order_item_id)
COMMENT '订单明细'
DISTRIBUTED BY HASH(order_item_id) BUCKETS 1
PROPERTIES ("replication_num" = "1");

INSERT INTO shop.order_items VALUES
  (50001, 1001, 3101, 1,  40.00),
  (50002, 1001, 3202, 1,  60.00),
  (50003, 1002, 3001, 2, 200.00),
  (50004, 1003, 3201, 2, 150.00),
  (50005, 1004, 3101, 2,  80.00),
  (50006, 1005, 3202, 1,  60.00),
  (50007, 1006, 3001, 1,  90.00);

CREATE TABLE shop.refund_raw (
  refund_id   BIGINT        NOT NULL COMMENT '退款单号',
  order_id    BIGINT        NOT NULL COMMENT '订单号',
  amount      DECIMAL(18,2) NOT NULL COMMENT '退款金额',
  refund_date DATE          NOT NULL COMMENT '退款日期'
) DUPLICATE KEY (refund_id)
COMMENT '退款明细（一笔订单可有多笔退款）'
DISTRIBUTED BY HASH(refund_id) BUCKETS 1
PROPERTIES ("replication_num" = "1");

INSERT INTO shop.refund_raw VALUES
  (9001, 1001,  4.00, '2026-09-16'),
  (9002, 1001,  6.00, '2026-09-18'),
  (9003, 1002, 20.00, '2026-09-17'),
  (9004, 1005,  5.00, '2026-09-16');

CREATE VIEW shop.daily_sales COMMENT '日销售（v1：实付）' AS
SELECT order_date, region, SUM(paid_amount) AS sales_amount
FROM shop.orders
GROUP BY order_date, region;
