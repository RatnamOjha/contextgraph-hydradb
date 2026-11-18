#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HYDRA_SOURCE="${HYDRADB_SOURCE:-$PROJECT_ROOT/hydradb-upstream}"
STATE_DIR="${CONTEXTGRAPH_STATE_DIR:-$PROJECT_ROOT/.hydradb}"
API_PORT="${CONTEXTGRAPH_API_PORT:-8000}"
HYDRA_HTTP_PORT="${HYDRA_HTTP_PORT:-18443}"
HYDRA_BOLT_PORT="${HYDRA_BOLT_PORT:-17687}"
HYDRA_ADMIN_PORT="${HYDRA_ADMIN_PORT:-19091}"
CATALOG_SIZE="${CONTEXTGRAPH_CATALOG_SIZE:-100}"
CATALOG_SCOPE="${CONTEXTGRAPH_CATALOG_SCOPE:-combined}"
HYDRA_PROVIDER="${CONTEXTGRAPH_HYDRA_PROVIDER:-memory}"

mkdir -p "$STATE_DIR/store" "$STATE_DIR/cache" "$STATE_DIR/logs"
if [[ ! -f "$HYDRA_SOURCE/Cargo.toml" ]]; then
  git clone https://github.com/hydra-db/hydradb.git "$HYDRA_SOURCE"
fi

if command -v brew >/dev/null; then
  NATIVE_PREFIX="$(brew --prefix)"
  export BINDGEN_EXTRA_CLANG_ARGS="-I$NATIVE_PREFIX/include"
  export LIBRARY_PATH="$NATIVE_PREFIX/lib${LIBRARY_PATH:+:$LIBRARY_PATH}"
  if [[ -d "$NATIVE_PREFIX/opt/libcypher-parser/lib/pkgconfig" ]]; then
    export PKG_CONFIG_PATH="$NATIVE_PREFIX/opt/libcypher-parser/lib/pkgconfig${PKG_CONFIG_PATH:+:$PKG_CONFIG_PATH}"
  fi
fi
export RUST_MIN_STACK=33554432

if [[ ! -x "$HYDRA_SOURCE/target/debug/graph-node" ]]; then
  (cd "$HYDRA_SOURCE" && cargo build --locked --features server-runtime --bin graph-node)
fi

printf '%s\n' "contextgraph-local-development-token-32-bytes" > "$STATE_DIR/auth-token"
export CLOUD_PROVIDER="$HYDRA_PROVIDER"
if [[ "$HYDRA_PROVIDER" == "local" ]]; then
  export LOCAL_PATH="$STATE_DIR/store"
else
  unset LOCAL_PATH || true
fi
export GRAPH_NAMESPACE=contextgraph GRAPH_ID=default GRAPH_CELL_ID=cell-0 GRAPH_CELLS=cell-0 GRAPH_DATA_PATH=data
export GRAPH_ALLOW_PLAINTEXT=true GRAPH_AUTH_TOKEN_FILE="$STATE_DIR/auth-token"
export GRAPH_DATA_CACHE_BYTES=67108864 GRAPH_DATA_CACHE_DIR="$STATE_DIR/cache"
export GRAPH_NODE_ID=node-0 GRAPH_BOLT_NODE_ADDRESSES="node-0=127.0.0.1:$HYDRA_BOLT_PORT"
export GRAPH_BOLT_ADDR="127.0.0.1:$HYDRA_BOLT_PORT" GRAPH_ADVERTISED_BOLT_ADDR="127.0.0.1:$HYDRA_BOLT_PORT"
export GRAPH_HTTP_ADDR="127.0.0.1:$HYDRA_HTTP_PORT" GRAPH_ADMIN_ADDR="127.0.0.1:$HYDRA_ADMIN_PORT"
export GRAPH_USAGE_REPORT_ENABLED=false RUST_LOG="${RUST_LOG:-warn}"
export HYDRA_URL="http://127.0.0.1:$HYDRA_HTTP_PORT" HYDRA_NAMESPACE=contextgraph HYDRA_GRAPH=default HYDRA_CELL=cell-0
export HYDRA_TOKEN="$(cat "$STATE_DIR/auth-token")"

HYDRA_PID=""; API_PID=""
cleanup() {
  [[ -z "$API_PID" ]] || kill -TERM "$API_PID" 2>/dev/null || true
  [[ -z "$HYDRA_PID" ]] || kill -TERM "$HYDRA_PID" 2>/dev/null || true
  [[ -z "$API_PID" ]] || wait "$API_PID" 2>/dev/null || true
  [[ -z "$HYDRA_PID" ]] || wait "$HYDRA_PID" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

"$HYDRA_SOURCE/target/debug/graph-node" >"$STATE_DIR/logs/hydradb.log" 2>&1 & HYDRA_PID=$!
for _ in $(seq 1 180); do
  if curl -fsS "http://127.0.0.1:$HYDRA_ADMIN_PORT/readyz" >/dev/null 2>&1; then break; fi
  if ! kill -0 "$HYDRA_PID" 2>/dev/null; then cat "$STATE_DIR/logs/hydradb.log"; exit 1; fi
  sleep .5
done
curl -fsS "http://127.0.0.1:$HYDRA_ADMIN_PORT/readyz" >/dev/null

if [[ ! -x "$PROJECT_ROOT/.venv/bin/uvicorn" ]]; then
  python3 -m venv "$PROJECT_ROOT/.venv"
  "$PROJECT_ROOT/.venv/bin/python" -m pip install -r "$PROJECT_ROOT/requirements.txt"
fi
if [[ "${CONTEXTGRAPH_SKIP_DEMO_SEED:-0}" == "1" ]]; then
  "$PROJECT_ROOT/.venv/bin/python" -m pip install -e "$PROJECT_ROOT/packages/contextgraph-sdk"
fi
(cd "$PROJECT_ROOT" && .venv/bin/python -m uvicorn backend.app:app --host 0.0.0.0 --port "$API_PORT") >"$STATE_DIR/logs/contextgraph.log" 2>&1 & API_PID=$!
for _ in $(seq 1 60); do
  READY_PATH="/api/health"
  if [[ "${CONTEXTGRAPH_SKIP_DEMO_SEED:-0}" == "1" ]]; then READY_PATH="/openapi.json"; fi
  curl -fsS "http://127.0.0.1:$API_PORT$READY_PATH" >/dev/null 2>&1 && break
  sleep .5
done
if [[ "${CONTEXTGRAPH_SKIP_DEMO_SEED:-0}" == "1" ]]; then
  echo "Starting an unseeded SDK workspace; developer catalog writes are enabled only for loopback requests."
else
  curl -fsS -X POST "http://127.0.0.1:$API_PORT/api/seed" -H 'Content-Type: application/json' -d "{\"catalog_size\":$CATALOG_SIZE,\"catalog_scope\":\"$CATALOG_SCOPE\",\"reset\":true}"
  curl -fsS -X POST "http://127.0.0.1:$API_PORT/api/evaluate" -H 'Content-Type: application/json' -d '{"catalog_size":'"$CATALOG_SIZE"',"iterations":3}' > "$STATE_DIR/logs/initial-evaluation.json"
  echo
fi
echo "ContextGraph is ready at http://127.0.0.1:$API_PORT"
echo "HydraDB provider: $HYDRA_PROVIDER"
if [[ "$HYDRA_PROVIDER" == "memory" ]]; then
  echo "HydraDB graph is in memory and is reseeded when this script starts."
else
  echo "HydraDB data: $STATE_DIR/store"
fi
echo "Logs: $STATE_DIR/logs"
echo "Press Ctrl-C to stop both processes."
wait "$API_PID"
