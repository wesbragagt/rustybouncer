# PERF-005/009 — final comparison and scheduling acceptance

Authority: [performance tasks](../tasks/pgbouncer-performance.md), scoped to
PERF-005/009 and the performance evidence for S06/S09–S12/Q08/H11/G06 in
[FEATURE_COMPLETENESS.md](../../FEATURE_COMPLETENESS.md). This milestone does
not close the broader canonical conformance or release checklist.

## Change and decision

Runtime `14f077e` replaces shared-pool broadcast notifications with bounded
per-ticket notifications. Only actual waiters allocate a signal. Capacity
handoff wakes the earliest eligible head; removing a head wakes its successor.
Only group heads arm reserve-delay timers. Topology invalidation still wakes
every affected waiter. FIFO eligibility, capacity limits, cancellation ownership,
reset, generation fencing, SQL purity and the pre-exposure retry boundary retain
their tested contracts. [Implementation and 144-case regression evidence](perf-scheduling-heads.md)
include 1,000 manually polled futures proving bounded handoff work and complete
invalidation/cleanup. No in-flight write or transaction replay was introduced.

The decision followed the full 24-arm pre-change attribution matrix and
[recorded review](perf-scheduling-plan.md). The final report retains 24 matching
after-change profiles, separate from native timings. At 1,000 clients, sampled
pool admission/handoff user-period shares change as follows:

| Placement | Mode | Before share | After share | Before/after CPU samples |
| --- | --- | ---: | ---: | ---: |
| Unrestricted | Session | 67.28% | 9.92% | 8,503 / 1,330 |
| Unrestricted | Transaction | 63.97% | 5.53% | 8,591 / 2,260 |
| One proxy CPU | Session | 62.97% | 10.63% | 1,504 / 527 |
| One proxy CPU | Transaction | 55.31% | 5.95% | 1,535 / 655 |

Runtime/lifecycle/other becomes the largest category, roughly 62–66% of the
after-change samples. Transaction session-affinity analysis is next at 15–18%;
cancellation control contributes roughly 6–13% across these treatments. These
are ranked, precedence-dependent callchain samples, not isolated marginal elapsed
costs. Kernel work is excluded. Off-CPU intervals include sleeping and runnable
delay; syscall amortization includes documented setup/probe work. Matching ELFs,
raw callchains, switch records and syscall traces are retained.

## Measurements and limits

The [complete comparison](../benchmarks/performance-final.md) links every raw arm,
summary, environment, exact commands, source snapshot, frozen binary and archive
checksum. All **654 normal timing arms** are retained with zero query errors:
30 original before +30 original after +30 normalized +144 pools before +144 pools
after +180 additional CTE/large/mixed +96 before/after shared-pool duration mixes.
All clients complete at the requested 10/100/1,000 concurrency, and independently
observed shared workload backends remain within the configured 16-backend ceiling.
The 64 profiling arms are additional instrumented observations, never timing
substitutes. Original `c8997e8` data remain immutable.

At 1,000 clients unrestricted Rust churn rises from about 4.0k to 12.7k TPS
(session) and 3.5k to 12.1k (transaction); p99 changes from 296 to 104ms and 367
to 118ms. Observed proxy CPU/transaction falls and RSS rises about 4 MiB. Rust
transaction persistent throughput rises from 4.1k to 21.6k TPS; PgBouncer remains
faster at 39.0k. Original 100-client routed throughput is 171.5k Q/s versus
PgBouncer session 155.0k, with worse p95/p99 and higher CPU/query on one CPU.
CTE, one-CPU and several low-concurrency costs remain. The measured 2,048-row
response favors Rust throughput/tails while consuming more proxy memory.

Every repetition, range, sample CV and matching-repetition ratio remains visible.
Control shifts and native regressions are included. Three shared-host repetitions
and time-separated batches cannot establish causal isolation, significance or a
production SLO. No invented performance budget gates completion. Session pooling
can queue a client until a session ends; maximum waits and minimum per-client
completions accompany aggregate percentiles. The README describes these costs
and the remaining protocol/security/operations limitations.

## Correctness, recovery and memory

The full native scope passes **66 Rust +73 offline Python tests**, zero failures,
errors, skips, ignored/filtered Rust tests, expected failures or unexpected
successes. Locked Nix verification includes Ruff, Astral ty, Clippy, formatting,
Prometheus/Grafana validation and cyclomatic complexity **28/30**. The full
**144-case live scope** passes: 13 queue, 55 protocol, 16 transaction, 16 session,
15 resource, seven failover, eight long-query, ten metrics and four buffering.

All **eight independent unsuppressed Memcheck arms** pass with 10/100 actual
clients across passthrough, unshared routing and both shared modes. There are
zero access, definite/indirect/possible leak or private-response findings. Shared
arms have declared VM-only queue/read allowances; native deadlines and client
counts are unchanged. Still-reachable process globals remain explicit. This is
not a claim that RSS plateaus. Four before/after slow-receiver experiments each
retain three barrier-synchronized cycles, exact small results, byte/deadline and
refund observations; database execution and held frontend delivery are distinct.

All **24 primary-crash/promotion experiments** pass: four poolers, 10/100 active
workers, three repetitions. Every worker acknowledges writes before the crash
and new writes after promotion, then continues for a five-second stable phase.
All **741,044 acknowledged writes** survive; **53 uncertain committed writes**
remain explicit. There are zero duplicate backend INSERT executions, application
SQL replays, corrupt/unsubmitted/cross-client rows or unexpected stable errors.
Expected outage errors are retained. PgBouncer uses an explicit external
host/reload/reconnect adapter; Rust uses role discovery. This does not rank all
PgBouncer HA deployments or establish replica freshness. The initial incomplete
journald statement capture is preserved as failed diagnostic evidence; owned
fixtures switched to blocking json-file logging before the complete fresh rerun.
See [fault evidence and correction](perf-fault-recovery.md).

The separate six-case kind/CNPG deployment gate and its complete result are linked
in the final comparison evidence. The suite uses a real four-node kind cluster,
the CNPG operator and three streaming PostgreSQL instances, including a stopped
primary worker, standby promotion, replica replacement and interrupted transaction
semantics. It is independent of the manual fault adapter above.

## Reproduction and handoff

Follow the [full protocol and commands](../benchmarks/performance-final.md).
The frozen before production SHA256 is
`208e98670db9a76d3ce4ca2528cedda4b8e4f1a27e1b162fcfe747ae7efef9b3`;
after is `103fb7c79cb53baebb629446b32481952fe3124a4cf3a7fc6e86bbd94c9b3e1f`.
Runtime source is `14f077e`; harness checkpoints `e4e9968`, `3dab4cb` and `9afd493`
record the expanded protocol, timing completion and corrected fault-log capture.
The final source/evidence commit precedes task registration and workmux merge;
the task ledger records that acceptance hash. All earlier explicitly incomplete
checkpoints remain historical records. Broader feature/release items remain open.
