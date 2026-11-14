# PERF-008 — prepared/extended ownership (INCOMPLETE feature)

Authority: performance tasks, PERF-008,
W02–W12/P04–P06/P08/T01–T10/S01–S03/S07–S08 in
the local feature checklist.
This is the third slice after leases `8fa5330` and queues `058c151`.
The full feature remains incomplete until the declared small-pool comparisons and
integration pass. No canonical checkbox or performance gain is claimed here.

## Behavior

Shared session/transaction modes support Parse, Bind, Describe, Execute, Close,
Flush and Sync on the primary, with named/unnamed statements, binary/NULL values,
portal suspension, ordered pipelines and native error recovery. Unshared routing
keeps its explicit simple-query contract. COPY remains an explicit 0A000 rejection.

Each native exchange uses the existing exclusive slab response storage through
its expected terminal, capped at 16 MiB/message and 32 MiB/exchange. Private Flush
permits drivers to wait before Sync. These complete preparatory/Execute replies
can precede commit; only the frontend's real Sync closes the implicit transaction.
Simple Query still waits for actual ReadyForQuery and is never streamed early.
No extended operation, write or transaction is replayed. A mixed Simple Query is
forwarded on the same exclusive backend without an injected early Sync; a later
query error still rolls back preceding uncommitted writes.

Transaction clients retain at most 128 logical statement names and 128 portals,
with a 1-MiB combined retained SQL/type/result-description/name allowance. Original
Parse bytes move into owned storage; inferred parameter OIDs are resolved once.
Restoration uses a vectored Parse prefix and resolved OIDs without copying SQL.
Private Describe verifies original result names/types before Bind, preventing a
binary decoder from silently receiving a new shape after DDL. A changed shape
fails closed with FATAL 0A000 and requires reconnect/reprepare. Native pinned plans
retain PostgreSQL's own invalidation errors. Logical limits use FATAL 54000;
physical reset removes plans and all logical definitions remain restorable.
Portals retain charged definition references after statement close or unnamed
replacement until their own close/transaction end.

Unknown session effects retain native affinity before preparation/planning;
PostgreSQL can evaluate immutable functions at Bind before Execute. Namespace or
session operations materialize logical definitions before using the native
namespace. SQL PREPARE, protocol Parse, from_sql, duplicate names, EXECUTE,
DEALLOCATE and DISCARD do not resurrect stale virtual definitions. This remains
conservative affinity analysis, not broader replica/retry eligibility or proof
of arbitrary hidden function/view/trigger effects.

The operation epoch spans the entire extended cycle. A cancellation of active
Execute drains its native reply, then the client's real Sync; an extra private
Sync fences accepted native cancellation without destroying unnamed statements.
No private Sync precedes the client boundary. Queued Parse cancellation refunds
its ticket without SQL dispatch. Cancellation between exchanges conservatively
closes/discards. EOF before Sync rolls back. Delivery cancellation/timeout closes
without inserting an ErrorResponse into a partially sent frame or handing off an
unproven lease. Protocol errors are 08P01; transport/topology failures use 08006
when an error reply can be delivered, or close the connection. New work recovers
on the real promoted primary; unfinished writes are never replayed.

Frontend queue capacity is now 256 complete frames, retaining the existing
16-MiB active/queued body budget plus one incoming frame. The overflow unit case
sends 257 frames and accounts for every received query. Separate fixed-label
extended frame/cycle/exchange metrics preserve simple-Q accounting. The strict
inventory is 275 +29N samples for N nodes, with no SQL/names/parameter labels.

## Verification and evidence

Final frozen release SHA256:
`208e98670db9a76d3ce4ca2528cedda4b8e4f1a27e1b162fcfe747ae7efef9b3`.
The source snapshot includes all Rust modules, test fixtures, Cargo.lock and sample
configuration. Linux x86_64, locked Nix and owned PostgreSQL 16 Docker fixtures.
The native gate passes **64 Rust +72 Python tests**, strict Clippy, Ruff, Astral ty,
formatting, Promtool/Grafana and complexity **27/30**.

The accepted complete live scope passes **55 protocol +13 queue +16 transaction
+16 session +15 resource +7 failover +8 long-query +10 metrics +4 buffering
=144 cases**, with zero failures, errors, skips, expected failures, unexpected
successes or ignored tests. The normal installed locked commit hook also runs
for this checkpoint.

[All suite results](perf-protocol-evidence/suite-results.json),
[complete live log](perf-protocol-evidence/protocol-ownership-accepted-live.log),
[native verification](perf-protocol-evidence/protocol-ownership-native.log),
[source/binary hashes](perf-protocol-evidence/provenance.json),
[archive hashes](perf-protocol-evidence/archives.json) and the per-file manifest
retain source snapshots, configurations, raw observations, scrapes and cleanup.
Per-mode summaries preserve all six 10/100/1,000-client prepared results and
native database/type/promotion oracles. Development failures are distinct files;
none replaces the accepted complete run.

The 55-case protocol scope exercises both modes at 10/100/**1,000 actual
frontends**, three prepared executions per client, exact unique values, binary
NULL/bytea, forced reassignment, 17 retained statements, 128-name/portal and byte
limits, native schema/type oracles, libpq pipelines, errors, namespace behavior,
cancellation during execution/delivery/queueing, slow delivery, disconnect rollback
and real pause/promotion. Direct PostgreSQL observations verify commit boundaries,
planning effects and schema-invalidation behavior. No capability switches, skips,
reduced concurrency or hidden client retries are used.

```sh
nix develop --no-update-lock-file --command bash scripts/verify
nix develop --no-update-lock-file --command cargo build --release --locked --offline
export RUSTYBOUNCER_BIN=/home/publicuser/dev/rustybouncer-evidence/perf-pools-20261109/protocol-ownership-frozen/production
# Use fresh output directories for each complete invocation.
PROTOCOL_POOL_EVIDENCE=NEW_PROTOCOL_DIRECTORY nix develop --command python3 tests/protocol_pool.py
QUEUE_POOL_EVIDENCE=NEW_QUEUE_DIRECTORY nix develop --command python3 tests/queue_pool.py
TRANSACTION_POOL_EVIDENCE=NEW_TRANSACTION_DIRECTORY nix develop --command python3 tests/transaction_pool.py
SESSION_POOL_EVIDENCE=NEW_SESSION_DIRECTORY nix develop --command python3 tests/session_pool.py
RESOURCE_EVIDENCE=NEW_RESOURCE_DIRECTORY nix develop --command python3 tests/resources.py
nix develop --command python3 tests/failover.py
nix develop --command python3 tests/long_queries.py
METRICS_EVIDENCE=NEW_METRICS_DIRECTORY nix develop --command python3 tests/metrics.py
BUFFER_EVIDENCE=NEW_BUFFER_DIRECTORY nix develop --command python3 tests/buffering.py
```

Development findings and failed runs are retained separately. They include output
folder collisions between modes, a planner-time division error at Bind rather than
Execute, and a native ParameterStatus produced by the planning-effect oracle.
The harness now isolates both modes and asserts PostgreSQL's actual responses.
Earlier 136-/140-case candidate runs are diagnostics, not the final ownership
candidate's acceptance. Source changes prompted a new full run; none are counted
as replacements for final scope.

Outstanding: all declared bounded-pool comparison arms at 10/100/1,000 clients,
PERF-005 workload/scheduling evidence, PERF-009 final comparison/profiles,
failover-under-load evidence, README publication and workmux integration.
Future benchmark harness files are outside this slice until actually validated.
