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

Latest candidate: runtime `14f077e`, measured on 2026-11-09 with PgBouncer **1.26.1**
and PostgreSQL 16 on a shared Linux host. All **654 normal timing arms** across
the before/after lookup, bounded-pool, normalized and additional-workload matrices
report zero query errors. The [complete comparison](docs/benchmarks/performance-final.md)
retains all repetitions, ranges, raw profiles and exact reproduction commands;
the [performance task ledger](docs/tasks/pgbouncer-performance.md) records scoped
acceptance. Broader conformance and release requirements remain open.

### Persistent indexed lookup

The unchanged original comparison uses 10,000 rows and a 128-byte payload, three
30s measurements after 5s warmups, rotated order, trust authentication, TLS/metrics
off and random 10% latency logs. Quantiles are medians of the three per-run
quantiles. These original Rust targets use **unshared** routed/passthrough modes;
they do not measure the new shared pools.

| Target | Clients | Median Q/s | p50 ms | p95 ms | p99 ms | Peak PG backends |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Direct PostgreSQL | 10 | 163,990 | 0.050 | 0.114 | 0.203 | 10 |
| PgBouncer session | 10 | 134,587 | 0.067 | 0.113 | 0.188 | 10 |
| PgBouncer transaction | 10 | 124,426 | 0.070 | 0.128 | 0.224 | 10 |
| Rust passthrough | 10 | 115,749 | 0.075 | 0.152 | 0.273 | 10 |
| Rust unshared routed | 10 | 104,552 | 0.087 | 0.150 | 0.275 | 10 |
| Direct PostgreSQL | 100 | 334,233 | 0.203 | 0.804 | 1.723 | 100 |
| PgBouncer session | 100 | 155,008 | 0.621 | 0.788 | 1.107 | 100 |
| PgBouncer transaction | 100 | 148,795 | 0.620 | 0.979 | 1.364 | 100 |
| Rust passthrough | 100 | 182,268 | 0.519 | 0.963 | 1.519 | 100 |
| Rust unshared routed | 100 | 171,531 | 0.523 | 1.103 | 1.607 | 100 |

