#!/bin/sh
# Read-only accounts for the demo store database. Passwords come from the container
# environment (.env), never from this file.
set -e
psql -v ON_ERROR_STOP=1 -U postgres -d storedb <<EOSQL
CREATE ROLE avelis_collector LOGIN PASSWORD '${SHOP_PG_COLLECTOR_PASSWORD}';
CREATE ROLE avelis_reader LOGIN PASSWORD '${SHOP_PG_READER_PASSWORD}';
ALTER ROLE avelis_reader SET default_transaction_read_only = on;
EOSQL
