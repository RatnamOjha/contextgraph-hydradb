"""Tiny .env reader for local development; server secrets still use environment variables."""
from __future__ import annotations

import os
from pathlib import Path


def load_env_file(path: str | Path | None = None) -> None:
    target = Path(path or os.getenv("CONTEXTGRAPH_ENV_FILE", ".env"))
    if not target.is_file(): return
    for raw in target.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line: continue
        key, value = line.split("=", 1)
        key, value = key.strip().removeprefix("export ").strip(), value.strip()
        if key and key.replace("_", "").isalnum() and key not in os.environ:
            if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
                value = value[1:-1]
            os.environ[key] = value
