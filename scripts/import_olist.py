#!/usr/bin/env python3
"""Import a deterministic Olist subset without extracting or committing raw CSVs."""
from __future__ import annotations

import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.olist_store import import_olist


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", default="data/raw/olist.zip", help="Official Kaggle dataset ZIP")
    parser.add_argument("--output", default="data/olist_demo.sqlite")
    parser.add_argument("--orders", type=int, default=500, help="latest historical orders to retain (max 10000)")
    args = parser.parse_args()
    print(import_olist(Path(args.archive), Path(args.output), args.orders))


if __name__ == "__main__":
    main()
