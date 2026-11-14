#!/bin/sh
set -eu
: "${HYDRADB_TOKEN:?Set HYDRADB_TOKEN through the host secret manager}"
mkdir -p "${LOCAL_PATH:-/var/lib/hydradb/store}" "${GRAPH_DATA_CACHE_DIR:-/var/cache/slatedb/data}"
chown 10001:10001 /var/lib/hydradb /var/lib/hydradb/store /var/cache/slatedb /var/cache/slatedb/data
printf '%s' "$HYDRADB_TOKEN" > /tmp/contextgraph-hydradb-token
chown 10001:10001 /tmp/contextgraph-hydradb-token
chmod 0600 /tmp/contextgraph-hydradb-token
export GRAPH_AUTH_TOKEN_FILE=/tmp/contextgraph-hydradb-token
exec gosu 10001:10001 /usr/local/bin/graph-node
