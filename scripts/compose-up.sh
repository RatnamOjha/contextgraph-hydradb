#!/usr/bin/env bash
set -euo pipefail

mkdir -p .hydradb
printf '%s' 'contextgraph-local-development-token-32-bytes' > .hydradb/auth-token
docker compose up --build
