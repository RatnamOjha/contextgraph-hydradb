#!/usr/bin/env python3
"""Run HydraDB and the API together on a free, ephemeral demo instance."""
from __future__ import annotations

import json
import os
import signal
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

API_PROCESS: subprocess.Popen[bytes] | None = None
HYDRA_PROCESS: subprocess.Popen[bytes] | None = None


def stop_children(*_: object) -> None:
    for process in (API_PROCESS, HYDRA_PROCESS):
        if process is not None and process.poll() is None:
            process.terminate()


def request(path: str, payload: dict | None = None) -> dict:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        f"http://127.0.0.1:{os.environ.get('PORT', '10000')}{path}",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST" if data is not None else "GET",
    )
    with urllib.request.urlopen(req, timeout=10) as response:
        return json.loads(response.read())


def configure() -> None:
    token = os.environ.get("HYDRADB_TOKEN", "").strip()
    if len(token) < 32:
        raise RuntimeError("HYDRADB_TOKEN must contain at least 32 characters")
    store = Path("/tmp/contextgraph-hydradb")
    (store / "store").mkdir(parents=True, exist_ok=True)
    (store / "cache").mkdir(parents=True, exist_ok=True)
    token_file = Path("/tmp/contextgraph-hydradb-token")
    token_file.write_text(token)
    token_file.chmod(0o600)
    subprocess.run(["chown", "10001:10001", str(store), str(store / "store"), str(store / "cache"), str(token_file)], check=True)

    os.environ.update({
        "CLOUD_PROVIDER": "local",
        "LOCAL_PATH": str(store / "store"),
        "GRAPH_NAMESPACE": "contextgraph",
        "GRAPH_ID": "default",
        "GRAPH_CELL_ID": "cell-0",
        "GRAPH_CELLS": "cell-0",
        "GRAPH_DATA_PATH": "data",
        "GRAPH_NODE_ID": "node-0",
        "GRAPH_BOLT_NODE_ADDRESSES": "node-0=127.0.0.1:17687",
        "GRAPH_BOLT_ADDR": "127.0.0.1:17687",
        "GRAPH_ADVERTISED_BOLT_ADDR": "127.0.0.1:17687",
        "GRAPH_HTTP_ADDR": "127.0.0.1:18443",
        "GRAPH_ADMIN_ADDR": "127.0.0.1:19091",
        "GRAPH_AUTH_TOKEN_FILE": str(token_file),
        "GRAPH_ALLOW_PLAINTEXT": "true",
        "GRAPH_DATA_CACHE_DIR": str(store / "cache"),
        "GRAPH_DATA_CACHE_BYTES": "67108864",
        "GRAPH_USAGE_REPORT_ENABLED": "false",
        "RUST_MIN_STACK": "33554432",
        "RUST_LOG": "warn",
        "HYDRA_URL": "http://127.0.0.1:18443",
        "HYDRA_GRAPH": "default",
        "HYDRA_NAMESPACE": "contextgraph",
        "HYDRA_CELL": "cell-0",
        "HYDRA_TOKEN": token,
    })


def main() -> None:
    global API_PROCESS, HYDRA_PROCESS
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, stop_children)
    configure()
    HYDRA_PROCESS = subprocess.Popen(["gosu", "10001:10001", "/usr/local/bin/graph-node"], env=os.environ.copy())
    API_PROCESS = subprocess.Popen(
        ["gosu", "10002:10001", "/opt/contextgraph-venv/bin/uvicorn", "backend.app:app", "--host", "0.0.0.0", "--port", os.environ.get("PORT", "10000")],
        env=os.environ.copy(),
    )

    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        if HYDRA_PROCESS.poll() is not None or API_PROCESS.poll() is not None:
            raise RuntimeError("HydraDB or the API exited during startup")
        try:
            request("/api/health")
            break
        except (OSError, urllib.error.URLError, TimeoutError):
            time.sleep(1)
    else:
        raise TimeoutError("HydraDB/API did not become ready within 180 seconds")

    size = int(os.environ.get("CONTEXTGRAPH_CATALOG_SIZE", "100"))
    request("/api/seed", {"catalog_size": size, "reset": True})
    request("/api/evaluate", {"catalog_size": size, "iterations": 3})
    request("/api/ready")
    print(f"ContextGraph bootstrapped at catalog_size={size}", flush=True)

    while API_PROCESS.poll() is None:
        if HYDRA_PROCESS.poll() is not None:
            raise RuntimeError("HydraDB exited while the API was serving")
        time.sleep(1)
    raise SystemExit(API_PROCESS.returncode or 0)


if __name__ == "__main__":
    try:
        main()
    except BaseException:
        stop_children()
        raise
