# PERF-005 — bounded head notification (INCOMPLETE performance acceptance)

Authority: [performance tasks](../tasks/pgbouncer-performance.md), PERF-005/009,
S06/S09–S12/Q08 in [FEATURE_COMPLETENESS.md](../../FEATURE_COMPLETENESS.md).
PERF-008 is accepted as `0604ee8` and workmux-integrated. This checkpoint does not
claim a throughput/tail gain or close PERF-005/009 before complete measurement.

## Evidence and decision

The complete 144-arm pre-change [pool comparison](../benchmarks/perf-pools/summary.md)
shows a large 1,000-client gap. For unrestricted persistent transactions, median
Rust transaction pooling is 4,057 TPS versus PgBouncer 38,723; both retain at most
16 PostgreSQL backends and every measured client completes. No repetition is removed.

Before changing runtime code, all **24 targeted profiling arms** completed:
100/1,000 actual clients, session/transaction modes, unrestricted/one-proxy-CPU,
and CPU/off-CPU/syscalls. Debug information and actual affinity/worker count are
verified, and all controlled query runs have zero errors. The controlled CPU
windows place pool admission/handoff at **67.3%/64.0%** of sampled user periods
for unrestricted 1,000-client session/transaction work, versus **63.0%/55.3%**
on one proxy CPU. Transaction affinity analysis contributes about 12.5–12.7%
at 100 clients. Mutex contention, notification polling, pool decisions and timer
work appear prominently. Shares depend on callchain precedence/inlining and do
not measure isolated marginal wall time; kernel execution is not attributed.

[Complete profile summary](perf-scheduling-evidence/before/summary.json),
[all 24 runs](perf-scheduling-evidence/before/profiles.json),
[environment](perf-scheduling-evidence/before/environment.json), build command,
manifest and [raw archive hash](perf-scheduling-evidence/before/archive.json)
retain 392 files including the matching profile binary. Switch intervals include
sleep and runnable delay; syscall amortization includes startup/preflight/probes.
An initial missing-debug-info assertion is retained as a development failure;
it produced no accepted profile measurement and was corrected before all 24 runs.

The [coordinator plan/decision](perf-scheduling-plan.md) precedes implementation.
This is a coordinator review, not an independent agent review.

## Implemented work bound

Replace the shared lease-return broadcast with per-ticket notifications. Signals
exist only for tickets that actually wait; immediate checkout does not allocate
one. Removing a head notifies its next group head; capacity changes notify the
globally earliest eligible head. Only group heads arm reserve-delay timers.
Every invalidated group's affected waiters still wake promptly on topology change.
No wake is repeatedly scheduled when global capacity cannot be obtained.

The original eligible-key FIFO decision, base/reserve/global limits, exact startup
identity, generation fencing, actual I/T/E pinning, reset and cancellation ownership
remain mandatory. Signal ownership has no strong manager cycle and refunds with
its ticket on completion, cancel, EOF, timeout or task drop. Affinity analysis,
SQL purity, result buffering and replay policy are unchanged.

Two additional native tests provision **1,000 manually polled waiting futures**.
Returning one reservation wakes exactly its eligible head; admission wakes at
most the current/next heads, with all remaining futures still pending. Invalidation
wakes all affected 1,000 futures immediately. Dropping every waiter returns all
signal/ticket ownership. These are structural wake/work checks, not invented TPS
or latency SLOs. Existing FIFO, reserve, independent-pool and weak-ownership tests
continue running in full.

## Verification

Frozen production SHA256:
`103fb7c79cb53baebb629446b32481952fe3124a4cf3a7fc6e86bbd94c9b3e1f`.
Linux x86_64, locked Nix, owned PostgreSQL 16 Docker fixtures. Complete native
verification passes **66 Rust +73 working-tree offline Python tests**, Ruff,
Astral ty, strict Clippy, formatting, Promtool/Grafana and complexity **27/30**.
The extra offline client-completion check belongs to the future workload harness;
the installed hook verifies exactly the staged snapshot for this checkpoint.

The accepted live scope passes **13 queue +55 protocol +16 transaction +16 session
+15 resource +7 failover +8 long-query +10 metrics +4 buffering =144 cases**,
with zero failures, errors, skips, expected failures, unexpected successes or ignored
tests. [Results](perf-scheduling-evidence/heads/results.json),
[complete live log](perf-scheduling-evidence/heads/heads-live.log),
[native gate](perf-scheduling-evidence/heads/heads-second-native.log),
[source/binary hashes](perf-scheduling-evidence/heads/provenance.json), archive hashes
and the compressed manifest retain the full frozen scope. Development Rust tests
passed before Clippy found an unused binding and a length-style warning; both were
fixed before the complete native gate. Neither threshold nor test scope changed.

```sh
nix develop --no-update-lock-file --command bash scripts/verify
nix develop --no-update-lock-file --command cargo build --release --locked --offline
export RUSTYBOUNCER_BIN=/home/publicuser/dev/rustybouncer-evidence/perf-scheduler-20261109/heads-frozen/production
QUEUE_POOL_EVIDENCE=NEW_QUEUE_DIRECTORY nix develop --command python3 tests/queue_pool.py
PROTOCOL_POOL_EVIDENCE=NEW_PROTOCOL_DIRECTORY nix develop --command python3 tests/protocol_pool.py
TRANSACTION_POOL_EVIDENCE=NEW_TRANSACTION_DIRECTORY nix develop --command python3 tests/transaction_pool.py
SESSION_POOL_EVIDENCE=NEW_SESSION_DIRECTORY nix develop --command python3 tests/session_pool.py
RESOURCE_EVIDENCE=NEW_RESOURCE_DIRECTORY nix develop --command python3 tests/resources.py
nix develop --command python3 tests/failover.py
nix develop --command python3 tests/long_queries.py
METRICS_EVIDENCE=NEW_METRICS_DIRECTORY nix develop --command python3 tests/metrics.py
BUFFER_EVIDENCE=NEW_BUFFER_DIRECTORY nix develop --command python3 tests/buffering.py
```

Outstanding: complete before/after 144-arm pool comparisons, candidate original
30-arm comparisons, final normalized 30/original 16-profile matrix, all 180
CTE/large/mixed arms, after-change 24 profiles, memory/failure-load/kind regressions,
variability/resource analysis, README publication, normal-hook commits and workmux
integration. No canonical release checkbox closes through this checkpoint.
