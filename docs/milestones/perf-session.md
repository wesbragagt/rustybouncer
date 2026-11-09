# PERF-007 — verified shared session reuse

Authority: [performance tasks](../tasks/pgbouncer-performance.md), PERF-007;
[canonical checklist](../../FEATURE_COMPLETENESS.md), P02/P03/P08–P10, C07 and D04.
This is the routed simple-query session-reuse slice, not full pooler/release
conformance. Transaction/protocol/queue acceptance remains PERF-008. The reviewed
[ownership design](../design/backend-pools.md) was committed before implementation
(`2f33b63`, with lazy-startup/protocol refinements `27af1cf`/`b9ed782`).

## Behavior

`pool_mode = "session"` requires routed trust-authenticated mode. It assigns an
exclusive primary backend on first SQL and retains affinity through frontend
close. The default `none` retains the previous unshared routed caches and replica
rotation. Opaque passthrough remains unchanged. Transaction mode is explicitly
pending PERF-008, not claimed supported by a passing rejection.

The process-owned key is node, role/generation and exact original startup packet,
including user, database, application name and options. Client SQL never uses
probe credentials. Trusted baseline startup metadata is shared only within that
exact key; every frontend gets fresh virtual cancellation credentials. Cache
entries are pruned with their physical reservations and waiters, not retained as
an unbounded history. Lazy assignment allows idle frontends to complete startup
without owning one backend each. Different startup bytes can reduce reuse.

Reset is owned by the frontend task: rollback T/E, DISCARD ALL, verify actual I
and baseline ParameterStatus, reinitialize replica protection where applicable,
then return. Reset failure drops the stream without replaying frontend work or
rewriting a previously known outcome. Unexpected idle bytes/EOF, retirement and
stale generations discard. Idle permits hold weak manager references; active
leases separately retain ownership. Unit tests verify the manager and real peer
socket disappear on final drop, avoiding a strong idle ownership cycle.

Physical reservations include connecting, active, resetting and idle ownership.
Per-key/global bounds, FIFO tickets, bounded admission, idle eviction and reserve
admission are implemented by the common manager; full transaction/queue/1,000-client
acceptance is deliberately retained for PERF-008. The frontend waiter bound is
`max_clients`; there is no detached reset queue or lock held over network await.
Session-mode initial metadata bootstrap EOF handling remains bounded by startup
wait; immediate queue-removal acceptance is part of PERF-008.

## Positive evidence

Frozen release SHA256:
`c3b6bb8e7efec3cc615d48b1229e5b1b4c8c0ef71059c8e98232e61cacce39e7`.
[Source hashes](perf-session-evidence/provenance.json) bind the tested runtime.
All **16 dedicated live cases +15 resource +7 failover +8 long-query +10 metrics
+4 buffering =60 cases** pass with zero failures/errors/skips. The 44 regressions
run the existing default routing contract; session cases independently test the
new mode's primary-affinity contract. No required behavior is deselected to pass.

The dedicated cases observe real PostgreSQL PIDs and state for:

- Session affinity, two concurrent exclusive owners and reuse after disconnect.
- Temporary tables, search_path/application_name, SQL PREPARE, advisory locks and
  LISTEN reset; open/aborted transaction rollback and subsequent healthy queries.
- Separate users/databases/startup options, original baseline defaults and session
  authorization restoration. Permission-denied checks prove the probe identity
  is not being used for client SQL.
- Old virtual cancellation credentials cannot cancel the next owner of the same
  physical PID; valid current cancellation works and preserves the session.
- Transport failure during reset, killed idle connections, idle/lifetime retirement
  and generation invalidation without restarting the pooler.
- A real primary plus two streaming replicas: pause primary, promote a standby,
  reject the interrupted original transaction without replay, independently verify
  no uncommitted row, then reuse only the new primary's backend.
- Saturated two-backend session capacity waits and hands off exclusive ownership.

The declared ownership/churn experiments retain independent cleanup barriers:
**50 sequential clients reuse one PID**, versus **50 distinct PIDs without pooling**.
Each executes two verified queries. **100 frontends complete startup while one
warm physical backend is held**, with 100 distinct virtual identities; assigning
SQL to two sessions then uses two distinct backends. These prove when backend
process savings occur, not a throughput/tail improvement. Timings include cleanup
barriers and are explicitly not presented as a throughput benchmark.

Native verification passes **53 Rust +72 Python tests**, Ruff, Astral ty, strict
Clippy, formatting, Promtool/Grafana and complexity **30/30**. The installed locked
hook must pass normally on this implementation/evidence commit. The task ledger
records the resulting hash in the coordinator's registration commit.

## Reproduction and limits

Linux x86_64; locked Nix toolchain; owned Docker PostgreSQL 16, including physical
streaming replicas for promotion. Paths must be fresh. No credentials or SQL values
are metric labels. Inventory is now **154+29*N** fixed series; shared idle streams
are included in the backend gauge. Separate active/idle/queue phase metrics remain
PERF-008 work. See [metric contract](../metrics.md).

```sh
nix develop --no-update-lock-file --command bash scripts/verify
nix develop --no-update-lock-file --command cargo build --release --locked --offline
export RUSTYBOUNCER_BIN=/home/publicuser/dev/rustybouncer-evidence/perf-pools-20261109/session-frozen/production
export SESSION_POOL_EVIDENCE=NEW_SESSION_DIRECTORY
nix develop --no-update-lock-file --command python3 tests/session_pool.py
export RESOURCE_EVIDENCE=NEW_RESOURCE_DIRECTORY
nix develop --no-update-lock-file --command python3 tests/resources.py
nix develop --no-update-lock-file --command python3 tests/failover.py
nix develop --no-update-lock-file --command python3 tests/long_queries.py
export METRICS_EVIDENCE=NEW_METRICS_DIRECTORY
nix develop --no-update-lock-file --command python3 tests/metrics.py
export BUFFER_EVIDENCE=NEW_BUFFER_DIRECTORY
nix develop --no-update-lock-file --command python3 tests/buffering.py
```

Full [native verification](perf-session-evidence/session-native-second.log),
[16 session cases](perf-session-evidence/session-promotion-final.log),
[complete regression chain](perf-session-evidence/session-live-final.log),
[archive hashes](perf-session-evidence/archives.json),
[per-file SHA256 manifest](perf-session-evidence/manifest.json.gz),
[pooled churn](perf-session-evidence/pooled-churn.json),
[unpooled control](perf-session-evidence/unpooled-churn.json) and
[idle observation](perf-session-evidence/idle-observation.json) are committed.
The earlier 14/15-case development runs remain in the external evidence directory;
the final required dedicated inventory is all 16 cases.

The original persistent comparison remains unchanged in historical evidence.
PERF-009 owns the next complete 30-arm measurement of this frozen candidate,
equal-core and new pooled comparisons; no new performance claim is made here.
Authentication/TLS, COPY, broad extended protocol and wider canonical deployment
acceptance are not established by this milestone. PERF-005/008/009 remain open.
