# PostgreSQL pooler black-box conformance

The authoritative target is [FEATURE_COMPLETENESS.md](../FEATURE_COMPLETENESS.md).
The current baseline runner still reports capability/fixture skips. These are
diagnostic results, not milestone or release acceptance under the zero-skip
policy; item R12 tracks removing that completion path. Missing required features
and fixtures must be implemented/provisioned rather than hidden or excluded.

Run common assertions against PostgreSQL-compatible endpoints. The suite imports
no pooler source, starts no processes in core tests, and uses no runtime internals.
PgBouncer is a reference deployment, not a mandatory behavioral oracle.

## Existing endpoint: no process control

Use Python 3.11+ with Psycopg 3.2+, or enter the pinned `nix develop` environment:

```sh
# Without Nix, in your chosen virtual environment:
python -m pip install -r conformance/requirements.txt

# Supply credentials/TLS through libpq DSNs, service files, or your secret manager.
export POOLER_DSN='service=pooler-conformance'
export POSTGRES_DSN='service=postgres-conformance'
python -m conformance --manifest conformance/manifests/minimal.json \
  --json /tmp/conformance/results.json --junit /tmp/conformance/results.xml
```

Copy/edit a manifest to declare the deployment's contract. There is no implicit
localhost, credential, database, TLS policy, pool size, or deployment mechanism.
`POOLER_DSN` targets the pooler; `POSTGRES_DSN` targets its writable PostgreSQL
backend. The verification role needs schema creation rights. Each test creates a
unique schema and fully qualified table names, and removes them during cleanup.
Use a dedicated test database/user/pool: saturation tests intentionally consume
its declared capacity. Limits are meaningful only when other workloads do not
compete for that capacity.

The **minimal** profile runs basic protocol, CTE, concurrency and churn checks,
not a certification of backend pooling. Sample PgBouncer session/transaction/
statement profiles and the current rustybouncer routing profile are provided.
Only declare capabilities that the exact deployment/configuration promises.
An unknown capability, invalid limit, or missing required endpoint is an error;
unsupported features are explicit skips. Missing optional credentials/adapters or
insufficient declared capacity also produce skips with distinct explanatory text.
A skipped fixture does not establish that a declared feature works.

```sh
# List assertions without connecting to any endpoint:
python -m conformance --manifest conformance/manifests/pgbouncer-transaction.json --list
# Run a single test or repeat all cases independently:
python -m conformance --manifest /path/to/contract.json --match test_commit_releases_capacity
python -m conformance --manifest /path/to/contract.json --repeat 3
```

## Contract and fixture configuration

A manifest has `name`, `mode`, boolean `features`, `settings`, and optional `actions`.
Supported modes are session, transaction, statement, and routing. Classification
is explicit; tests never infer a product from a version banner. `config.py` lists
recognized capability names and validates configuration. Omitted capabilities are
unsupported, rather than assumed PgBouncer-compatible.

Important settings:

- `pool_size`, `max_clients`: actual configured limits, not suggested sizes.
- `capacity`: maximum client concurrency this test deployment can support. The
  10/100/1000-client cases skip levels above it; they never silently reduce a level.
- `operation_timeout`, `connect_timeout`: independent client-side deadlines.
- `recovery_timeout`: bounded polling budget after a fault.
- `observation_window`: bounded negative observation when asserting queueing.
- `poll_interval`, `timing_tolerance`, `wait_timeout`: explicit timing policy.
- `cycles`, `queries_per_client`: repeated recovery/saturation and concurrency work.
- `prepared_capacity`: prepared-cache size for the eviction scenario.
- `minimum_queries_per_second`: optional explicit performance budget. Without one,
  performance metrics are descriptive; correctness determines pass/fail.

Transactions are never automatically retried. Recovery polling explicitly opens
fresh clients and checks eventual availability. Every SQL operation has a bounded
client deadline; deadline disposal stops its driver task before a new socket can
reuse the old file descriptor. Ordinary query assertions do not retry errors.

Optional endpoint fixtures are environment-owned:

| Variable | Purpose |
| --- | --- |
| `POOLER_ALT_USER_DSN` | Authorized second user |
| `POOLER_ALT_DATABASE_DSN` | Independent second database/pool |
| `POOLER_BAD_AUTH_DSN` | Credentials that must be rejected |
| `POOLER_DENIED_DSN` | Unauthorized user/database combination |
| `POOLER_VERIFIED_TLS_DSN` | TLS endpoint configured with certificate/hostname verification |
| `POOLER_BAD_CERT_DSN` | Certificate/hostname configuration that must fail |
| `POOLER_ADMIN_DSN` | Optional documented administration interface |

Core tests need no admin endpoint or PgBouncer commands. Optional admin-statistics
queries come from `settings.stats_query`; the PgBouncer profile uses `SHOW POOLS`.
The supplied authentication fixtures use trust for disposable local tests; they do
not validate SCRAM/MD5 or TLS. To validate those, supply deployments configured for
the desired method and the positive/negative DSNs above. DSN passwords are redacted
from generated reports; other adapter output must also avoid printing secrets.

