# Measured concurrency results

Run started UTC: `20261107T004433Z`. Binary SHA-256: `0f1b8bfdfeb0a087fa512e9e072371c3bedc775f93380f7972cadf2caab5e473`.

## Outcome

All 24 measured points completed with **zero reported failed transactions and zero pgbench process/setup failures** (11,335,304 successful measured transactions across router and direct-baseline runs).

At 64 concurrent persistent clients, the router served **78,068 SELECT queries/s** (p95 **1.524 ms**) and **61,573 read-only CTE queries/s** (p95 **1.882 ms**). Throughput rose across the tested concurrency levels; this does not locate the maximum sustainable capacity.

The separate admission check established **64 of 80 connections**, rejected **16 during setup** as expected for `max_clients=64`, and all 64 established clients successfully executed SELECT 1. Those expected setup refusals are distinct from the zero SQL errors in the throughput runs.

## Method and environment

- Release binary built with `cargo build --release --locked`; Rust 1.95.0, PostgreSQL/pgbench 16.15.
- AMD Ryzen 7 9700X: 8 physical cores / 16 logical CPUs; approximately 60.4 GiB host RAM. Shared host, no dedicated CPU pinning or controlled background load.
- One real PostgreSQL primary and two streaming replicas in isolated Docker containers. Router and native pgbench load generators run on the same host; Docker host networking reaches loopback-published PostgreSQL ports.
- `route_selects=true`, `max_clients=64`, probe interval 100ms, probe timeout 300ms, query/routing wait 4s, startup timeout 2s. Exact config saved with the run.
- 10,000 rows, integer primary key, 64-character payload. Each transaction is a random key lookup, either a plain SELECT or a MATERIALIZED read-only CTE. No writes or fault injection during measured points.
- Concurrency 1,4,8,16,32,64; pgbench worker threads=min(clients,8); persistent connections and simple protocol. One-second warmup then four measured seconds per point; direct then proxy, SELECT then CTE. Warmup uses separate clients, so initial lazy backend setup is still present in some measured first-query latencies.
- Direct baseline connects to **one replica**, while the router distributes across **two replicas**. Same data/query/client shape, different server count; not an equal-topology or isolated proxy-cost comparison.
- TPS is pgbench successful transactions/s excluding initial connection time. p50/p95/p99 use nearest-rank successful transaction latency from approximately 10% random log sampling, not full-population percentiles. Transaction latency includes random-id generation and receiving the complete query response. Failed counts use full pgbench summary, not sampled estimates.

## Router results

| Workload | Clients | Successful q/s | p50 ms | p95 ms | p99 ms | Sampled latencies | SQL failures |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| select | 1 | 13,952 | 0.065 | 0.109 | 0.135 | 5,617 | 0 |
| select | 4 | 38,726 | 0.097 | 0.146 | 0.188 | 15,359 | 0 |
| select | 8 | 54,621 | 0.129 | 0.252 | 0.397 | 22,075 | 0 |
| select | 16 | 62,275 | 0.224 | 0.474 | 0.717 | 24,882 | 0 |
| select | 32 | 73,975 | 0.378 | 0.818 | 1.197 | 29,325 | 0 |
| select | 64 | 78,068 | 0.728 | 1.524 | 2.175 | 31,036 | 0 |
| cte | 1 | 10,954 | 0.083 | 0.141 | 0.178 | 4,254 | 0 |
| cte | 4 | 30,587 | 0.125 | 0.186 | 0.228 | 12,110 | 0 |
| cte | 8 | 43,694 | 0.167 | 0.291 | 0.452 | 17,535 | 0 |
| cte | 16 | 50,220 | 0.282 | 0.583 | 0.889 | 19,950 | 0 |
| cte | 32 | 54,392 | 0.505 | 1.171 | 1.784 | 21,925 | 0 |
| cte | 64 | 61,573 | 0.936 | 1.882 | 2.654 | 24,619 | 0 |

## Direct single-replica baseline

| Workload | Clients | Successful q/s | p50 ms | p95 ms | p99 ms | SQL failures |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| select | 1 | 43,161 | 0.021 | 0.032 | 0.058 | 0 |
| select | 4 | 134,048 | 0.027 | 0.043 | 0.068 | 0 |
| select | 8 | 199,009 | 0.036 | 0.062 | 0.096 | 0 |
| select | 16 | 222,498 | 0.061 | 0.126 | 0.254 | 0 |
| select | 32 | 286,148 | 0.085 | 0.244 | 0.551 | 0 |
| select | 64 | 317,989 | 0.138 | 0.503 | 1.151 | 0 |
| cte | 1 | 37,882 | 0.024 | 0.037 | 0.063 | 0 |
| cte | 4 | 116,508 | 0.031 | 0.051 | 0.082 | 0 |
| cte | 8 | 169,325 | 0.042 | 0.074 | 0.115 | 0 |
| cte | 16 | 196,793 | 0.069 | 0.142 | 0.262 | 0 |
| cte | 32 | 250,959 | 0.096 | 0.273 | 0.618 | 0 |
| cte | 64 | 289,926 | 0.155 | 0.527 | 1.286 | 0 |

## Interpretation and limits

- At 64 clients the single-replica direct baseline reached 317,989 SELECT q/s (p95 0.503ms) and 289,926 CTE q/s (p95 0.527ms). The current proxy path is substantially slower even with two replica destinations. This establishes an end-to-end difference; it does not isolate parser, buffering, task scheduling, network hops or load-generator effects.
- CTE throughput is lower than plain SELECT in this workload, consistent with additional query work somewhere along the path, but no CPU profile was captured to assign causality to AST parsing.
- Router process CPU reached 290.5% of one core, and end-of-run RSS ranged from 5.13 to 9.38 MiB. CPU divides process CPU time by **whole Docker invocation wall time, including startup/cleanup**, so it is not an exact pgbench-only interval metric. RSS is an endpoint sample, not peak memory. Database/kernel/load-generator CPU and memory are not included.
- Tail latency increases with concurrency; the four-second single-trial sweep is too short to establish stable production percentiles, an SLO concurrency ceiling, sustained capacity, or leak behavior.
- Tiny cached reads do not represent long-running queries, large buffered responses, slow clients, heavy joins, writes, replica lag, or failover under load. A dedicated soak/fault-injection run should follow before changing interruption/retry policy.
- All earlier debug-enabled exploratory measurements were discarded; the reported commands do not pass pgbench `-d`.

## Reproduce and inspect

```sh
cargo build --release --locked
python3 benchmarks/concurrency.py --output /new/empty/results-directory
```

- [Harness and methodology](README.md)
- [Machine-readable JSON](runs/measured-20261106/results.json)
- [Chart-ready CSV](runs/measured-20261106/results.csv)
- [Exact router configuration](runs/measured-20261106/router-config.toml)

Raw pgbench summaries, sampled latency logs (`.gz`), workload SQL and router log are in the same run directory. Docker database containers, anonymous volumes, load-generator containers, network and router process were removed after completion.
