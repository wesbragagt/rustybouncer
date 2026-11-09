# rustybouncer

Experimental Rust PostgreSQL router with automatic primary discovery, failover
waiting, and opt-in round-robin routing of conservative SELECTs to replicas.

See [design.md](design.md) for the architecture, routing decisions, failover
boundaries, and implementation tradeoffs.

## Getting started and deployments

Follow [Getting started](docs/getting-started.md) for a complete Compose demo or a
native-binary connection to existing PostgreSQL. The [deployment guide](docs/deployments.md)
covers local processes, Docker/Podman, [Docker Compose](deploy/compose/README.md),
and [CloudNativePG](deploy/cnpg/README.md), with configuration, verification and cleanup.

Enable the opt-in Prometheus endpoint with top-level `metrics_listen = "127.0.0.1:9090"`.
The [metrics guide](docs/metrics.md) covers accounting boundaries, private scrape
examples and tested recording/alert rules; the [reviewed plan](docs/plans/prometheus-metrics.md)
records the implementation and validation contract.
See [Grafana dashboard recommendations](docs/grafana.md) for the included router
dashboard and complementary PostgreSQL, CNPG and infrastructure dashboards.

## PgBouncer comparison

Historical baseline `c8997e8`, measured on 2026-11-08: PgBouncer **1.26.1**,
the Rustybouncer release at that commit,
and the same PostgreSQL 16 primary. A random indexed lookup over 10,000 rows
returns a 128-byte payload; clients stay connected and use simple query protocol.
Each cell summarizes three 30-second runs after 5-second warmups, with rotated
execution order. Latencies are medians of per-run sampled quantiles (10% random
sampling); backend peaks are independently observed via PostgreSQL. All 30 arms
completed with **zero query errors**. TLS and metrics are disabled; authentication
is trust on an isolated loopback fixture.

| Target | Clients | Median Q/s | p50 ms | p95 ms | p99 ms | Peak PG backends |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Direct PostgreSQL | 10 | 222,569 | 0.039 | 0.072 | 0.116 | 10 |
| PgBouncer session | 10 | 157,913 | 0.060 | 0.083 | 0.121 | 10 |
| PgBouncer transaction | 10 | 155,583 | 0.061 | 0.083 | 0.125 | 10 |
| Rustybouncer passthrough | 10 | 156,937 | 0.058 | 0.092 | 0.155 | 10 |
| Rustybouncer routed → primary | 10 | 67,520 | 0.124 | 0.268 | 0.426 | 10 |
| Direct PostgreSQL | 100 | 332,702 | 0.203 | 0.814 | 1.732 | 100 |
| PgBouncer session | 100 | 152,507 | 0.621 | 0.849 | 1.277 | 100 |
| PgBouncer transaction | 100 | 152,280 | 0.606 | 0.951 | 1.344 | 100 |
| Rustybouncer passthrough | 100 | 186,693 | 0.508 | 0.915 | 1.436 | 100 |
| Rustybouncer routed → primary | 100 | 93,031 | 0.987 | 1.942 | 2.674 | 100 |

