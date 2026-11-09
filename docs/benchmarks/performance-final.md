# Final performance comparison protocol — PERF-005/009 (in progress)

Authority: [performance tasks](../tasks/pgbouncer-performance.md) and
[FEATURE_COMPLETENESS.md](../../FEATURE_COMPLETENESS.md). This protocol is a draft
until the complete evidence and its acceptance commit are linked below. It does
not close canonical release requirements. Historical reference `c8997e8` and its
[original report](pgbouncer-comparison.md) remain unchanged.

## Frozen sources and controls

The pre-scheduler runtime source is `487a7b4`; the bounded-notification runtime
source is `14f077e`. Complete harness checkpoint `e4e9968` defines the expanded
matrices. The final retained source snapshots and hashes identify subsequent
reporting and fault-harness corrections. Use separate workmux checkouts and
freeze artifacts before measurements. Preserve each binary; never overwrite an
ELF needed to interpret earlier profiler data.

Production binaries use locked release builds. Separate profile binaries retain
full debug information and frame pointers:

```sh
nix develop --no-update-lock-file --command cargo build --release --locked --offline
nix develop --no-update-lock-file --command env \
  CARGO_TARGET_DIR=/absolute/path/to/a/separate/profile-target \
  CARGO_PROFILE_RELEASE_DEBUG=2 RUSTFLAGS=-Cforce-frame-pointers=yes \
  cargo build --release --locked --offline
```

Record binary/source SHA256, tool versions, PostgreSQL image digest, actual
process/thread affinities, cgroup controls and worker counts. Unrestricted runs
use one process per target with its normal worker model. Normalized runs bind only
the proxy to one CPU and set one Tokio worker; PostgreSQL and pgbench keep their
recorded unrestricted affinity. Both are shared-host observations. Run normal
matrices sequentially, with no owned compiler, verification, profiler or archive
job during their timing windows. Record observed external activity; retain all
repetitions and all slow queued completions.

## Mandatory observations

The original raw target name `rustybouncer-session` means historical passthrough
with `route_selects=false`; it does not enable shared session pooling.
`rustybouncer-routed` uses unshared routed mode. Only `rustybouncer-pool-session`
and `rustybouncer-pool-transaction` enable the new shared pools.

| Scope | Complete arms | Work and interpretation |
| --- | ---: | --- |
| Original lookup before/after | 30 each | Unchanged five-target comparison, 10/100 clients, three repetitions; 5s warmup +30s measurement, random 10% latency logs |
| Normalized original lookup | 30 | Same lookup, one proxy CPU and one Tokio worker; separate from unrestricted |
| Original attribution | 16 | Two Rust modes, 10/100 clients, CPU/off-CPU/heap/syscalls; instrumented observations |
| Small pools before/after | 144 each | Four poolers, 10/100/1,000 actual clients, two placements, churn/persistent transactions, three repetitions; 16-backend ceiling |
| Shared-pool attribution before/after | 24 each | Both Rust pooling modes, 100/1,000 clients, both placements, CPU/off-CPU/syscalls |
| CTE/large/mixed workloads | 180 | Original five targets, 10/100 clients, two placements and three repetitions |
| Shared-pool duration mix | 96 | Both candidates, four poolers, 10/100 clients, both placements and three repetitions |
| Crash/promotion under load | 24 | Four poolers, 10/100 active workers and three repetitions; synchronous writes independently reconciled |
| Independent Memcheck | 8 | Four Rust modes at 10/100 clients; no default suppressions; all workload connections gone after shutdown |
| Slow-receiver regression | Four experiments, three cycles each | 10/100 clients before/after; execution-complete/held-delivery barriers, exact small results and byte/deadline/refund checks |
| Final live regression | 144 cases | Entire queue/protocol/transaction/session/resource/failover/long-query/metrics/buffering files |
| Final kind/CNPG | Six cases | Actual operator, streaming replicas, primary crashes/promotions, metrics and interrupted transaction semantics |

