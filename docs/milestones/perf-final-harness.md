# PERF-005/009 — expanded final harness (INCOMPLETE checkpoint)

Authority: [performance tasks](../tasks/pgbouncer-performance.md), PERF-005/009,
S01–S12/H11/H13/G06 in [FEATURE_COMPLETENESS.md](../../FEATURE_COMPLETENESS.md).
Neither task is complete at this checkpoint. The runtime candidate `14f077e`
passes 66 Rust tests and all 144 live cases; its performance acceptance is pending.

The pre-scheduler production binary is SHA256
`208e98670db9a76d3ce4ca2528cedda4b8e4f1a27e1b162fcfe747ae7efef9b3`,
source `487a7b4`. Its unchanged original comparison now contains all 30 arms,
three repetitions, 5-second warmups and 30-second measured windows, with zero
query errors. [Complete results](perf-scheduling-evidence/original-before/results.json),
[all repetitions/ranges/CV](perf-scheduling-evidence/original-before/summary.json),
[environment](perf-scheduling-evidence/original-before/environment.json),
[run log](perf-scheduling-evidence/original-before/run.log), raw archive and per-file
manifest preserve the full observation. The historical `c8997e8` baseline remains
unchanged. These targets retain the original unshared routing/passthrough setup;
shared-pool performance is a separate matrix.

Large variation occurred in some unrestricted runs. Unrelated compiler and
barman activity was observed on the shared host and is retained in the evidence.
The coordinator only drafted code/docs during this timing matrix; no owned build,
verification, profiler or archive job overlapped its timing windows. Every run is
retained. These are descriptive observations, not causal estimates or production
SLO evidence.

The additional workload harness now requires every requested frontend to finish
startup concurrently and verify full workload bytes plus a distinct client
identity. All measured transactions are logged. CTE/short lookups additionally
check returned id/client/length in pgbench; large-result full rows are checked
before and after load. Separate short/long distributions and maxima remain
visible. A negative offline case rejects missing client completion evidence.

`pool_mixed.py` adds all 96 before/after shared-pool duration-mix arms described
in the [scheduling plan](perf-scheduling-plan.md). `failover_load.py` defines all
24 real crash/promotion experiments, with every worker acknowledging writes on
both sides of promotion, synchronous pre-crash acknowledgements, independent
committed-row reconciliation and zero application SQL replay. It records proxy
CPU/RSS, per-node connection observations and explicitly unavailable nodes.
PgBouncer uses a documented external reload/reconnect adapter; Rust uses topology
discovery. This does not compare every possible upstream HA deployment.

The independent Memcheck audit expands its original four arms with four shared
session/transaction arms at 10/100 actual clients. All eight are mandatory;
shared idle physical connections are bounded and shutdown must remove every
workload connection. Default suppressions remain disabled. Instrumented audits
remain separate from native timing and native deadline regressions.

The expanded harnesses have **not yet completed their runtime matrices** at this
checkpoint. The normal installed hook verifies the staged snapshot with the
locked Nix toolchain, 66 Rust and 73 offline Python tests, Ruff, Astral ty, Clippy,
Prometheus/Grafana checks and complexity <=30. A hook failure prevents this commit.
Full candidate original/pool/workload/mix timing, normalized/profile attribution,
all eight Memcheck arms, all 24 faults, final kind/CNPG and README publication
remain required. No canonical checkbox is closed here.
