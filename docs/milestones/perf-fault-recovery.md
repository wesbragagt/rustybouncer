# PERF-009 — complete crash/promotion scope; final publication still open

Authority: [performance tasks](../tasks/pgbouncer-performance.md), PERF-009,
S09/G06/H11/R11 in [FEATURE_COMPLETENESS.md](../../FEATURE_COMPLETENESS.md).
This records the fault-validation slice, not completion of PERF-005/009 or broad
release conformance. Runtime remains `14f077e`, frozen production SHA256
`103fb7c79cb53baebb629446b32481952fe3124a4cf3a7fc6e86bbd94c9b3e1f`.

All **24 complete crash/promotion experiments** pass: both PgBouncer pool modes
and both Rust shared modes, 10/100 actual workers, three repetitions. The primary
is killed with Docker SIGKILL and a physical standby promoted. Before the crash,
both standbys are verified synchronous quorum members. Every worker acknowledges
a write before the fault, then acknowledges a new write after promotion and
continues through the stable phase. Unique operations are never resent.

Independent checks find zero lost/corrupted acknowledged rows, unsubmitted rows,
wrong-client results, duplicate backend INSERT executions, unexpected stable-phase
operation errors or application SQL replays. Full database statement logs verify
each acknowledged/committed INSERT appears exactly once; uncertain committed rows
remain explicit. Expected connection/query failures during the controlled outage
are retained as fault observations, not counted as failed or skipped test cases.

The six runs per target observe all-worker write-recovery ranges of:

| Configured target | Seconds, min–max |
| --- | ---: |
| PgBouncer session | 1.134–1.271 |
| PgBouncer transaction | 1.099–1.330 |
| Rust shared session | 0.584–0.881 |
| Rust shared transaction | 0.731–1.018 |

These include the common crash/promotion action plus each configured recovery
path. PgBouncer uses an explicit host rewrite + RELOAD/RECONNECT adapter; Rust
uses discovery without restart/reload/endpoint rewrite. This does not rank every
possible PgBouncer HA design. Worker rates differ, so raw outage error counts are
not normalized production error rates. Reads return distinct literals; the manual
fixture does not reparent the remaining standby or establish replica freshness.

## Capture failure and complete rerun

The first 10-worker PgBouncer session attempt failed the statement-evidence oracle.
Database row checks passed, but captured logs stopped after the initial burst:
34,718 acknowledged writes versus 5,609 matched execution records. The host's
Docker logging driver was journald, consistent with rate-limited/incomplete log
capture. This failed attempt is diagnostic evidence, not a passing result.

The fixture now accepts optional container arguments with an empty default;
existing non-fault adapters keep identical arguments. Only the owned fault
containers select **blocking json-file** logging, with their actual Docker
LogConfig inspected and retained. No host daemon setting, workload level,
transaction count, oracle or timeout threshold was weakened. The whole 24-case
matrix ran again from a fresh directory and passed.

[All 24 results](perf-scheduling-evidence/recovery/results.json),
[validated summary](perf-scheduling-evidence/recovery/fault-summary.json),
[environment](perf-scheduling-evidence/recovery/environment.json),
[complete passing log](perf-scheduling-evidence/recovery/failover-load.log),
[failed capture log](perf-scheduling-evidence/recovery/diagnostic-fault-journald.log),
and [full verification](perf-scheduling-evidence/recovery/logging-fix-native.log)
are retained. Complete operation/statement logs, row snapshots, configurations,
resource observations and failed-attempt artifacts remain in the coordinator's
`perf-scheduler-20261109/{failover-load,diagnostic-fault-journald}` directories
until final complete archiving.

```sh
nix develop --no-update-lock-file --command python3 benchmarks/failover_load.py \
  --binary /absolute/path/to/frozen-production --output /absolute/path/to/new-fault-run
```

The full native gate passes 66 Rust +73 offline Python tests, Ruff, Astral ty,
Clippy, Prometheus/Grafana and complexity <=30, with zero failures/errors/skips or
ignored/filtered Rust tests. Focused checks also passed after adding actual
container-log inspection. The installed hook verifies the exact staged snapshot
normally before this milestone commit exists. Source/harness retention now
includes the fixture modules needed to reproduce the logging adapter.

All 654 normal timing arms, 64 separate profiles and eight unsuppressed Memcheck
arms have also finished successfully; they remain separate observations. Final
six-case kind/CNPG validation, complete report/archive/visuals, README publication
and workmux acceptance still keep PERF-005/009 open. Canonical release criteria
remain open independently.
