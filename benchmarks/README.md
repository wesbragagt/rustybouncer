# Concurrency benchmark

`concurrency.py` measures the current query router with native PostgreSQL `pgbench`
load generation, persistent client connections, and an isolated real PostgreSQL
primary plus two streaming replicas. It reuses the cluster lifecycle fixture from
`tests/failover.py`; existing services/databases are not modified.

```sh
cargo build --release --locked
python3 benchmarks/concurrency.py
```

Requires Docker, Python 3 standard library and the `postgres:16` image. Native
`pgbench` runs in a temporary Docker container with host networking, avoiding a
Python/GIL load-generation bottleneck. The router and all databases run on the
same host. Docker containers, volumes, network and router process are cleaned up.

Options:

```sh
python3 benchmarks/concurrency.py --levels 1,8,32,64 --seconds 10 --warmup-seconds 2
python3 benchmarks/concurrency.py --binary /absolute/path/to/rustybouncer --output /new/results/directory
```

Default workloads perform a random primary-key lookup in a 10,000-row table:
ordinary SELECT and a read-only MATERIALIZED CTE. Each transaction contains one
query returning an id plus a 64-character payload. Each point has a separate
one-second warmup, then four measured seconds. Tests alternate direct-replica and
router targets at each of 1, 4, 8, 16, 32 and 64 clients. The direct baseline uses
one replica; the router uses both, so this is an end-to-end overhead comparison,
not an equal-server-count scaling experiment. No writes run during measurements. Warmup and measured invocations use separate
clients: warmup primes database caches, while measured runs still include lazy
replica-backend setup in their first query latencies.

A final admission check attempts 80 simultaneous persistent connections against
`max_clients=64`, recording connection/setup failures separately from SQL errors
on established clients. It is not counted as a throughput benchmark.

Each run writes:

- `results.json`: environment, configuration references, commands, measurements,
  success/failure counts and overload outcomes.
- `results.csv`: chart-ready results, one row per measured point.
- `*.stdout.txt`: exact pgbench output, including connection time and transaction totals.
- `*.latency.*.gz`: sampled raw pgbench transaction logs.
- `select.sql`, `cte.sql`, `router-config.toml`, `router.log`: workload/config/log artifacts.

Successful TPS is pgbench's reported TPS excluding initial connection time.
Latency percentiles use nearest-rank p50/p95/p99 from successful transaction log
samples (`--sampling-rate=0.1`, approximately 10%), in milliseconds. They measure
the pgbench transaction, including the random-id metacommand and response receipt;
they are not PostgreSQL-only execution time. Failed-transaction counts come from
pgbench's full summary, not extrapolation from sampled logs. Process/setup failures
are separately captured through process status and error output. There are no
client retries or reconnections during a measured run.

Router CPU percentage is process CPU time divided by end-to-end Docker invocation wall duration (startup and cleanup included),
expressed relative to one CPU core (can exceed 100%). RSS is the router's resident
memory at the end of a run, not peak memory. Both include background discovery;
direct-baseline runs leave the router's background probes running.

Short closed-loop runs of a cached, tiny-result workload cannot establish maximum
production capacity or long-query behavior. Client, proxy and databases share CPU,
network and memory, and external host load is uncontrolled. Sampling, kernel
scheduling, Docker networking, connection churn and test order affect the results.
Existing nonempty output directories are refused to avoid mixing trials. SIGINT and
SIGTERM trigger cleanup, including removal of the active load-generator container.
Use longer repeated trials and production-shaped workloads before capacity planning.

## Performance-task acceptance matrices

PERF-001–009 define acceptance;
the local feature checklist remains authoritative.
The accepted pool milestone is `0604ee8`: all 144 live cases and all 144
bounded-pool comparison arms pass with zero failures/errors/skips. The scheduler
candidate `14f077e` passes 66 Rust tests and all 144 live cases; its performance
acceptance remains incomplete pending the full final matrices. Historical lookup
reports stay unchanged.

The additional harnesses below require a fresh output directory and an explicitly
frozen release binary. They have no target/arm filters, skip flags or downscaling
switches. Complete execution and row/capacity evidence, not partial progress,
establishes acceptance.

```sh
nix develop --command python3 benchmarks/pool_compare.py \
  --binary /absolute/path/to/frozen-production \
  --output /absolute/path/to/new-pool-comparison
nix develop --command python3 benchmarks/workload_compare.py \
  --binary /absolute/path/to/frozen-production \
  --output /absolute/path/to/new-workload-comparison
nix develop --command python3 benchmarks/failover_load.py \
  --binary /absolute/path/to/frozen-production \
  --output /absolute/path/to/new-failover-load
nix develop --command python3 benchmarks/pool_mixed.py \
  --before-binary /absolute/path/to/frozen-before \
  --after-binary /absolute/path/to/frozen-after \
  --output /absolute/path/to/new-pool-mixed
```

- Pool comparison: **144 arms**, four session/transaction poolers, 10/100/**1,000**
  actual frontends, two CPU placements, churn/persistent transactions and three
  repetitions. All use a 16-backend ceiling; 1,200 frontend capacity provisions
  the requested load. A complete concurrent prepared/row preflight precedes each
  arm. Full transaction logging proves every measured client completes, includes
  the slowest queued completions and adds the same logging overhead to all targets.
  The pgbench thread cap of 16 does not reduce its client count.
- Additional workloads: **180 arms**, the original five targets, CTE/large/mixed
  SQL, 10/100 clients, both CPU placements and three repetitions. Full logging
  preserves per-client completion and short/long latency distributions separately.
  These 5s-warm/10s-observation runs do not replace the unchanged original 30s
  lookup measurements.
- Shared-pool duration mix: **96 arms**, both frozen candidates, four poolers,
  10/100 actual clients, two CPU placements and three repetitions. Each client
  verifies exact workload bytes/identity before timing. All measured completions,
  separate short/long tails, maxima, CPU/RSS and the 16-backend ceiling are retained.
- Failure load: **24 experiments**, four shared poolers, 10/100 active workers
  and three repetitions. A real primary is killed and a physical replica promoted.
  Unique operations are never replayed; acknowledged synchronous writes and
  uncertain outcomes are independently reconciled with the database. PgBouncer
  uses an explicit configuration/reload/reconnect adapter; Rust uses discovery.
  This tests that configuration, not every possible PgBouncer HA deployment.

Run normal timing alone, with no compilers, verification suites or profilers.
Run attribution afterward. `pool_profile.py` additionally requires a debug-info
profile build and the matching production binary; it runs all **24** shared-pool
CPU/off-CPU/syscall arms at 100/1,000 clients and both placements. Instrumented
observations are not normal throughput evidence. The original 30 equal-core
and 16 attribution arms in `performance_profile.py` remain separately mandatory.

`performance_summary.py` accepts only a complete 144-/180-arm result set, rejects
duplicates/errors/missing client completions and retains all three repetitions,
ranges, CV and paired ratios. It does not apply an invented TPS/latency budget.
Direct PostgreSQL has no proxy CPU/RSS; missing proxy measurements are never
reported as zero-cost database execution.

The independent `memory_audit.py` runs all eight Memcheck arms: the original
passthrough/unshared-routed modes plus both shared modes at 10/100 clients.
Shared mode retains at most 16 idle backends before shutdown; all physical
workload backends must disappear after shutdown. No default suppressions are
used. Memcheck observations are separate from native timing and native deadlines.
