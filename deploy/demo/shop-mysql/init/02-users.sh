#!/bin/bash
# Read-only accounts for the demo dimension database. Passwords come from the
# container environment (.env), never from this file.
#
# sr_legacy exists only for StarRocks' legacy ENGINE=mysql external tables: the
# BE's bundled MySQL client cannot load caching_sha2_password, so that account
# uses mysql_native_password (enabled on this server with --mysql-native-password=ON).
set -e

mysql --protocol=socket -uroot -p"${MYSQL_ROOT_PASSWORD}" <<EOSQL
CREATE USER IF NOT EXISTS 'avelis_collector'@'%' IDENTIFIED BY '${SHOP_MYSQL_COLLECTOR_PASSWORD}';
GRANT SELECT, SHOW VIEW ON shop_dim.* TO 'avelis_collector'@'%';

CREATE USER IF NOT EXISTS 'sr_catalog'@'%' IDENTIFIED BY '${SHOP_MYSQL_CATALOG_PASSWORD}';
GRANT SELECT ON shop_dim.* TO 'sr_catalog'@'%';

CREATE USER IF NOT EXISTS 'sr_legacy'@'%' IDENTIFIED WITH mysql_native_password BY '${SHOP_MYSQL_LEGACY_PASSWORD}';
GRANT SELECT ON shop_dim.* TO 'sr_legacy'@'%';

FLUSH PRIVILEGES;
EOSQL
