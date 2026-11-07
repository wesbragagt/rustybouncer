# Black-box conformance validation: 2026-11-07

## Result and scope

The same **62 assertions** ran against upstream **PgBouncer 1.26.1** and the
current rustybouncer simple-query router. Product differences were declared in
manifests; test assertions did not change between products. All enabled baseline
assertions passed. Unsupported or capacity-inapplicable scenarios are explicit
skips, not successes.

| Deployment | Contract | Repetitions | Passed | Skipped | Failures/errors |
| --- | --- | ---: | ---: | ---: | ---: |
| Native PostgreSQL 18.6, native poolers | PgBouncer transaction | 2 | 76 | 48 | 0 |
| Native PostgreSQL 18.6, native poolers | rustybouncer routing | 2 | 44 | 80 | 0 |
| Docker PostgreSQL 16.15, native poolers | PgBouncer transaction | 2 | 76 | 48 | 0 |
| Docker PostgreSQL 16.15, native poolers | rustybouncer routing | 2 | 44 | 80 | 0 |
| Docker PostgreSQL 16.15 and Docker poolers | PgBouncer transaction | 2 | 76 | 48 | 0 |
| Docker PostgreSQL 16.15 and Docker poolers | rustybouncer routing | 2 | 44 | 80 | 0 |
| Native PostgreSQL, native poolers | PgBouncer session | 1 | 31 | 31 | 0 |
| Native PostgreSQL, native poolers | PgBouncer statement | 1 | 15 | 47 | 0 |

Each row's counts include its repetitions. Different pass counts reflect different
contracts and are not comparative quality scores. The extra target runs paired
with the session/statement reference runs each passed 22 and skipped 40 cases.
The required 10-client concurrency case ran; 100/1000 clients were explicitly
skipped because the fixture declares capacity 10. No throughput budget was set.

[Machine-readable aggregate](summary.json) compares the identical test inventory
and retains pass/skip/error counts without treating optional-feature skips as
successes.

## What was validated

The PgBouncer transaction profile exercises backend reuse, actual capacity release
after commit/rollback, transaction isolation/savepoints/errors, simultaneous
backend ownership, named/unnamed extended preparation, raw binary parameters,
forced backend reassignment, name collisions, prepared-cache eviction, pipelines,
limits, queue timeouts, disconnected/canceled waiters, active-query cancellation,
backend termination and repeated recovery, PostgreSQL restart, concurrency/churn,
concurrent preparation, mixed short/blocked transactions, and optional pool stats.

The rustybouncer profile exercises core simple queries, nested/recursive CTEs,
transaction semantics and pinning, stateful SQL, SQL PREPARE, binary/NULL decoding,
backend termination, interrupted-transaction non-replay, PostgreSQL restart,
repeated recovery, 10-client response isolation, churn, and mixed transactions.
It does not claim shared backend reuse, cancellation, TLS, or extended protocol.

A blocked-transaction test observes `pg_blocking_pids()` before issuing short work.
Transaction-release cases fill the declared two-backend pool, observe a waiting
operation, then release capacity. A prepared-statement case holds the original
backend and frees a different one before execution, demonstrating reassignment
rather than assuming backend rotation. Protocol statements use explicit libpq
prepare/execute calls without a driver's named-statement cache.

Both repeated deployments restart PostgreSQL through optional adapters and then
assert eventual SQL availability without restarting either pooler. Each iteration
creates new UUID schemas and cleans them up. No application SQL assertion silently
retries failures; only explicit recovery observations reconnect/poll.

## Conditions and reproducibility

- Linux x86_64; Python 3.14.8, Psycopg 3.3.6; pinned Nix development tools.
- rustybouncer binary SHA-256:
  `49b71cb74548b35b03c0290cb2e9b04222aa355413c8d13eaad3fd6308d08548`.
- Local-only trust authentication, loopback endpoints, dedicated disposable data.
- Reference pool size 2, client limit 24, prepared capacity 4, query wait 1 second,
  and server-login retry 1 second. Recovery budget 15 seconds; operation budget
  8 seconds. Exact manifests are retained with each run.
- The login retry was set explicitly after an exploratory Docker restart exceeded
  the earlier recovery budget under PgBouncer's default retry delay. No assertion
  was weakened to accept an unexpected outage. Exploratory failed runs are not
  represented as passing final measurements.
- JSON/JUnit reports and logs are retained under [reports](reports/). Comparison metadata records the installed native PostgreSQL CLI as
  `host_postgres_version`; the Docker database's actual 16.15 version is evidenced
  by its `postgres.log`, not that host-tool field.

```sh
nix develop --command cargo build --locked
nix develop --command python3 -m conformance.adapters.local \
  --output /tmp/new-native-results --repeat 2
nix develop --command python3 -m conformance.adapters.local \
  --output /tmp/new-docker-results --docker-postgres --docker-poolers --repeat 2
nix develop --command python3 -m conformance.adapters.local \
  --output /tmp/new-session-results --reference-mode session
nix develop --command python3 -m conformance.adapters.local \
  --output /tmp/new-statement-results --reference-mode statement
```

All output directories must be new. Native and fully containerized deployments use the same host; a hybrid run
with Docker PostgreSQL and native poolers also passed. The container adapter
mounts the same Nix-built binaries and libraries read-only into containers, so
this is deployment isolation, not a different compiler/OS build or Kubernetes
validation.

## Limits and remaining acceptance

Optional security/lifecycle/admin/fault cases are implemented but not exercised
unless their manifest features, fixtures and explicit adapter authorization are
supplied. This run does **not** validate SCRAM/MD5 negatives, certificate/TLS policy,
multiple users/databases, reserved pools, idle/lifetime policies, reload semantics,
pause/drain configurations, network interruption, or pooler restart. Optional
statistics currently establish availability/nonempty output, not full accounting
consistency. Reload currently establishes service continuity, not every option's
before/after value. See [acceptance.md](acceptance.md) for the remaining detailed
checklist coverage. These gaps are not hidden behind a claim of full certification.

Short concurrency tests assert correctness and completion. They record per-case
throughput/p50/p95/p99 but do not establish sustained capacity, resource leak bounds,
long-running failover recovery, or an exactly-once guarantee for ambiguous COMMIT.
No CPU/RSS resource-budget conformance is inferred from these results. The separate
performance benchmark remains in `benchmarks/`.
