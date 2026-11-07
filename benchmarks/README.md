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