In this historical baseline, routed Rustybouncer is slower than both PgBouncer modes; it
classifies and buffers each query. Passthrough has different semantics and cannot
stand in for routed-mode performance. At 100 clients its throughput exceeds the
single PgBouncer process, but this is **not an equal-core efficiency comparison**:
Tokio can use multiple cores, while one PgBouncer instance uses one core. The host
has 16 logical CPUs and also runs other workloads. PgBouncer supports multiple
instances/SO_REUSEPORT; that setup was not measured.
[PgBouncer CPU configuration](https://www.pgbouncer.org/config.html#so_reuseport).

Use these as local observations, not a production ranking or SLO. All modes target
one primary with maximum client/pool capacity 256 and PostgreSQL capacity 400.
All modes reached the same observed backend count here; the benchmark does not
establish pooling savings for idle clients or connection churn.
The test does not measure replica read scaling, failover, connection churn,
prepared statements, writes, TLS or large-result memory pressure.
[Reproduce, individual runs, ranges and provenance](docs/benchmarks/pgbouncer-comparison.md).

The [performance improvement task backlog](docs/tasks/pgbouncer-performance.md)
records later wire/classifier studies and the remaining tail-latency/shared-pooling
work. A final combined comparison remains pending; this table is preserved as
historical evidence.

### Advantages and trade-offs

| Area | PgBouncer | Rustybouncer today |
| --- | --- | --- |
| Backend capacity | Shared pools reuse connections across clients; transaction mode can release them after each transaction | Opt-in shared primary session reuse; default routed mode caches per-client connections. Transaction leases are an experimental partial slice; PERF-008 acceptance remains open |
| Read routing | Host/DNS selection chooses connection destinations; no SQL purity classifier | Conservative SELECT/read-only CTE routing to replicas, with primary fallback |
| Primary changes | Integrates with external host/DNS/reload/reconnect orchestration | Probes PostgreSQL roles, waits for a unique primary, and retries eligible buffered reads before exposing results |
| Protocol and security | Authentication/TLS and broad protocol support; transaction pooling has session-state restrictions | Routed mode requires trust; shared pools support extended queries, while unshared routing supports simple Query only. No COPY or TLS. Routed cancellation uses virtual frontend identities. Passthrough preserves backend protocol/authentication |
| Response and memory | Streams protocol traffic | Simple Query buffers to ReadyForQuery; extended exchanges buffer to native terminals before client Sync. Limits are 32 MiB/exchange and 16 MiB/message, with a 64-MiB default process bulk-storage budget and separate frontend/runtime overhead |
| Operations | Administrative console, reload and pool controls | Prometheus/Grafana support and container/CNPG examples; reload, shared-pool controls and graceful draining remain open |

Choose PgBouncer when your main need is shared connection pooling and established
protocol/security/administrative features. Transaction pooling requires compatible
application session behavior; protocol prepared statements need appropriate
configuration. [Upstream feature map](https://www.pgbouncer.org/features.html).

Rustybouncer is an experimental option for evaluating role-aware routing and
buffered read continuity. Both need external promotion, replication and fencing;
interrupted transactions and uncertain writes require application handling and
are never transparently replayed by Rustybouncer. Modern PgBouncer host lists and
DNS selection are connection-level mechanisms, distinct from SQL-aware routing.
[Host configuration](https://www.pgbouncer.org/config.html#host),
[failover integration](https://www.pgbouncer.org/faq.html#how-to-failover).
[FEATURE_COMPLETENESS.md](FEATURE_COMPLETENESS.md) remains authoritative; these
benchmarks do not establish the full workload or release checklist.

## Development verification

Use `nix develop` for the Iceman-generated development environment and local commit
hook installation. `nix flake check --no-update-lock-file` enforces Rust typechecking,
strict Clippy, formatting, unit tests, Ruff, Astral ty, and cyclomatic complexity
(maximum 30). See [development.md](docs/development.md) for staged-snapshot hook
behavior, first-use dependencies, and exact checks.

## Run against existing PostgreSQL

Configure reachable backend addresses first; the sample does not start PostgreSQL.
See the [native quickstart](docs/getting-started.md#run-a-native-rust-binary-against-existing-postgresql).

```sh
cargo build --locked
cargo run --locked -- rustybouncer.toml
psql 'host=127.0.0.1 port=6432 user=postgres dbname=postgres sslmode=disable'
```

Configure your primary and replicas in `rustybouncer.toml`; the sample lists ports
55432–55434. All nodes must be members of the same streaming-replication cluster.
The router does not validate cluster identity, elect leaders, fence nodes, or
promote standbys. Run a cluster manager such as CloudNativePG or Patroni for those jobs.

The sample enables `route_selects = true`. The default when omitted is `false`,
which preserves the original session router. `RUST_LOG=rustybouncer=debug` changes
logging. Ctrl-C stops discovery and closes active sessions. Configuration changes
require a restart. Probe authentication may use a password via the environment
variable named by `probe_password_env`; never put secrets in the TOML file.

## Shared session reuse

Set `pool_mode = "session"` with `route_selects = true` to reuse primary backends
across successive clients. The first SQL command assigns an exclusive backend
until disconnect, preserving session affinity. Idle frontends can finish startup
from verified exact-identity metadata without holding a backend. This mode sends
all SQL to the primary; `pool_mode = "none"` retains replica round robin.

`pool_size` (default 16) bounds each exact startup identity/node/generation;
`max_pool_backends` (64) bounds the process, including connect/reset/idle ownership.
Startup user, database, application name and options partition pools without
normalization. Client work always uses its original identity, never probe credentials.
On clean close, rollback open/aborted transactions, DISCARD ALL, verify idle state
and baseline parameters before reuse. Reset errors, unexpected idle bytes, expired
idle/lifetime limits or stale generations discard. Cancellation credentials are
fresh per frontend and cannot target a later owner of a reused backend.

Pool settings in [rustybouncer.toml](rustybouncer.toml) include bounded wait,
reserve delay and retirement/reset timeouts. `pool_mode = "transaction"` currently
implements transaction leases: hold actual T/E transactions,
release after verified idle and cancellation fencing, and retain explicitly
recognized session state. Savepoints, rollback, isolation and serialization errors
keep PostgreSQL semantics. Known primary-only observations such as pg_backend_pid()
can release a lease without becoming retryable replica reads. Internal DISCARD ALL
runs before reuse; its failure discards the backend and preserves known user outcomes.

The capacity/queue slice adds bounded FIFO admission, cancellation/disconnect
removal, reserve creation after its configured delay and independent-key progress.
A reader remains active during metadata bootstrap, including pipelined input.
Explicit pool phase/queue metrics distinguish backend saturation from frontend
admission. Positive tests provision all 10/100/1,000 frontend levels.

**PERF-008 is incomplete:** full combined protocol verification and the complete
PgBouncer comparison remain required work. This partial prototype must not be treated as
full transaction-pool conformance.
Shared pooling uses routed trust authentication and the bounded response contract
described below. The historical benchmark
above did not measure this feature; final comparisons remain pending.

## Query-routing mode

**Requires trusted local networking and PostgreSQL trust authentication for client
backend connections.** This mode does not authenticate clients itself. It forwards
the original startup user, database and parameters to each backend and only accepts
AuthenticationOk; any backend requesting a password/SCRAM challenge is rejected.
Probe credentials never execute client queries. Do not expose this listener to
untrusted users, and configure PostgreSQL `pg_hba.conf` for the proxy source IP.

With `pool_mode = "none"`, routed clients use PostgreSQL's **simple Query
protocol**. Shared `session` and `transaction` pools additionally support
Parse/Bind/Describe/Execute/Close/Flush/Sync and named/unnamed prepared statements.
Extended work always uses the primary and is never replayed. Preparatory and
Execute responses may reach the client before its Sync; the implicit transaction
commits only at the real native boundary. Each exchange stays privately buffered
through its native terminal (32 MiB maximum), using the shared result-storage
budget. Simple Query still waits for a validated ReadyForQuery before exposure.
COPY remains unsupported. Opaque passthrough uses `route_selects = false` and
`pool_mode = "none"`.

Transaction pools retain up to 128 statement names and 128 portals per frontend,
with a 1-MiB combined retained SQL/type/name byte allowance. Limits fail closed
with FATAL 54000; names are never silently evicted. Resolved parameter types and
exact original SQL survive backend reassignment. Restore checks the original
result names/types; a changed result shape closes with FATAL 0A000 so a driver
cannot decode binary data using stale metadata. Reconnect and prepare again after
such a schema change. Portals retain their exclusive
backend until actual transaction completion. SQL namespace/session operations
materialize protocol definitions and retain native session affinity, preserving
SQL PREPARE/EXECUTE/DEALLOCATE/DISCARD and `from_sql` semantics. Session pools use
their permanently assigned backend's native prepared namespace.

An active Execute cancellation drains its native response through the client's
Sync before reuse; a private extra Sync fences accepted native cancellation.
Cancellation between exchanges closes/discards conservatively. Partial-delivery
cancellation or delivery timeout closes without replay or inserting error bytes
into a partially delivered frame. EOF before Sync rolls back unfinished work.
The frontend queue is bounded at 256 frames and 16 MiB of active/queued bodies,
plus one incoming frame; overflow closes explicitly.
Routed clients receive virtual cancellation credentials. Cancellation stops read
retries and drains the active backend before the next query; transaction/savepoint
state is preserved. Cancellation during partial response delivery closes the
connection. Opaque passthrough cancellation remains unsupported.

On a persistent client connection:

- Eligible autocommit SELECTs choose healthy recovery-mode replicas in round-robin
  order, using a process-wide counter. They fall back to the primary if no eligible
  replicas remain. During a primary gap, existing idle clients can keep reading
  replicas. Initial client startup still requires an available primary.
- Writes and all explicit transaction commands go to the current primary. `BEGIN`
  pins subsequent queries, including SELECTs, to that primary until PostgreSQL
  reports idle after COMMIT/ROLLBACK. Leadership changes interrupt active
  transactions; they are never reconstructed or replayed.
- Idle stateless sessions can reconnect to the new primary without requiring a new
  client socket. A write not yet dispatched waits up to `wait_timeout_ms`. Once
  bytes are dispatched, a failed write has an uncertain outcome and is never retried.
- SET/RESET, temporary objects, prepared statements, LISTEN, unknown functions and
  other unclassified SQL permanently pin the session to the primary. A subsequent
  leadership change ends that session, preserving the boundary around session state.
- Backend connections are cached per client and node, not shared across clients.
  A topology generation change invalidates cached backends. Replica withdrawal
  interrupts an in-flight buffered read so it can try another eligible backend.

### What counts as a pure SELECT?

The classifier deliberately accepts a small lexical subset. It rejects routing for
comments, quoted identifiers, dollar quotes, multiple statements, SELECT INTO,
locking SELECTs, explicit OPERATOR expressions and unknown functions.
Allowlisted functions include basic aggregates/string/numeric functions,
`current_setting`, `pg_is_in_recovery`, and `inet_server_addr`/`inet_server_port`.
Schema-qualified functions outside `pg_catalog` are not accepted. Everything
outside that subset stays on the primary and pins the session conservatively.
Replica connections also set `default_transaction_read_only = on`.

Read-only `WITH` queries are eligible too, including multiple/nested CTEs,
`WITH RECURSIVE`, column aliases, and `AS [NOT] MATERIALIZED`. A PostgreSQL-dialect
syntax-tree visitor validates every CTE, subquery, and set-operation branch;
parser failures or unsupported structures stay on the primary. Data-modifying
CTEs (`INSERT`, `UPDATE`, `DELETE`, `MERGE`), outer writes, SELECT INTO, locking
clauses, and unknown functions stay on the primary even if the final statement
is a SELECT or the modifying CTE is unreferenced. The existing conservative
restrictions on comments and quoted identifiers still apply. Explicit transactions
and session-state pinning take precedence over read-only CTE classification.

**SQL text cannot prove semantic purity.** Views, RLS policies, custom operators,
casts and shadowed allowlisted functions can execute user code. Routing/retries
therefore require trusted schemas and functions without external side effects.
PostgreSQL read-only mode prevents many database mutations, but cannot undo
external effects from user code. Do not treat the lexical classifier as a security
boundary or an exactly-once guarantee. Replicas may also return stale data;
read-your-writes consistency is not guaranteed outside explicit primary transactions.

Responses are buffered until ReadyForQuery before exposing result bytes. Eligible
reads can retry a transport failure or withdrawn backend only before any result is
forwarded. A failed target is excluded for the rest of that query. Each
routing/setup phase uses `wait_timeout_ms`; healthy read execution uses
`query_timeout_ms` (default 5 minutes per attempt). `read_retry_timeout_ms`
(default 10 minutes) covers routing, setup and all read attempts, capped by
`read_max_attempts` (default 3, including failed setup). Ordinary SQL errors are
returned unchanged. Healthy reads can finish when an unrelated primary changes
or their replica becomes the unique primary.
A response is limited to 32 MiB and an individual protocol message to 16 MiB.
Oversized results close the connection. Writes have no automatic execution timeout;
set PostgreSQL-side statement timeouts as needed. `result_buffer_bytes` defaults to
64 MiB of reusable bulk storage, including cached slabs. Small results use fixed
inline storage. Capacity exhaustion returns FATAL 53200 before result exposure and
does not trigger retries. `delivery_timeout_ms` defaults to 30 seconds;
`cancel_cleanup_timeout_ms` defaults to 5 seconds. These limits do not bound total
process RSS. See [memory ownership](docs/design/result-memory.md) and
[metrics](docs/metrics.md) for accounting and overhead.

## Discovery and failure boundaries

Discovery probes all nodes concurrently for `pg_is_in_recovery()` and
`transaction_read_only`. Exactly one reachable writable non-recovery server enables
primary routing; recovery-mode servers are replica candidates. No primary causes
writes to wait. Multiple visible writable servers disable all query routing.
Probe errors mark nodes unavailable. Replica health changes update routing, while
primary identity/ambiguity changes increment the leadership generation.

The polling period plus probe timeout approximates detection latency. SQL checks
are not a leadership lease: an unreachable old primary may still be writable
elsewhere, and role changes can happen between checks and execution. External
fencing and replication durability remain necessary. Disconnects during COMMIT can
leave unknown outcomes; guaranteed business retries require application idempotency.

`max_clients` bounds accepted session tasks, including waiting clients; excess
SQL startup is rejected; bounded reserve startup slots keep cancellation available. `startup_timeout_ms` bounds startup packets (64 KiB
maximum). `wait_timeout_ms` bounds routing and backend setup. TLS, Unix
sockets, hot reload and full transaction-pool acceptance
are not implemented. All traffic is unencrypted.

## Original session mode

Set `route_selects = false` (or omit it) to retain transparent authentication and
full backend protocol passthrough. One backend serves the entire client session.
New connections wait for a primary; a leadership generation change closes existing
sessions. Replica-only health changes do not close sessions. There is no query
routing or replay in this mode. SSL/GSS negotiation receives `N` in both modes.

## Runtime-agnostic conformance

The [black-box conformance suite](conformance/README.md) runs common assertions
against existing `POOLER_DSN` and `POSTGRES_DSN` endpoints. Capabilities control
explicit skips in the incomplete baseline, and every skip now makes the run
exit nonzero. Missing required features/fixtures remain open work. Optional
adapters own local/Docker setup and fault injection. JSON and JUnit aggregate
setup, body and cleanup diagnostics per invocation; filtered developer runs
cannot report release acceptance.

## Verification

```sh
cargo test --locked
cargo build --locked
python3 tests/failover.py
```

See [the integration tests](tests/README.md). They use Docker, Python's
standard library and real PostgreSQL 16 streaming replication. The suite covers
same-socket replica reads through a primary gap and promotion, a queued same-socket
write, concurrent queued writers and ambiguity refusal, interrupted transaction
safety, replica withdrawal/retry with primary fallback, read-only CTE routing
through failover, and modifying-CTE primary routing. Temporary resources are
isolated and cleaned up. Set `RUSTYBOUNCER_BIN` to use another binary.

Protocol references: [PostgreSQL message flow](https://www.postgresql.org/docs/current/protocol-flow.html),
[message formats](https://www.postgresql.org/docs/current/protocol-message-formats.html),
[PgBouncer pooling modes](https://www.pgbouncer.org/features.html).

## Distroless container

Build a Docker-loadable image with `nix build .#docker --out-link result-image`,
then `docker load -i result-image` (or `podman load -i result-image`). The image
runs as UID/GID 10001 with no shell or package manager. See the
[container guide](deploy/README.md) for read-only deployment, configuration and
rootless runtime verification.

## Real Kubernetes integration

The [kind + CloudNativePG suite](tests/kubernetes/README.md) provisions four kind
nodes and three PostgreSQL instances, then tests the distroless proxy through real
replica replacement and primary-node failovers. After building/loading the image:

```sh
nix develop --command python3 -m tests.kind_cnpg --output /tmp/rustybouncer-kind-results
```

All six cases run without skips and emit JSON/JUnit reports and diagnostics.
