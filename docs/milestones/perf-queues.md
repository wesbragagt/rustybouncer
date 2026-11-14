# PERF-008 — bounded queues and failure fencing (INCOMPLETE feature)

Authority: performance tasks, PERF-008,
L01–L10/P04–P06/P08 and the local feature checklist.
This is the second verified slice after `8fa5330`. PERF-008 remains incomplete:
extended/prepared protocol and the complete small-pool comparisons are required.
No canonical box is closed and no throughput improvement is claimed.

## Implemented and observed

Backend reservations own exactly one connecting/leased/resetting/idle phase.
FIFO wait tickets, phase charges and wait durations are refunded on completion,
cancellation, timeout, EOF and task drop, independently of manager lifetime.
Fixed-cardinality metrics expose configured base/reserve/global limits, backend
reservations/phases and queued work. The strict scrape inventory is 184 +29N
series for N configured nodes; SQL, credentials and startup values remain absent.
Valid excess frontend startup receives PostgreSQL FATAL 53300 within a bounded
reply deadline. The frontend reader starts before backend/bootstrap acquisition,
so EOF removes a queued startup even when pipelined Query bytes remain buffered.

All 13 dedicated cases pass. At 10, 100 and **1,000 actual frontends**, three
saturation cycles each prove exactly two exclusive backends, no early dispatch,
correct unique rows, canceled/disconnected write absence and complete ticket
refunds. Separate tests cover maximum clients at all three sizes, FIFO transaction
handoff, a 500ms wait deadline, reserve activation after 500ms, independent
user/database pools and 20 bootstrap-EOF cycles with immediate ticket removal.

Real PostgreSQL pause/promotion tests retain the same queued startup socket,
recover on the new primary without client reconnect, and never replay old writes.
An obsolete blocked reset is discarded promptly on topology change instead of
holding global capacity until its five-second reset timeout. Unassigned stale
leases are dropped before dispatch; active T/E and session ownership remain fenced.
Routing and startup retries share absolute deadlines rather than multiplying them.

## Complete verification and evidence

Linux x86_64, locked Nix, owned real PostgreSQL 16 Docker fixtures; exact tool and
image versions are in [environment](perf-queues-evidence/queues-environment.json).
Frozen release SHA256:
`242a078baeaf14d3246e0cd27cbf67b03c903ef1285aff7c479d6651c5037c79`.
[Provenance](perf-queues-evidence/provenance.json) retains source hashes.

The complete native gate passes **58 Rust +72 Python tests**, strict Clippy,
Ruff, Astral ty, formatting, Promtool/Grafana and complexity **30/30**.
The frozen release passes **13 queue +16 transaction +16 session +15 resource
+7 failover +8 long-query +10 metrics +4 buffering =89 live cases**.
There are zero failures, errors, skips, expected failures, unexpected successes or
ignored tests in either accepted run. Every suite runs its entire inventory.
The installed locked commit hook runs normally for this checkpoint.

```sh
nix develop --no-update-lock-file --command bash scripts/verify
nix develop --no-update-lock-file --command cargo build --release --locked --offline
export RUSTYBOUNCER_BIN=/home/publicuser/dev/rustybouncer-evidence/perf-pools-20261109/queues-fencing-frozen/production
# Each evidence directory must be fresh; do not overwrite retained acceptance.
QUEUE_POOL_EVIDENCE=NEW_QUEUE_DIRECTORY nix develop --command python3 tests/queue_pool.py
TRANSACTION_POOL_EVIDENCE=NEW_TRANSACTION_DIRECTORY nix develop --command python3 tests/transaction_pool.py
SESSION_POOL_EVIDENCE=NEW_SESSION_DIRECTORY nix develop --command python3 tests/session_pool.py
RESOURCE_EVIDENCE=NEW_RESOURCE_DIRECTORY nix develop --command python3 tests/resources.py
nix develop --command python3 tests/failover.py
nix develop --command python3 tests/long_queries.py
METRICS_EVIDENCE=NEW_METRICS_DIRECTORY nix develop --command python3 tests/metrics.py
BUFFER_EVIDENCE=NEW_BUFFER_DIRECTORY nix develop --command python3 tests/buffering.py
```

[Native log](perf-queues-evidence/queues-fencing-full-native.log),
[all live results](perf-queues-evidence/queues-accepted-live.log),
[archive hashes](perf-queues-evidence/archives.json), and the compressed per-file
manifest retain raw configs, scrapes, direct database observations and cleanup.
Development failures are preserved separately: three admission cases originally
received EOF, and a native startup fixture lacked the now-required cancellation
registry. Production replies and the realistic fixture were corrected before the
complete accepted runs; assertions and concurrency were not weakened.

Outstanding: W02–W12 prepared/extended/namespace/pipeline/binary/null behavior,
positive protocol cancellation and 10/100/1,000 prepared concurrency; all declared
small-pool comparisons, PERF-005 scheduling analysis and PERF-009 publication.
Future benchmark harness edits are deliberately outside this checkpoint.
