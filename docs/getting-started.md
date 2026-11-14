# Getting started

Run commands from the repository root. Start with the Compose demo below for a
complete PostgreSQL + rustybouncer setup, or follow the native-binary steps if you
already have PostgreSQL. The [deployment guide](deployments.md) covers single
containers, Docker Compose and CloudNativePG.

Shared session and transaction pools support TLS, rotating PEM credentials and
SCRAM authentication through `auth_query`. The local Compose example uses trust
authentication on an isolated network. Follow the CNPG guide for the secured setup.
Dispatched writes and interrupted transactions are never replayed automatically.
This is experimental software; broader production acceptance remains incomplete.

## Start PostgreSQL and rustybouncer with Compose

Install Nix with flakes enabled, Docker Engine with Compose v2, and start Docker.
The locked Nix environment supplies Rust, `psql`, Python and verification tools.
Build/load the local image, then start the supplied stack:

```sh
nix build .#docker --no-update-lock-file --out-link result-image
docker load -i result-image
docker compose -f deploy/compose/compose.yaml up -d
nix develop --command pg_isready -h 127.0.0.1 -p 6432 -U postgres -d postgres
```

Wait until the last command reports `accepting connections`; repeat it if the
router is still starting. Connect and run SQL:

```sh
nix develop --command psql \
  'host=127.0.0.1 port=6432 user=postgres dbname=postgres sslmode=disable' \
  -c 'SELECT 42, pg_is_in_recovery();'
```

Expect `42` and `f`: this starter has **one primary and no replicas**. SELECTs
therefore fall back to the primary. It demonstrates connectivity, not failover or
read balancing. PostgreSQL persists in a Compose-managed named volume, and only
the router port is published on host loopback.

```sh
docker compose -f deploy/compose/compose.yaml logs -f rustybouncer
# Stop services, preserving the database volume:
docker compose -f deploy/compose/compose.yaml down
```

See the [Compose guide](../deploy/compose/README.md) for port changes, persistence,
explicit data deletion and integration with an existing replication cluster.

## Run a native Rust binary against existing PostgreSQL

Enter the toolchain and build:

```sh
nix develop
cargo build --locked
cp rustybouncer.toml config.local.toml
```

Edit `config.local.toml` before starting. Set each `[[nodes]]` host/port to an
actual member of your PostgreSQL replication cluster; remove unused entries.
The checked-in sample ports 55432–55434 are placeholders, not running servers.
For an initial single-node trial, keep just the reachable primary entry.
Set `probe_user`/`probe_database` to a role/database allowed on every node, and
ensure the client's original user/database can connect through the proxy too.

```sh
./target/debug/rustybouncer config.local.toml
```

In another terminal:

```sh
nix develop --command psql \
  'host=127.0.0.1 port=6432 user=postgres dbname=postgres sslmode=disable'
```

Use the actual database/user names from your backend. To observe replica routing,
issue separate statements on the same connection:

```sql
SELECT inet_server_addr(), pg_is_in_recovery();
SELECT inet_server_addr(), pg_is_in_recovery();
WITH answer AS (SELECT 42 AS value) SELECT value FROM answer;
```

With two healthy replicas, the first two statements should show different replica
addresses and `t`. On a single primary they show the primary and `f`. Replicas may
lag writes. Use an explicit transaction for a primary read after a write:

```sql
BEGIN;
CREATE TABLE IF NOT EXISTS example (id integer PRIMARY KEY);
INSERT INTO example VALUES (1) ON CONFLICT DO NOTHING;
SELECT * FROM example;
COMMIT;
```

`SET`, temporary objects and other session state pin the connection to the primary;
use a fresh connection when experimenting with replica routing. Drivers must use
the simple Query protocol in unshared routing mode, or select a shared pool for
extended/prepared protocol. The [main guide](routing.md#query-routing-mode)
explains classification, CTEs, authentication, buffering and retry limits.

## Try managed failover with CloudNativePG

For a retained Kubernetes demo, follow the [CNPG deployment guide](../deploy/cnpg/README.md).
For a disposable cluster that automatically injects failures and validates them:

```sh
nix develop --command python3 -m tests.kind_cnpg --output /tmp/rustybouncer-kind-results
```

Build/load the image first. Use a fresh output directory each time. This runs the
complete five-case suite against four kind nodes and three real CNPG PostgreSQL
instances, writes JSON/JUnit/diagnostics, and removes the cluster afterwards.
Allow roughly ten minutes and at least 8 GiB available memory plus 10 GiB disk.
See [test details](../tests/kubernetes/README.md).

## Verify your changes

```sh
nix flake check --no-update-lock-file
```

The installed commit hook checks the staged snapshot, including Rust checks,
Ruff, Astral ty and complexity <=30. [Development instructions](development.md)
list when live integration suites are required. Track remaining work in
the local feature checklist.
