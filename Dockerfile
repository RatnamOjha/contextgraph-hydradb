FROM ghcr.io/hydra-db/hydradb:0.4.0

USER root
RUN apt-get update && apt-get install -y --no-install-recommends \
      gosu python3 python3-venv python3-pip && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt ./
RUN python3 -m venv /opt/contextgraph-venv && \
    /opt/contextgraph-venv/bin/pip install --no-cache-dir -r requirements.txt
COPY backend ./backend
COPY web ./web
COPY data/olist_demo.sqlite ./data/olist_demo.sqlite
COPY data/README.md ./data/README.md
COPY scripts/render_free_entrypoint.py /usr/local/bin/render-free-entrypoint.py
ENV PYTHONPATH=/app PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
ENTRYPOINT ["python3", "/usr/local/bin/render-free-entrypoint.py"]