## Optional deployment/fault adapters

Adapters own deployment mechanics, not SQL assertions. They can invoke local
commands, Docker, `kubectl`, or an environment-specific program. An already-running
remote pooler requires none of them.

Manifest `actions` map names to **argv arrays**, never implicit shell commands.
Fault/configuration actions require `CONFORMANCE_ALLOW_FAULTS=1`. Names currently
used are `postgres_restart`, `pooler_restart`, `network_cut`, `network_restore`,
`reload`, `pause`, `resume`, `drain`, `pooler_start`, and `invalid_config`.
Commands must return within `recovery_timeout`; daemons must detach and redirect
inherited stdout/stderr. Paired restore/start actions should be idempotent because
cleanup may repeat them. Use reversible changes on isolated test deployments.
`invalid_config` must return nonzero; `invalid_config_error_pattern` is required
to identify the documented rejection. Other actions must return zero on success.

The included optional adapter creates isolated PostgreSQL, PgBouncer and
rustybouncer deployments and runs identical assertions with different manifests:

```sh
nix develop
cargo build --locked
python -m conformance.adapters.local --output /tmp/conformance-local --repeat 2
python -m conformance.adapters.local --output /tmp/conformance-docker \
  --docker-postgres --docker-poolers --repeat 2
python -m conformance.adapters.local --output /tmp/conformance-session --reference-mode session
python -m conformance.adapters.local --output /tmp/conformance-statement --reference-mode statement
```

Output directories must not exist. Native mode uses `initdb`/`pg_ctl`; With `--docker-postgres`, PostgreSQL runs in Docker. Adding
`--docker-poolers` runs both poolers in Docker too (Linux/Nix adapter), using
the same binaries and a read-only Nix store mount. The hybrid variant without
that flag also works. This is not Kubernetes or independent-OS certification. Both modes bind test ports to loopback and clean up owned processes,
containers, anonymous volumes and temporary data. The adapter controls configuration
and restart; neither test assertions nor the core runner depend on it.

## Coverage and evidence

The current cases cover:

- Session affinity; transaction/statement release; backend reuse; distinct
  simultaneous transaction backends; documented session state.
- Capacity exhaustion, queued completion, max-client rejection, wait timeout,
  disconnected/canceled waiters, repeated saturation, reserve and independent pools.
- Commit/rollback, savepoints, visibility, repeatable read, serializable conflicts,
  transaction-local state and disconnect rollback.
- Simple/extended queries, named/unnamed protocol preparation, binary/NULL data,
  forced backend reassignment, prepared-name isolation, eviction and pipelining.
  SQL PREPARE is a separate capability and case; no driver statement cache is used
  for the explicit protocol preparation assertions.
- Optional credential/TLS scenarios, cancellation, idle-client/server retirement,
  server lifetime, administration, reload, pause/resume and draining.
- Backend termination, interrupted transaction non-replay, repeated recovery,
  optional PostgreSQL/pooler restarts and network interruption.
- Concurrent clients, churn, concurrent prepared execution, and a controlled
  blocked transaction alongside short work. Tokens detect response cross-talk.

Held transactions and barriers establish ordering. The mixed-duration case uses
an advisory lock and `pg_blocking_pids()`, not an arbitrary query sleep. Polling and
negative queue/idle observations still use explicit timing budgets. Capacity,
backend PID/address/port, errors/SQLSTATEs, durations and per-case evidence are
captured where relevant. JSON and JUnit include expected/actual assertion details
and explicit skip reasons. Concurrency cases record throughput and latency
percentiles; their tiny closed-loop runs are not production capacity benchmarks.

See [validation report](../docs/conformance/report.md) for measured results and
[acceptance checklist](../docs/conformance/acceptance.md) for remaining work.
The checklist is a living coverage contract: a green capability-adjusted run does
not mean every optional feature, operating environment, or timing race was tested.

References: [upstream PgBouncer tests](https://github.com/pgbouncer/pgbouncer/tree/master/test),
[upstream testing guide](https://github.com/pgbouncer/pgbouncer/blob/master/test/README.md),
[Psycopg cursor/protocol choices](https://www.psycopg.org/psycopg3/docs/advanced/cursors.html).

Queued pool-exhaustion scenarios require transaction support to hold known
backends; max-client admission is the independent `client_limits` capability.
Server-count assertions use each test's unique startup `application_name` tag.

For Kubernetes or remote environments, supply reachable service DSNs (or optional
port forwarding) and environment-owned argv adapters; core assertions do not
import Kubernetes/Docker libraries or require access to deployment processes.

Standard session/transaction/statement mode declarations must enable their
required affinity/release, transaction and backend-reuse guarantees. A manifest
cannot turn those required assertions off while retaining the pooling-mode claim.
The `routing` profile makes no shared-pooling claim.
