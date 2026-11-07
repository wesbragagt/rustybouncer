# rustybouncer

Experimental Rust PostgreSQL router with automatic primary discovery, failover
waiting, and opt-in round-robin routing of conservative SELECTs to replicas.

See [design.md](design.md) for the architecture, routing decisions, failover
boundaries, and implementation tradeoffs.

## Run

```sh
cargo build --locked
cargo run --locked -- rustybouncer.toml
psql 'host=127.0.0.1 port=6432 user=postgres dbname=postgres sslmode=disable'
```

Configure your primary and replicas in `rustybouncer.toml`; the sample lists ports
55432–55434. All nodes must be members of the same streaming-replication cluster.
The router does not validate cluster identity, elect leaders, fence nodes, or
promote standbys. Run a cluster manager such as Patroni for those jobs.

The sample enables `route_selects = true`. The default when omitted is `false`,
which preserves the original session router. `RUST_LOG=rustybouncer=debug` changes
logging. Ctrl-C stops discovery and closes active sessions. Configuration changes
require a restart. Probe authentication may use a password via the environment
variable named by `probe_password_env`; never put secrets in the TOML file.

## Query-routing mode

**Requires trusted local networking and PostgreSQL trust authentication for client
backend connections.** This mode does not authenticate clients itself. It forwards
the original startup user, database and parameters to each backend and only accepts
AuthenticationOk; any backend requesting a password/SCRAM challenge is rejected.
Probe credentials never execute client queries. Do not expose this listener to
untrusted users, and configure PostgreSQL `pg_hba.conf` for the proxy source IP.

Clients must use PostgreSQL's **simple Query protocol**. `psql` works; drivers that
use extended queries/prepared statements must disable those features or use session
mode. Extended-protocol messages and COPY receive explicit unsupported errors.
Cancellation packets are dropped and backend cancellation keys are not exposed.

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
forwarded. A failed target is excluded for the rest of that query. All read attempts
share the `wait_timeout_ms` deadline; ordinary SQL errors are returned unchanged.
A response is limited to 32 MiB and an individual protocol message to 16 MiB.
Oversized results close the connection. Writes have no automatic execution timeout;
set PostgreSQL-side statement timeouts as needed. Memory use can grow with the number
of concurrent buffered results.

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
connections close immediately. `startup_timeout_ms` bounds startup packets (64 KiB
maximum). `wait_timeout_ms` bounds routing and pure-read retry waiting. TLS, Unix
sockets, metrics, hot reload, cancellation routing, and shared transaction pooling
are not implemented. All traffic is unencrypted.

## Original session mode

Set `route_selects = false` (or omit it) to retain transparent authentication and
full backend protocol passthrough. One backend serves the entire client session.
New connections wait for a primary; a leadership generation change closes existing
sessions. Replica-only health changes do not close sessions. There is no query
routing or replay in this mode. SSL/GSS negotiation receives `N` in both modes.

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
