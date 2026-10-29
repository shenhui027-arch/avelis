-- 演示电商维表库 shop_dim：由业务系统维护的维度表。合成数据，日期固定。
-- StarRocks 通过 JDBC Catalog（shop_mysql）和 MySQL 外部表读取这些表。
-- The init client defaults to latin1; without this the Chinese text is double-encoded.
SET NAMES utf8mb4;
USE shop_dim;

CREATE TABLE dim_region (
  region_code VARCHAR(8)  NOT NULL COMMENT '区域编码',
  region_name VARCHAR(16) NOT NULL COMMENT '区域名称',
  provinces   VARCHAR(64) NOT NULL COMMENT '覆盖省份',
  updated_at  DATETIME    NOT NULL,
  PRIMARY KEY (region_code),
  UNIQUE KEY uk_region_name (region_name)
) COMMENT = '销售区域';

INSERT INTO dim_region VALUES
  ('EC', '华东', '上海、江苏、浙江、安徽', '2026-01-01 00:00:00'),
  ('NC', '华北', '北京、天津、河北',       '2026-01-01 00:00:00'),
  ('SC', '华南', '广东、广西、福建',       '2026-01-01 00:00:00'),
  ('WC', '华西', '四川、重庆、云南',       '2026-01-01 00:00:00');

CREATE TABLE dim_category (
  category_id   INT         NOT NULL COMMENT '类目编号',
  category_name VARCHAR(32) NOT NULL COMMENT '类目名称',
  parent_id     INT             NULL COMMENT '上级类目',
  level         TINYINT     NOT NULL COMMENT '层级，1 为一级类目',
  PRIMARY KEY (category_id)
) COMMENT = '商品类目';

INSERT INTO dim_category VALUES
  (10, '数码', NULL, 1),
  (11, '手机', 10,   2),
  (12, '耳机', 10,   2),
  (20, '家居', NULL, 1),
  (21, '厨具', 20,   2);

CREATE TABLE dim_product (
  product_id   BIGINT        NOT NULL COMMENT '商品编号',
  product_name VARCHAR(64)   NOT NULL COMMENT '商品名称',
  category_id  INT           NOT NULL COMMENT '末级类目',
  brand        VARCHAR(32)   NOT NULL COMMENT '品牌',
  list_price   DECIMAL(18,2) NOT NULL COMMENT '标价',
  status       VARCHAR(16)   NOT NULL COMMENT 'on_sale / off_sale',
  updated_at   DATETIME      NOT NULL,
  PRIMARY KEY (product_id),
  KEY idx_category (category_id)
) COMMENT = '商品';

INSERT INTO dim_product VALUES
  (3001, '星澜手机 S1',    11, '星澜', 100.00, 'on_sale', '2026-01-01 00:00:00'),
  (3101, '星澜耳机 B2',    12, '星澜',  40.00, 'on_sale', '2026-01-01 00:00:00'),
  (3201, '禾木炒锅 30cm',  21, '禾木',  75.00, 'on_sale', '2026-01-01 00:00:00'),
  (3202, '禾木汤锅 24cm',  21, '禾木',  60.00, 'on_sale', '2026-01-01 00:00:00');

CREATE TABLE dim_customer (
  customer_id   BIGINT      NOT NULL COMMENT '客户编号（不含个人信息）',
  member_level  VARCHAR(16) NOT NULL COMMENT 'gold / silver / normal',
  region_code   VARCHAR(8)  NOT NULL COMMENT '常住区域',
  register_date DATE        NOT NULL COMMENT '注册日期',
  updated_at    DATETIME    NOT NULL,
  PRIMARY KEY (customer_id)
) COMMENT = '会员';

INSERT INTO dim_customer VALUES
  (501, 'gold',   'EC', '2025-03-01', '2026-01-01 00:00:00'),
  (502, 'silver', 'EC', '2025-05-12', '2026-01-01 00:00:00'),
  (503, 'gold',   'NC', '2025-07-20', '2026-01-01 00:00:00'),
  (504, 'normal', 'EC', '2026-02-02', '2026-01-01 00:00:00'),
  (505, 'normal', 'SC', '2026-04-18', '2026-01-01 00:00:00'),
  (506, 'silver', 'EC', '2025-11-30', '2026-01-01 00:00:00');
