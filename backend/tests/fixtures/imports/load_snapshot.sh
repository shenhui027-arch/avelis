#!/usr/bin/env bash
# 每日快照（演示：Shell heredoc 与 -e 参数中的 SQL）
DT=${1:-2026-09-15}
mysql -h starrocks -P 9030 -u etl <<SQL
INSERT INTO ads.region_paid_snapshot
SELECT region, SUM(paid_amount) AS paid_total FROM shop.orders WHERE order_date = '${DT}' GROUP BY region;
SQL
mysql -h starrocks -P 9030 -u etl -e "INSERT INTO ads.refund_count SELECT order_id, COUNT(*) AS refund_cnt FROM shop.refund_raw GROUP BY order_id"
