# PgBouncer comparison — S10/S12

Authority: the local feature checklist. This milestone
measures a declared workload; it does not establish the complete conformance,
1,000-client, mixed-load or sustained-capacity checklist. No performance budget
is imposed and no required test is skipped.

## Reproduce

```sh
nix develop --no-update-lock-file --command cargo build --release --locked --offline
nix develop --no-update-lock-file --command python3 benchmarks/compare.py --output /absolute/path/to/a/new/comparison-run
```

First-use Cargo dependencies can be fetched with `cargo fetch --locked`. Docker,
pgbench and PgBouncer come from the locked shell. The runner refuses an existing
output directory; no reduced-duration/concurrency or target-filter options exist.
It creates one UUID-owned PostgreSQL Docker container published only on loopback,
then removes it and terminates only its own subprocesses in finally blocks.

## Method

Five targets: direct PostgreSQL, PgBouncer session, PgBouncer transaction,
Rustybouncer passthrough (`route_selects=false`) and routed mode (`true`) with one
configured primary. All share the same physical primary and database, TCP
transport, trust authentication and simple query protocol. Metrics and TLS are
disabled. Each frontend limit is 256, PgBouncer pool maximum 256, PostgreSQL
max_connections 400. These are capacity ceilings, not a forced server count;
transaction pooling can reuse fewer backends naturally.

The workload performs random primary-key lookups in 10,000 rows with 128-byte
payloads. Preflight opens the complete client count concurrently and verifies
both protocol completion and known lookup values before timing. Persistent
pgbench clients use 10 or 100 connections and one worker thread per client.
Each target/concurrency pair has three repetitions, each with 5s warmup and 30s
measurement. Order is deterministically rotated/reversed between repetitions.
All 30 arms must complete with zero reported transaction errors and nonempty
latency samples. There is no winner-dependent selection or early stopping.

Full pgbench transaction/error totals and command output are retained. Independent
random 10% transaction logs yield p50/p95/p99 (floor-index quantiles). Per-group
summary latencies are medians of per-run quantiles, not pooled quantiles. Actual
PostgreSQL backend counts are sampled every five seconds in every treatment;
the verifier's session and a separate discovery-probe role are excluded. Samples
can straddle startup; peaks are observations, not configured limits.

All targets run as one native process on a shared Linux development host; the
PostgreSQL server is containerized. No CPU affinity, equal-core quota or exclusive
host is asserted. Different threading models, the host load, hot dataset, lack of
TLS and simple protocol limit extrapolation. This is not a failover, replica-scaling,
connection-churn, bulk-result, write or prepared-statement benchmark. Those behaviors
must not be inferred from these numbers. Rustybouncer's buffering/classification
cost is represented by routed mode; passthrough results describe different semantics.

## Results and evidence

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

All 30 arms completed with zero transaction errors. Total measured transactions:
**150,702,831**. No valid repetition was removed.
[All runs](pgbouncer-comparison/results.json), [summary](pgbouncer-comparison/summary.json),
[environment](pgbouncer-comparison/environment.json), [source hashes](pgbouncer-comparison/provenance.json),
[run log](pgbouncer-comparison/run.log.gz), and [raw manifest](pgbouncer-comparison/raw-manifest.json.gz)
are retained. Full raw logs, configuration and sampled latencies remain at
`/home/publicuser/dev/rustybouncer-evidence/pgbouncer-comparison-20261108/run-1` (1907 files). The manifest hashes every raw file.

The Rust binary SHA-256 is `b58acae3b99be386af2640bdca00dc6efd1f7e42510655bdd2d9531d60a9bdab`.
All targets used the same primary/image digest and controls. Read the README
for the scope-qualified interpretation. Final locked Nix/installed-hook verification
is required before committing; the actual commit and results are reported at handoff.
No router source was changed, so this benchmark/documentation milestone does not
replace or claim new failover/protocol conformance evidence.


[Interactive comparison](pgbouncer-comparison/chart.html) shows each run, median
and range for throughput and p95.

## How to choose

PgBouncer is the starting point for applications needing shared backend capacity,
connection reuse across clients, authentication/TLS, administrative controls and
broad protocol compatibility. Transaction pooling requires applications to respect
its session-state restrictions; protocol prepared statements require suitable
configuration. See the [upstream feature map](https://www.pgbouncer.org/features.html)
and [configuration](https://www.pgbouncer.org/config.html).

RustyBouncer is a role-aware router. Routed mode can choose replicas
for conservative SELECT/CTEs, discover a changed primary and retry eligible buffered
reads before exposing results. It has no shared cross-client pool. Routed mode
currently requires trusted PostgreSQL authentication and simple queries; it rejects
extended protocol/COPY and does not support cancellation routing or TLS. Sessions
can cache one backend per configured node. The 32-MiB result buffer limit and
per-query allocations matter for large results and memory pressure; this lookup
workload does not measure that cost at scale. Passthrough preserves backend
protocol/authentication but does not offer SQL-aware routing/read retries.

Modern PgBouncer can select connection destinations through configured host lists
and DNS; this is distinct from Rustybouncer's SQL classification and PostgreSQL
role probes. External DNS, a proxy, reload or reconnect can integrate PgBouncer
with failover orchestration. Both deployments still need external promotion,
replication management and fencing; neither makes uncertain writes safe to replay.
See [host-selection configuration](https://www.pgbouncer.org/config.html#host)
and the [failover FAQ](https://www.pgbouncer.org/faq.html#how-to-failover).

One PgBouncer instance uses one CPU core; Rustybouncer uses Tokio's multithreaded
runtime. The single-process comparison has no equal-core quota, so it is not a
CPU-efficiency ranking. PgBouncer can be deployed with multiple instances and
SO_REUSEPORT; that topology is not measured here. See
[upstream SO_REUSEPORT documentation](https://www.pgbouncer.org/config.html#so_reuseport).

## Follow-up work

Prioritized performance tasks distinguish
measured gaps, code-inspection hypotheses and unmeasured pooling/resource gaps.
Task acceptance preserves routing safety and the full required verification scope.
