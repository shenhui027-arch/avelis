#!/bin/bash
# Low-resource settings for the demo StarRocks all-in-one container, applied
# once per container before the image's own entrypoint starts FE and BE.
set -e

SR_HOME="${SR_HOME:-/data/deploy/starrocks}"
FE_CONF="$SR_HOME/fe/conf/fe.conf"
BE_CONF="$SR_HOME/be/conf/be.conf"
MARKER="# avelis-demo low-resource settings"

if ! grep -qF "$MARKER" "$FE_CONF"; then
  cat >> "$FE_CONF" <<EOF

$MARKER
JAVA_OPTS="-Dlog4j2.formatMsgNoLookups=true -Xmx${AVELIS_SR_FE_HEAP_MB:-1024}m -XX:+UseG1GC -XX:ErrorFile=\${LOG_DIR}/hs_err_pid%p.log -Djava.security.policy=\${STARROCKS_HOME}/conf/udf_security.policy"
EOF
fi

if ! grep -qF "$MARKER" "$BE_CONF"; then
  cat >> "$BE_CONF" <<EOF

$MARKER
mem_limit = ${AVELIS_SR_BE_MEM_MB:-3072}M
EOF
fi

# The image's director registers the BE as root on every start. On the very
# first start root has no password; scripts/demo.py sets it once the BE is
# alive. From then on the director needs that password via MYSQL_PWD.
if [ -f "$SR_HOME/fe/meta/image/VERSION" ] && [ -n "$AVELIS_SR_ROOT_PASSWORD" ]; then
  export MYSQL_PWD="$AVELIS_SR_ROOT_PASSWORD"
else
  unset MYSQL_PWD
fi
unset AVELIS_SR_ROOT_PASSWORD

cd /data/deploy
exec ./entrypoint.sh