At 100 clients, routed Rust has higher throughput and worse p95/p99 than PgBouncer
session. Passthrough has different semantics and cannot stand in for routed mode.
These unrestricted results do not establish per-core efficiency: Tokio can use
multiple CPUs, while one PgBouncer instance uses one. PgBouncer also supports
multiple instances/SO_REUSEPORT, which this study does not measure.
[Upstream CPU configuration](https://www.pgbouncer.org/config.html#so_reuseport).
All targets reach the original 10/100 backend counts; pooling savings belong to
the separate small-pool experiment below.

With one proxy CPU at 100 clients, PgBouncer session measures **101,322 Q/s**
versus **60,953 Q/s** for unshared routed Rust; p99 is **4.917ms versus 6.874ms**.
Proxy CPU per query is **3.00µs versus 6.16µs**. That CPU is shared with other host
work, so these are measured process costs and rates, not exclusive-core capacity
or a production efficiency guarantee. All repetitions/ranges/CV remain in the report.

### Shared pools with a 16-backend ceiling

All **144 arms per candidate** provision 10/100/**1,000** actual frontends. Every
client completes; independent PostgreSQL observations stay <=16 workload backends.
Three repetitions use 5s warmup +10s measurement with complete connection/drain
time and 100% transaction logging. Each transaction includes BEGIN, a guarded
indexed lookup and COMMIT. These TPS values are distinct from single-query Q/s.
“One CPU” constrains only the proxy and sets one Tokio worker; PostgreSQL and the
load generator keep their recorded unrestricted placement.

| Workload | Proxy placement | Clients | PgBouncer session TPS | Rust session TPS | PgBouncer transaction TPS | Rust transaction TPS |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| Connection churn | Unrestricted | 1,000 | 12,841 | 12,671 | 13,443 | 12,135 |
| Persistent transactions | Unrestricted | 1,000 | 42,665 | 34,165 | 39,034 | 21,624 |
| Connection churn | One CPU | 1,000 | 12,840 | 5,545 | 13,277 | 7,213 |
| Persistent transactions | One CPU | 1,000 | 32,923 | 21,512 | 26,914 | 9,677 |

Compared with the pre-scheduler candidate, unrestricted 1,000-client Rust churn
medians rose from 4.0k→12.7k TPS (session) and 3.5k→12.1k (transaction); p99 fell
from 296→104ms and 367→118ms. Proxy CPU per transaction fell, while observed RSS
rose about 4 MiB. At 1,000 persistent transaction clients, Rust transaction TPS
rose from 4.1k→21.6k; PgBouncer remains faster. Low-concurrency transaction costs
and one-CPU gaps remain.

Session pooling can leave clients waiting about ten seconds while active sessions
hold the 16 backends, even when aggregate p99 is low. Maximum latency and minimum
completions per client expose that wait. Every repetition stays included, including
slower native results and substantial control shifts: for example, PgBouncer's
one-CPU persistent transaction median fell from 41.8k to 26.9k between batches.
No isolated causal estimate, statistical significance or production SLO is claimed.

### CTE, larger results and mixed durations

All **180 additional workload arms** complete with exact per-client workload
preflight, full transaction logs and every client represented. At 100 clients:

| Workload | Proxy placement | PgBouncer session Q/s | Rust unshared routed Q/s | PgBouncer p99 ms | Rust p99 ms |
| --- | --- | ---: | ---: | ---: | ---: |
| CTE lookup | Unrestricted | 124,050 | 98,644 | 1.931 | 2.580 |
| 2,048-row result | Unrestricted | 3,223 | 9,704 | 42.620 | 34.315 |
| CTE lookup | One CPU | 131,269 | 51,643 | 1.859 | 3.112 |
| 2,048-row result | One CPU | 3,154 | 5,397 | 39.352 | 27.993 |

The guarded CTE/short lookup returns four columns; the original lookup returns
only its payload. Routed Rust is faster for this result size in this fixture,
with higher proxy memory: roughly **32 MiB versus 13 MiB RSS** on one CPU. Its
buffered/coalesced response policy is part of the measured mode; this does not
isolate that policy's marginal cost or benefit. Rust's
32-MiB response limit and buffering contract still apply; this does not establish
unlimited streaming capacity or a general large-query advantage.

A 9:1 mix of short lookups and 20ms queries produces similar unrestricted rates
(~48k Q/s), but short-query p99 is 0.179ms for routed Rust versus 0.132ms for
PgBouncer session. On one CPU, those short tails are 1.088ms versus 0.274ms.
Aggregate throughput alone hides that difference. The separate 96-arm shared-pool
before/after duration mix also retains short/long distributions and maximum waits.

### Recovery and verification

All **24 crash/promotion load cases** complete across both poolers' session and
transaction modes, 10/100 workers and three repetitions. Independent database rows
and statement logs verify **741,044 acknowledged writes** survived, with zero
duplicate INSERT executions or application SQL replays. **53 uncertain committed
writes** remain explicit. Expected outage errors are retained; every worker
acknowledges new writes after promotion and continues through the stable phase.
PgBouncer uses an external host/reload/reconnect adapter and Rust uses discovery;
these results do not rank every HA deployment or establish replica freshness.

The candidate also passes **144 live regression cases**, **six kind/CNPG cases**,
**eight independent unsuppressed Memcheck arms**, and **66 Rust +73 Python tests**,
with zero failures, errors or skips in those required functional scopes. Memcheck
has no access/definite/indirect/possible leak or private-response findings; retained
process globals and total RSS are separate. Instrumented profiles and memory
audits do not substitute for normal timings. See
[acceptance and limits](docs/milestones/perf-final.md) for complete evidence,
including the corrected and preserved failed fault-log capture.

### Advantages and trade-offs

| Area | PgBouncer | Rustybouncer today |
| --- | --- | --- |
| Backend capacity | Shared pools and mature limits; lower costs in the measured transaction/CTE workloads | Opt-in shared session/transaction pools; 1,000 frontends with a 16-backend ceiling verified. Default routing keeps per-client caches |
| Read routing | Host/DNS selection chooses connection destinations; no SQL purity classifier | Eligible simple-protocol autocommit SELECT/CTE reads use replicas in unshared/transaction mode, with primary fallback; extended queries and shared session mode stay primary |
| Primary changes | Host/DNS and reload/reconnect controls support external HA orchestration | Probes PostgreSQL roles, waits for a unique primary, and retries eligible buffered reads before exposing results |
| Protocol and security | Authentication/TLS and broad protocol support; transaction pooling has session-state restrictions | Routed/shared mode requires trust; shared pools support extended queries, while unshared routing supports simple Query only. No COPY or TLS. Passthrough preserves backend protocol/authentication |
| Response and memory | Streams protocol traffic with low connection overhead | Simple Query buffers to ReadyForQuery; extended exchanges buffer to native terminals before client Sync. Limits: 32 MiB/exchange, 16 MiB/message, 64-MiB default process bulk-storage budget; frontend/runtime memory is separate |
| Operations | Administrative console, reload and pool controls | Prometheus/Grafana and container/CNPG examples; reload, administrative pool controls and graceful draining remain open |

PgBouncer fits applications prioritizing established protocol/security/admin
features and efficient shared pooling. Transaction pooling requires compatible
session behavior; protocol prepared statements need appropriate configuration.
[Upstream feature map](https://www.pgbouncer.org/features.html).

Rustybouncer is experimental, with role-aware routing and buffered read continuity.
Both need external promotion, replication and fencing. Interrupted transactions
and uncertain writes require application handling and are never transparently
replayed by Rustybouncer. Modern PgBouncer host lists and DNS selection are
connection-level mechanisms, distinct from SQL-aware routing.
[Host configuration](https://www.pgbouncer.org/config.html#host),
[failover integration](https://www.pgbouncer.org/faq.html#how-to-failover).

The immutable [historical comparison](docs/benchmarks/pgbouncer-comparison.md),
reference `c8997e8`, retains its original script, all 30 runs and provenance. These
new observations do not supersede [FEATURE_COMPLETENESS.md](FEATURE_COMPLETENESS.md)
or establish its broader conformance/release checklist.

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

**Scoped PERF-008 acceptance is complete** as `0604ee8`: all 144 live cases and
all 144 bounded-pool comparison arms passed, including actual 1,000-client scopes.
[Evidence and trade-offs](docs/milestones/perf-pools.md) show backend capacity
savings and the current high-concurrency CPU/tail gap. Scheduling improvement and
the final combined comparison remain open; broader canonical conformance is
tracked separately.
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
