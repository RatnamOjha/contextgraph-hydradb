# Deployment

## Container demo

Docker Compose builds a small wrapper around the published HydraDB `0.4.0` image and runs it with ContextGraph's API. The wrapper writes the token from an environment variable, then drops privileges to the upstream `graph` user. The graph is stored in a named volume, and HydraDB HTTP/admin ports stay inside the Compose network.

```bash
./scripts/compose-up.sh
```

After both services start, wait until HydraDB responds to the API health probe, then seed and evaluate the demo catalog once:

```bash
until curl -fsS http://127.0.0.1:8000/api/health | grep -q '"ok":true'; do sleep 1; done
curl -X POST http://127.0.0.1:8000/api/seed \
  -H 'Content-Type: application/json' \
  -d '{"catalog_size":100,"reset":true}'
curl -X POST http://127.0.0.1:8000/api/evaluate \
  -H 'Content-Type: application/json' \
  -d '{"catalog_size":100,"iterations":3}'
```

Open `http://127.0.0.1:8000`. To stop the services without deleting the graph, press Ctrl-C. `docker compose down -v` deletes the named graph volume.

The Compose file uses a demo token and plaintext only within the private container network. Replace both before exposing the API beyond a trusted demo. Keep the HydraDB HTTP, Bolt, and admin ports private. For hosted use, inject a long random token through the platform's secret manager and enable TLS or private networking.

## Hosted demo

The `render.yaml` Blueprint uses one **Render Free web service** to honor the no-cost requirement. The root `Dockerfile` packages the published HydraDB `0.4.0` image with the Python API and bundled Olist subset. Both processes run in one container; HydraDB's HTTP, Bolt, and admin listeners bind to loopback, and only the API binds to Render's public port. On each start, the entrypoint waits for HydraDB, seeds the graph, warms the deterministic evaluation, and then reports the service ready. `GROQ_API_KEY` is declared as a non-synced secret; add it through the Render service environment settings to enable hosted agent mode. A local `.env` is not uploaded.

This free setup is a demo deployment with volatile state. Render Free services have an ephemeral filesystem, cannot attach persistent disks, and spin down after 15 minutes without inbound traffic; waking a service takes about a minute. The graph is rebuilt after each sleep, restart, or redeploy. Startup restores the demo automatically, but past execution history does not persist. Render includes 750 free instance hours per workspace each calendar month. If free limits are exhausted without a payment method, Render suspends the free service rather than billing extra usage.

For durable hosted data, HydraDB needs an object store or persistent volume. Render persistent disks require paid services and cannot scale to multiple instances. A paid durable topology would put HydraDB on a private disk-backed service and keep its API separately public. The no-cost Blueprint intentionally does not use that configuration.

Deployment references:

- [HydraDB README: runtime ports, local store, and HTTP queries](https://github.com/hydra-db/hydradb/blob/main/README.md)
- [HydraDB Helm chart: object storage and graph configuration](https://github.com/hydra-db/hydradb/tree/main/charts/hydradb)
- [Render Free service limits](https://render.com/docs/free)
- [Render persistent disk limitations](https://render.com/docs/disks)
- [Render Blueprint reference](https://render.com/docs/blueprint-spec)

The public free service is live at [contextgraph-demo.onrender.com](https://contextgraph-demo.onrender.com). It has no paid database or disk resource. The deployment's generated HydraDB token is stored as a Render environment secret and is not in this repository. The bundled Olist subset is licensed CC BY-NC-SA 4.0, so this public demo must remain non-commercial unless separately licensed.

## Groq credential handling and status

For local development, copy `.env.example` to the ignored `.env` file and set `GROQ_API_KEY` there. Do not commit `.env`, paste keys into chat, or place credentials in the frontend. `.env.example` documents variable names only.

For Render, `render.yaml` declares `GROQ_API_KEY` with `sync: false` and declares the non-secret `GROQ_MODEL`. The actual key is configured outside Git in the Render service environment. As of November 15, 2026, the hosted `/api/agent/status` reported the Groq key configured, dataset present, and model `openai/gpt-oss-20b`. That endpoint only checks configuration; `provider_call_status: not_probed` is deliberate. A local bounded resume observed HTTP 429 and the provider result was not persisted because the local HydraDB checkpoint write failed. The post-push hosted smoke confirmed API health/readiness and configured agent status, but deliberately made no hosted provider call. Successful hosted provider operation is **not verified**.

The API does not return the credential. It returns only the configured flag, provider/model names, dataset flag, and `not_probed` status. Provider error responses include a sanitized error category and message, never request authorization headers.

## Evaluation checkpoint persistence

Real-agent evaluation attempts to save immutable checkpoint revisions in HydraDB after each case-strategy pair. If the HydraDB query runtime rejects that mutation, the runner writes the snapshot atomically to the ignored `data/evaluation-checkpoints/` directory. This sidecar supports resume while the instance and filesystem remain available; Render Free storage is ephemeral, so it is not a durable replacement for a working HydraDB checkpoint write. The current local HydraDB build returned HTTP 500 for the `UNWIND/MERGE/SET` checkpoint mutation during final verification. The existing HydraDB checkpoint remains readable and is preserved.

Learning demonstrations use the same verified retrieval-filter behavior and keep the `DEPENDS_ON` edge unchanged. Evidence is written to a HydraDB `LearningUpdate` record when that writer is available; if it rejects the write, an atomic JSON record in ignored `data/learning-updates/` supports recovery and rollback while that instance filesystem remains available. The API reports which store holds the record. Render Free restarts may discard this fallback state.
