# PERF-008 — transaction-lease slice (INCOMPLETE feature)

Authority: [performance tasks](../tasks/pgbouncer-performance.md), PERF-008,
P04–P06/P08/P10 and T01–T10 in [FEATURE_COMPLETENESS.md](../../FEATURE_COMPLETENESS.md).
This commits the first simple-query transaction-leasing slice. PERF-008 remains
**incomplete** until capacity/queue, protocol/prepared, full cancellation and
10/100/1,000-client acceptance and comparisons pass. No canonical box is closed.

## Implemented slice

`pool_mode = "transaction"` now accepts routed simple-query work. Hold an exclusive
backend while actual PostgreSQL Ready status is T/E; COMMIT/ROLLBACK release only
when actual status is I and no tracked session affinity remains. Savepoints,
errors and COMMIT/ROLLBACK AND CHAIN preserve ownership. Cancellation finishes
native delivery/drain/barrier and its operation epoch before reset/handoff.
There is no in-flight write/transaction replay.

On idle release, the owning frontend task awaits verified rollback/DISCARD/reset
and then returns the backend. Its previously delivered user result remains known
if reset fails; the backend is discarded. A dropped/failed operation cannot return
unproven ownership. New clients continue using original user/database/startup keys.
Session mode retains its exclusive backend; default unshared behavior is unchanged.

Separate affinity analysis recognizes primary-only observations (including
pg_backend_pid/current_user/current_database), parameterized SELECT expressions,
known stateless functions and transaction-local SET. These can release an idle
lease **without changing the replica/retry classifier**. Unknown session effects,
SQL PREPARE, LISTEN, temporary objects and session SET retain session affinity.
Plain INSERT/UPDATE/DELETE release after actual idle; explicit unknown functions
in those statements conservatively retain affinity. Existing SQL purity limitations
for hidden view/trigger/function effects still apply; this is not a proof of
arbitrary user SQL semantics. It is not an advertised production compatibility claim.

## Verification

Frozen release SHA256
`b08cc320fb117977ac46fef7e07e023120cd2cb000460137f1e0f32238710fc7`.
[Exact source hashes](perf-transaction-leases-evidence/provenance.json) bind the
candidate before the final complete run. **56 Rust +72 Python tests**, strict
Clippy, Ruff, Astral ty, formatting, Promtool/Grafana and complexity **30/30** pass.
The normal installed locked hook verifies the staged slice for this commit.

The release passes **16 transaction +16 session +15 resource +7 failover
+8 long-query +10 metrics +4 buffering =76 live cases**, zero failures/errors/skips.
Each file's entire inventory runs; there is no capability switch or reduced
concurrency. Existing suites retain 10/100-client cycles, all failover cases and
real physical promotion in session regression coverage.

The new transaction cases independently prove actual PID pinning, committed versus
uncommitted visibility, rollback, simultaneous ownership/local-state isolation,
savepoint recovery and failed-transaction retention. Other connected clients hold
all declared capacity after the original client's COMMIT/ROLLBACK, proving release
without requiring arbitrary PID rotation. Open/aborted disconnects roll back;
repeatable-read snapshots match direct PostgreSQL; READ COMMITTED/READ UNCOMMITTED
hide dirty writes; a real SERIALIZABLE conflict returns 40001 without replay.
Positive cancellation preserves a savepoint and prior write. Read-only transaction
errors and multi-statement/AND CHAIN boundaries preserve actual I/T/E semantics.

Development found an unnecessary pin for current_user/current_database observations:
the AST represents current_user as a function. Both the failed 16-case development
run and the subsequent unit failure are retained. Recognition and unit coverage
were corrected before the final all-76 run. Those failures are diagnostics,
never counted as passing evidence.

## Reproduction/evidence

Linux x86_64, locked Nix and owned real PostgreSQL 16 Docker fixtures. Output
folders must be fresh; original frozen evidence must not be overwritten.

```sh
nix develop --no-update-lock-file --command bash scripts/verify
nix develop --no-update-lock-file --command cargo build --release --locked --offline
export RUSTYBOUNCER_BIN=/home/publicuser/dev/rustybouncer-evidence/perf-pools-20261109/transaction-slice1-frozen/production
export TRANSACTION_POOL_EVIDENCE=NEW_TRANSACTION_DIRECTORY
nix develop --no-update-lock-file --command python3 tests/transaction_pool.py
export SESSION_POOL_EVIDENCE=NEW_SESSION_DIRECTORY
nix develop --no-update-lock-file --command python3 tests/session_pool.py
export RESOURCE_EVIDENCE=NEW_RESOURCE_DIRECTORY
nix develop --no-update-lock-file --command python3 tests/resources.py
nix develop --no-update-lock-file --command python3 tests/failover.py
nix develop --no-update-lock-file --command python3 tests/long_queries.py
export METRICS_EVIDENCE=NEW_METRICS_DIRECTORY
nix develop --no-update-lock-file --command python3 tests/metrics.py
export BUFFER_EVIDENCE=NEW_BUFFER_DIRECTORY
nix develop --no-update-lock-file --command python3 tests/buffering.py
```

[Native log](perf-transaction-leases-evidence/transaction-third-native.log),
[complete live log](perf-transaction-leases-evidence/transaction-slice1-final.log),
[raw archive hashes](perf-transaction-leases-evidence/archives.json) and
[per-file SHA256 manifest](perf-transaction-leases-evidence/manifest.json.gz)
retain results, configs, scrapes, exact oracles and cleanup evidence.
The shared session test fixture was extracted without changing its 16-case
inventory and that complete suite was rerun against the frozen release.

Outstanding: bootstrap EOF queue removal; explicit pool phase/queue metrics;
reserve/isolation/saturation positive coverage at 10/100/1,000 clients; complete
named/unnamed protocol/prepared/pipeline/binary/null/namespace/cancel behavior;
final bounded-pool benchmark and PERF-005/009 comparisons/publication. New benchmark
harness work remains unstaged and is not delivered by this slice. No timing gain
or final PERF-008 conformance is claimed.
