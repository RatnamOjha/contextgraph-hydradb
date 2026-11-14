# Deployment

## Container demo

Docker Compose runs the published HydraDB `0.4.0` image with ContextGraph's API. The graph is stored in a named volume, and the HydraDB HTTP/admin ports stay inside the Compose network.

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

## Hosted demo architecture

Use one long-running HydraDB process with durable storage and a separate public API service. The API should reach HydraDB over the host's private network; publish only the API's HTTPS endpoint. For a quick single-node demo, a paid Render private service with a persistent disk can preserve HydraDB's local object-store directory. Mount the disk at `/var/lib/hydradb`, configure `CLOUD_PROVIDER=local` and `LOCAL_PATH=/var/lib/hydradb/store`, and keep the service at one instance. Render documents that its default filesystem is ephemeral, that a disk is single-instance, and that disk-backed deploys stop the old instance before starting the new one. This is suitable for a hackathon demo, with brief deploy downtime.

For a longer-lived deployment, configure HydraDB's SlateDB object store to use an S3-compatible bucket instead of local disk. Store credentials as secrets, keep the graph service private, and follow HydraDB's upstream Helm chart for multi-node deployment. Do not run multiple graph-node processes against one local filesystem volume.

Deployment references:

- [HydraDB README: runtime ports, local store, and HTTP queries](https://github.com/hydra-db/hydradb/blob/main/README.md)
- [HydraDB Helm chart: object storage and graph configuration](https://github.com/hydra-db/hydradb/tree/main/charts/hydradb)
- [Render persistent disks](https://render.com/docs/disks)
- [Render web services and port binding](https://render.com/docs/web-services)

No hosted service has been created yet. The repository contains a containerized demo path; creating a public deployment still requires a hosting account, service creation, domain choice, and production secrets.
