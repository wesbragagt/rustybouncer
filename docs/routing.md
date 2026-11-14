# Routing and failover reference

## Query-routing mode

Configure `frontend_tls`, `backend_tls` and `auth_query` for encrypted,
authenticated routing and shared pools. Clients prove their PostgreSQL password
with SCRAM-SHA-256; a certificate-authenticated lookup retrieves the SCRAM verifier
using a bound username parameter. Backend connections authenticate as the original
client role, preserving database permissions. Probe/lookup roles never execute
application queries. CA and hostname verification are mandatory for backend TLS.

PEM files are reread for every new connection, including probes and auth queries,
so projected Kubernetes Secrets can rotate without restarting the router. Existing
TLS sessions retain their original negotiated credentials until closed; pool
idle/lifetime limits retire existing backend connections. This reloads certificate
files, not TOML configuration. See the [CNPG setup](../deploy/cnpg/README.md) and
[real kind security suite](../tests/kubernetes/README.md).

Without `auth_query`, routed mode retains legacy trust behavior and requires a
trusted network and trust-authenticated backends. Only configured TLS listeners
reject plaintext. Do not expose a trust listener to untrusted clients.

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
process RSS. See [memory ownership](design/result-memory.md) and
[metrics](metrics.md) for accounting and overhead.

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
maximum) and each authentication exchange. `wait_timeout_ms` bounds routing and
backend setup. Unix sockets, TOML hot reload and full transaction-pool acceptance
remain open. TLS is enabled through explicit frontend/backend configuration.

## Original session mode

Set `route_selects = false` (or omit it) to retain transparent authentication and
full backend protocol passthrough. One backend serves the entire client session.
New connections wait for a primary; a leadership generation change closes existing
sessions. Replica-only health changes do not close sessions. There is no query
routing or replay in this mode. SSL/GSS negotiation receives `N` in both modes.

