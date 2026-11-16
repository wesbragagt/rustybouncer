# Docker Compose starter

From the repository root, start the example:

```sh
docker compose -f deploy/compose/compose.yaml up -d
nix develop --command pg_isready -h 127.0.0.1 -p 6432 -U postgres -d postgres
nix develop --command psql \
  'host=127.0.0.1 port=6432 user=postgres dbname=postgres sslmode=disable' \
  -c 'SELECT 42, pg_is_in_recovery();'
```

Wait for `pg_isready` to report `accepting connections` before running SQL.
The expected recovery flag is `f`. This is a **single-primary connectivity demo**;
there are no replicas, automatic promotion or high-availability guarantees.
See [CNPG](../cnpg/README.md) for a managed three-instance example.

The Compose file uses PostgreSQL 16 pinned by registry manifest digest, a named
data volume, health-based startup ordering, and the published
`ghcr.io/wesbragagt/rustybouncer:1.0.1` image. To test a local build, load it and set
`RUSTYBOUNCER_IMAGE=rustybouncer:latest`. PostgreSQL
is not published on a host port. The router publishes only on host loopback and
runs as UID/GID 10001 with a read-only rootfs, no capabilities and no-new-privileges.
The included trust authentication is for this isolated local demo only.

[Compose startup ordering](https://docs.docker.com/compose/how-tos/startup-order/)
waits for PostgreSQL's healthcheck before creating the router. Router startup still
needs its own discovery cycle; PostgreSQL readiness alone is not SQL readiness
through the proxy. There is no shell in the router image for a healthcheck.

## Ports, configuration and existing databases

Choose another host port if 6432 is already occupied:

```sh
RUSTYBOUNCER_PORT=16432 docker compose -f deploy/compose/compose.yaml up -d
```

Connect to port 16432 in that case. Set the same variable on later Compose
operations when you want the same configuration. Use `docker compose -p NAME`
for separate project instances; the default project name is `rustybouncer-demo`.

The router mounts [../container.toml](../container.toml) read-only. Relative bind
paths resolve from the Compose file directory, so commands work from the repository
root. The PostgreSQL host is `postgres`, the service name, not `127.0.0.1`.
After editing TOML, restart the router:

```sh
docker compose -f deploy/compose/compose.yaml restart rustybouncer
```

For an existing replication cluster, use your own Compose override/configuration:
remove the demo `postgres` dependency/service, attach rustybouncer to the database
network, and list every actual member under `[[nodes]]`. All nodes must belong
to the same cluster. An external cluster manager must promote/fence primaries;
listing unrelated writable PostgreSQL servers creates ambiguity and blocks routing.

## Observe and stop

```sh
docker compose -f deploy/compose/compose.yaml ps
docker compose -f deploy/compose/compose.yaml logs -f rustybouncer
# Stop containers/network while retaining PostgreSQL data:
docker compose -f deploy/compose/compose.yaml down
```

For an intentionally disposable database, `down --volumes` also deletes this
project's PostgreSQL data volume. Do not use it for data you want to retain.
Trust-init settings apply when PostgreSQL initializes an empty data directory;
changing environment variables does not reinitialize an existing volume.

[Getting started](../../docs/getting-started.md) ·
[Deployment guide](../../docs/deployments.md) ·
Canonical checklist
