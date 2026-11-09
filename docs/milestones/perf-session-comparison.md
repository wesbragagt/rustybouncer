# PERF-009 — session-source comparison checkpoint (INCOMPLETE)

Authority: [performance tasks](../tasks/pgbouncer-performance.md), PERF-009 and
PERF-005; [canonical checklist](../../FEATURE_COMPLETENESS.md), S10–S12/H11–H13.
This records one complete candidate timing matrix; PERF-009 remains incomplete.
It is not the new bounded-pool comparison or final README acceptance.

The unchanged original benchmark ran **all 30 arms**, three repetitions at 10/100
clients, with zero query errors. Source is the accepted PERF-007 runtime `534c2d0`,
production SHA256
`c3b6bb8e7efec3cc615d48b1229e5b1b4c8c0ef71059c8e98232e61cacce39e7`.
The original fixture enables **no shared Rust pool**: its historical target name
`rustybouncer-session` means opaque passthrough, while `rustybouncer-routed` means
unshared routed mode. Do not interpret these as measurements of `pool_mode=session`.
The new feature's ownership/idle/churn evidence is [recorded separately](perf-session.md).

| Clients | Target | Median Q/s | Median p99 ms | Q/s sample CV |
| --- | --- | ---: | ---: | ---: |
| 10 | Direct PostgreSQL | 210,160 | 0.127 | 3.9% |
| 10 | PgBouncer session | 154,637 | 0.130 | 3.6% |
| 10 | PgBouncer transaction | 148,144 | 0.136 | 3.5% |
| 10 | Rustybouncer passthrough | 154,202 | 0.171 | 9.8% |
| 10 | Rustybouncer unshared routed | 131,997 | 0.182 | 10.8% |
| 100 | Direct PostgreSQL | 271,318 | 2.439 | 5.3% |
| 100 | PgBouncer session | 123,414 | 1.977 | 9.1% |
| 100 | PgBouncer transaction | 127,720 | 2.003 | 1.8% |
| 100 | Rustybouncer passthrough | 147,055 | 2.225 | 2.2% |
| 100 | Rustybouncer unshared routed | 135,976 | 2.155 | 4.0% |

[Every repetition, ranges and CV](perf-session-evidence/original-comparison/summary.json)
are retained, without outlier removal. This shared host shows considerable
between-run and between-matrix variation. In particular, both direct PostgreSQL
and PgBouncer differ from the earlier combined resource matrix. These data do not
establish a separate session-source optimization/regression, a causal scheduling
finding, statistical significance or per-core efficiency. No Rust/Nix builds or
heavy verification ran during this timing matrix; code drafting continued in the
isolated worktree. Other host workloads remain outside benchmark control.

Linux x86_64, owned PostgreSQL 16 Docker fixture, trust/loopback, TLS/metrics off;
5-second warmups and 30-second measurements. Pool/client limits remain the
original 256 and PostgreSQL capacity 400, so there is no capacity-saving claim.
Per-target CPU/RSS and equal-core measurements are separate pending runs.
The actual frozen binary, all native/60-case verification and source hashes are
recorded in the session milestone; no source changed during its accepted runs.

```sh
nix develop --no-update-lock-file --command python3 benchmarks/compare.py \
  --output /home/publicuser/dev/rustybouncer-evidence/perf-pools-20261109/session-original-comparison \
  --binary /home/publicuser/dev/rustybouncer-evidence/perf-pools-20261109/session-frozen/production
```

Use a fresh output directory to reproduce; retain the original result. The
[run log](perf-session-evidence/original-comparison/run.log),
[environment](perf-session-evidence/original-comparison/environment.json),
[raw results](perf-session-evidence/original-comparison/results.json),
[raw archive hash](perf-session-evidence/original-comparison/archive.json) and
[per-file SHA256 manifest](perf-session-evidence/original-comparison/manifest.json.gz)
include complete latency logs, commands, configs and independently observed
backend counts. The original `c8997e8` dataset remains unchanged.

Open gates: full PERF-008 positive scope and 1,000-client pool comparison; final
candidate original/equal-core profiles; all CTE/large/mixed workloads; bounded-pool,
churn/idle and failover-under-load experiments; final variability analysis and
README trade-offs. The next benchmark harness is a prototype until those required
runs pass. No final checklist acceptance is claimed by this checkpoint commit.
