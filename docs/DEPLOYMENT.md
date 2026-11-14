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

The `render.yaml` Blueprint uses one **Render Free web service** to honor the no-cost requirement. The root `Dockerfile` packages the published HydraDB `0.4.0` image with the Python API. Both processes run in one container; HydraDB's HTTP, Bolt, and admin listeners bind to loopback, and only the API binds to Render's public port. On each start, the entrypoint waits for HydraDB, seeds a 100-tool catalog, runs the three-cycle evaluation, and then reports the service ready.

This free setup is a demo deployment with volatile state. Render Free services have an ephemeral filesystem, cannot attach persistent disks, and spin down after 15 minutes without inbound traffic; waking a service takes about a minute. The graph is rebuilt after each sleep, restart, or redeploy. Startup restores the demo automatically, but past execution history does not persist. Render includes 750 free instance hours per workspace each calendar month. If free limits are exhausted without a payment method, Render suspends the free service rather than billing extra usage.

For durable hosted data, HydraDB needs an object store or persistent volume. Render persistent disks require paid services and cannot scale to multiple instances. A paid durable topology would put HydraDB on a private disk-backed service and keep its API separately public. The no-cost Blueprint intentionally does not use that configuration.

Deployment references:

- [HydraDB README: runtime ports, local store, and HTTP queries](https://github.com/hydra-db/hydradb/blob/main/README.md)
- [HydraDB Helm chart: object storage and graph configuration](https://github.com/hydra-db/hydradb/tree/main/charts/hydradb)
- [Render Free service limits](https://render.com/docs/free)
- [Render persistent disk limitations](https://render.com/docs/disks)
- [Render Blueprint reference](https://render.com/docs/blueprint-spec)

No hosted service has been created yet. The Blueprint uses the free plan and creates no paid database or disk resource.
