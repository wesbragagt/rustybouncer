# PERF-008 — complete bounded-pool comparison and ownership acceptance

Authority: performance tasks, PERF-008,
P04–P06/P08/P10, L01–L10, T01–T10 and W02–W12 in
the local feature checklist.
This records the scoped pooling milestone, not broad canonical release acceptance.
Source checkpoints: leases `8fa5330`, queues/fencing `058c151`, prepared/extended
ownership `487a7b4`. Canonical deployment/workload criteria remain open.

## Accepted behavior and complete scope

The prepared ownership source passes **144 complete live cases**, **64 Rust
+72 offline Python tests**, all static checks and the normal installed locked hook.
[Protocol acceptance](perf-protocol.md) and its raw evidence retain every required
positive transaction/queue/prepared/cancellation/failure case, including actual
10/100/1,000-client scopes. The global/per-key/reserve limits, real I/T/E ownership,
FIFO admission, user/database isolation, bounded waits, cancellation/EOF refunds,
reset safety and generation fencing preserve exclusive ownership. No in-flight
write or transaction replay is introduced.

The same source now completes all **144/144 bounded-pool comparison arms** with
**zero query errors**, no excluded repetitions and all requested clients finishing.
Both Rust modes and both PgBouncer modes are configured with 16 total PostgreSQL
connections, no reserve and 1,200 frontend capacity. Independent PostgreSQL
observations remain at or below 16 in every measured arm.

Each arm first provisions **all 10/100/1,000 frontend connections**, verifies the
startup barrier and physical counts, then executes exact row/identity and named
prepared oracles inside transactions and again after COMMIT. This preflight
includes every requested client; there are no reconnect/SQL retries. The normal
load uses BEGIN, an indexed lookup with per-transaction identity/payload guards,
and COMMIT, with either connection churn or persistent frontend sessions.
Five-second warmup precedes ten-second measurement plus full completion drain.
All transactions are logged: every measured client ID must appear, and log count
must equal processed transactions. Slow queued completions stay included.

Two placements (unrestricted and one proxy CPU/one Tokio worker), three repetitions,
10/100/1,000 clients, two workloads and four targets produce exactly 144 arms.
PostgreSQL/pgbench are not constrained with the proxy. Actual affinity, worker
counts, cgroup limits, CPU/RSS, backend counts, commands, configurations and raw
latencies are retained. Logging overhead is included identically in this matrix;
these multi-command TPS values are not the historical single-query Q/s.

## What the comparison shows

Median unrestricted observations, with all repetitions/ranges/CV available in the
[full summary](../benchmarks/perf-pools/summary.md):

| Workload | Clients | PgBouncer session TPS | Rust session TPS | PgBouncer transaction TPS | Rust transaction TPS |
| --- | ---: | ---: | ---: | ---: | ---: |
| Churn | 10 | 13,809 | 13,547 | 14,297 | 12,392 |
| Churn | 1,000 | 12,594 | 4,042 | 13,254 | 3,546 |
| Persistent transactions | 10 | 45,071 | 35,334 | 43,684 | 23,492 |
| Persistent transactions | 1,000 | 40,461 | 32,816 | 38,723 | 4,057 |

Both implementations demonstrate the declared backend ceiling; this is no longer
a 1,000-frontend/1,000-backend cache experiment. Rust does **not** match PgBouncer
throughput/per-core cost in this study. The largest observed gap is shared-pool
high-concurrency churn/transaction admission. It remains PERF-005 investigation,
not an unmeasured claim that a particular lock or notification is the cause.

At 1,000 persistent session clients, both poolers have roughly ten-second maximum
queueing latency and clients completing only one transaction, while median p99
can remain low because the 16 active sessions produce most samples. The raw
maximum/per-client completion evidence prevents hiding this inherent session
pooling head-of-line behavior behind aggregate p99. Transaction pooling trades
that session affinity for release after each actual idle transaction.

Three shared-host repetitions do not establish statistical significance or a
production SLO. One coordinator-owned workmux checkout was provisioned during the
matrix; this known host activity is recorded, and every sample remains retained.
No compiler, functional suite or profiler ran during normal timing. The following
scheduling profiles are separately instrumented and cannot replace timing evidence.

## Reproduction and retained artifacts

Production SHA256:
`208e98670db9a76d3ce4ca2528cedda4b8e4f1a27e1b162fcfe747ae7efef9b3`.
Linux x86_64, locked Nix, PostgreSQL 16 image digest, PgBouncer 1.26.1,
pgbench 18.6, trust authentication on owned loopback, TLS/metrics disabled.

```sh
nix develop --no-update-lock-file --command python3 benchmarks/pool_compare.py \
  --binary /absolute/path/to/frozen-production \
  --output /absolute/path/to/fresh-pool-comparison
nix develop --no-update-lock-file --command python3 benchmarks/performance_summary.py \
  --kind pools --results /absolute/path/to/fresh-pool-comparison/results.json \
  --output /absolute/path/to/fresh-pool-summary
```

[Environment/provenance](../benchmarks/perf-pools/environment.json),
[all 144 results](../benchmarks/perf-pools/results.json),
[all repetitions/paired ratios](../benchmarks/perf-pools/summary.json),
[complete progress log](../benchmarks/perf-pools/complete-run.log),
[raw archive hash](../benchmarks/perf-pools/archive.json), compressed per-file
manifest, frozen harness source copies and raw archive retain 4,474 files.
The original comparison/reference `c8997e8` remains untouched.

PERF-005 scheduling improvement and PERF-009 final original/normalized/workload,
profiling, failover-under-load and README publication remain open. The new future
harnesses are separate incomplete work until their complete required scopes pass.
This milestone does not claim every canonical pooler feature, a performance SLO,
or full deployment-matrix conformance.