Additional normal workloads use 5s warmup +10s observation and do not replace the
original 30s measurements. Their logs retain every successful transaction and
must include every requested client. CTE/short measured lookups check returned
identity and length; every client independently verifies exact workload bytes
before load, and complete large rows are also checked afterward. The numeric
lookup checks use the documented [pgbench result variables](https://www.postgresql.org/docs/18/pgbench.html)
with one returned row per `\gset` command. The shared-pool
lookup and the additional CTE/short workloads return four columns, including the
128-byte payload and their validation fields. This response shape differs from
the original one-column payload lookup. The shared-pool
comparison includes positive named prepared/row oracles for all 1,000 frontends;
its measured workload remains simple protocol, not prepared-statement throughput.

Memcheck runs use declared instrumentation budgets. The original four arms keep
their 30s route budget. The four shared arms allow 180s queue waits and 60s wire
reads so all 100 clients can pass through 16 instrumented backends; the complete
pgbench invocation still has its 180s bound. Client and transaction counts are
unchanged. Native timing and all native deadline/cleanup cases keep their own
settings and are not replaced by these VM observations.

## Reproduction

Use a fresh output root and explicit frozen artifact paths. The runners reject
existing output directories and expose no target filters, skip flags or reduced
concurrency options. Enter the locked shell with `nix develop --no-update-lock-file`. Set
`PERF_RESULTS` to the new study root, `PERF_PRODUCTION`/`PERF_BEFORE` to the two
frozen release executables, `PERF_PROFILE` to the final debug-info executable,
`PERF_POOL_BEFORE` to the complete pre-scheduler pool result directory, and
`PERF_IMAGE` to the recorded final image tag. Before/after pool
and original matrices must both be retained; below are the final candidate arms:

```sh
python3 benchmarks/pool_compare.py --binary "$PERF_PRODUCTION" --output "$PERF_RESULTS/pools-after"
python3 benchmarks/compare.py --binary "$PERF_PRODUCTION" --output "$PERF_RESULTS/original-after"
python3 benchmarks/workload_compare.py --binary "$PERF_PRODUCTION" --output "$PERF_RESULTS/workloads-after"
python3 benchmarks/pool_mixed.py --before-binary "$PERF_BEFORE" --after-binary "$PERF_PRODUCTION" --output "$PERF_RESULTS/pool-mixed"
python3 benchmarks/performance_profile.py --binary "$PERF_PRODUCTION" --profile-binary "$PERF_PROFILE" --output "$PERF_RESULTS/normalized-after"
python3 benchmarks/pool_profile.py --production-binary "$PERF_PRODUCTION" --profile-binary "$PERF_PROFILE" --output "$PERF_RESULTS/profiles-after"
python3 benchmarks/memory_audit.py --binary "$PERF_PROFILE" --output "$PERF_RESULTS/memory-after"
python3 benchmarks/failover_load.py --binary "$PERF_PRODUCTION" --output "$PERF_RESULTS/failover-load"
```

Build and load the final non-root distroless image with `nix build .#docker` and
`docker load`. Record its immutable image identity and run all six cases with
`python3 -m tests.kind_cnpg --image "$PERF_IMAGE" --output "$PERF_RESULTS/kind-after"`.
The image build may have a different ELF hash from the standalone release build;
retain its exact source and image identities rather than asserting byte identity.

Run `nix develop --no-update-lock-file --command bash scripts/verify`, retaining
`final-native.log`, and use the installed hook normally for acceptance commits.
Generate the separate attribution summaries and complete report after every
required scope finishes:

```sh
python3 benchmarks/pool_profile_report.py --profiles "$PERF_RESULTS/profiles-after" --output "$PERF_RESULTS/profiles-after-summary.json"
python3 benchmarks/profile_report.py --baseline "$PERF_RESULTS/original-after" --normalized "$PERF_RESULTS/normalized-after" --profiles "$PERF_RESULTS/normalized-after" --output "$PERF_RESULTS/final-profile-summary.json"
python3 benchmarks/final_report.py --run-root "$PERF_RESULTS" --pools-before "$PERF_POOL_BEFORE" --output "$PERF_RESULTS/final-report"
```

## Interpretation and limits

Use every repetition, ranges, sample CV and matching-repetition before/after
ratios. Medians of per-run percentiles do not pool samples across repetitions.
Three shared-host runs, separated before/after batches and external activity do
not establish causal isolation, statistical significance or a production SLO.
There is no invented performance pass/fail budget. Instrumented CPU callchain
shares are not hypothetical marginal elapsed costs; off-CPU intervals include
sleep and runnable delay. RSS, observed peaks and lifetime high-water marks have
different meanings; the bulk-buffer byte cap does not cap total process RSS.
CPU and RSS describe the proxy process, excluding PostgreSQL, Docker forwarding
and the load generator. Client transaction latency includes the complete exchange;
it is not PostgreSQL-only execution latency. Slow-receiver barriers distinguish
completed database work from held frontend delivery in the separate resource cases.

Session pooling can leave clients queued until an active session closes. Maximum
latency and minimum completions per client stay visible alongside p99. Native
pool identity includes original startup parameters, so preflight/pgbench identities
can retain different idle backend groups within the same global limit. Configured
ceilings do not guarantee identical actual backend counts in each treatment.

The fault experiment uses `ANY 2 (*)` synchronous acknowledgements before killing
the primary, then promotes a physical standby. Every worker must acknowledge a
new write after promotion and continue through the stable phase. Uncertain writes
are independently reconciled, not replayed. Database statement logging is enabled only on the owned
fault fixtures: every acknowledged/committed INSERT must have exactly one SQL
execution record, and any duplicate execution fails even if its second attempt
would hit the primary key. This instrumentation is separate from normal timing.
Expected outage errors are positive fault observations; unexpected persistent
errors fail the experiment. PgBouncer
uses an explicit host rewrite plus RELOAD/RECONNECT adapter; Rust uses discovery.
This is not a ranking of every PgBouncer HA deployment.
Read operations return distinct per-operation literals; committed write rows are
checked independently on the promoted primary. The manual fixture does not
reparent its remaining standby, so it does not establish replica freshness or
read-after-write consistency. CNPG manages its own recovery topology in the
separate Kubernetes suite.

TLS and broad authentication/protocol/administration requirements, replica-read
scaling and production soak capacity remain separate canonical acceptance work.
The final README must describe workload-specific benefits and remaining costs.
