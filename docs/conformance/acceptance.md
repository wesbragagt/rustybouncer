# Conformance acceptance and coverage

> Current authority: the local feature checklist.
> The zero-skip policy supersedes the historical baseline bookkeeping below.
> Those runs do not complete current feature or release gates; all eight current
> release gates remain open until the required matrix passes without skips.

## Baseline execution checklist

These eight execution checks are complete **for the supplied capability contracts**.
This is not completion of every feature in the broader requested checklist.

- [x] Run the complete 62-case inventory against upstream PgBouncer.
- [x] Run the same inventory against rustybouncer without changing assertions.
- [x] Compare capability-adjusted results and retain manifests.
- [x] Verify all enabled required assertions pass in the measured deployments.
- [x] Verify undeclared features and insufficient capacity are explicitly skipped.
- [x] Repeat concurrency and recovery cases twice in each baseline deployment.
- [x] Run native and fully Docker-containerized deployments, plus a hybrid variant.
- [x] Generate JSON, JUnit, logs and a final scoped report.

## Detailed coverage still requiring work or validation

| Area | Implemented baseline | Remaining scope |
| --- | --- | --- |
| Harness | DSNs, TLS/credentials via libpq, schemas, capabilities, bounded operations, JSON/JUnit, optional argv adapters | Validate a remote/Kubernetes deployment and Windows-specific event-loop compatibility if required |
| Pooling | Reuse, session/transaction affinity, commit/rollback/statement release, concurrent ownership, session state | Exercise multiple-user/database contracts and additional reset semantics |
| Limits | Capacity, queue/release, wait timeout, canceled/disconnected waiters, repeated saturation | Exercise reserve and independent-pool fixtures; quantify backend counts/leaks over longer cycles |
| Transactions | Visibility, rollback, savepoints, failed-state recovery, disconnect cleanup, repeatable read, serializable conflict | Additional isolation anomalies and parameterized configured-isolation comparisons |
| Protocol | Simple/extended, raw named/unnamed prepare, reassignment, name isolation, eviction, binary/NULL, pipeline ordering | Pipeline error synchronization and more type/format combinations |
| Authentication/TLS | Optional positive/negative DSN and TLS/certificate cases | Run SCRAM/MD5 and TLS-required/verified-certificate fixtures; add authentication-reload change assertions |
| Lifecycle | Cancellation, optional idle/server-lifetime cases | Exercise lifecycle fixtures; add dedicated connection-establishment timeout scenarios |
| Recovery | Backend termination, interrupted non-replay, repeated reconnect, real PostgreSQL restart | Exercise network/pooler restart; add queued-client recovery and failure injection during sustained concurrency |
| Administration | Optional stats/reload/pause/drain/config-error interfaces | Validate accounting invariants, reload before/after values, and full rejection/drain policy on configured fixtures |
| Stress | Capacity-gated 10/100/1000 cases, churn, concurrent preparation, mixed blocked/short work, metrics separate from correctness | Run 100/1000-client-capable deployments, longer sustained trials, repeated-fault load and resource-growth budgets |
| CI | Workflow executes locked quality checks and repeated live conformance, uploads reports | Hosted workflow has not been executed in this workspace; configure branch protection as appropriate |

Capability manifests are reviewed claims, not permission to remove required
PostgreSQL semantics from a product's advertised contract. Missing fixtures must
remain visible skips and must be supplied before certifying those features.
Performance pass/fail requires an explicit reviewed budget; unrelated products
need not match PgBouncer's scheduling or administrative surface.
