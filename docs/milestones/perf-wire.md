# PERF-002 / PERF-003 — wire batching and private response ownership

Authority: [performance tasks](../tasks/pgbouncer-performance.md), PERF-002/003, and
[canonical checklist](../../FEATURE_COMPLETENESS.md), W01/W09–W11, Q04/Q07, S06/S11.
Runtime source checkpoint: `60d14df`. This acceptance report records completed
comparative verification; acceptance/evidence commit: `3ab8c6b`, registered in the task ledger.
Broader cancellation, aggregate limits, pooling and release acceptance remain open.

The vectored header/body writer handles partial writes, Pending, WriteZero and
disconnect. An 8-KiB backend reader and one bounded contiguous encoded response
replace per-message body vectors. Each attempt owns its response until validated
ReadyForQuery, then delivers it without a second full-result copy. Retry drops the
failed attempt and backend. Writes and transactions are never replayed. The
16-MiB message and 32-MiB encoded-result/capacity limits remain enforced.

## Complete comparisons

All **30 original + 30 equal-core + 16 attribution** arms passed with zero SQL
errors. All repetitions are retained. Shared-host ranges/CV are descriptive, not
confidence intervals or production SLOs. No new performance budget is imposed.
The historical target `rustybouncer-session` means opaque passthrough, not shared
backend pooling. Every mode still reaches 10/100 backends at its respective load.

| Placement | Clients | Target | Median Q/s | All Q/s repetitions | CV | Median p95 / p99 ms |
| --- | ---: | --- | ---: | --- | ---: | ---: |
| Unrestricted | 10 | direct | 229,049 | 229,049, 228,428, 229,264 | 0.2% | 0.067 / 0.103 |
| Unrestricted | 10 | pgbouncer-session | 152,835 | 131,154, 158,487, 152,835 | 9.8% | 0.086 / 0.153 |
| Unrestricted | 10 | pgbouncer-transaction | 155,834 | 150,851, 157,351, 155,834 | 2.2% | 0.082 / 0.121 |
| Unrestricted | 10 | rustybouncer-session | 162,254 | 162,254, 162,250, 162,490 | 0.1% | 0.087 / 0.139 |
| Unrestricted | 10 | rustybouncer-routed | 147,508 | 110,654, 147,508, 150,066 | 16.2% | 0.100 / 0.162 |
| Unrestricted | 100 | direct | 330,202 | 328,824, 330,202, 333,278 | 0.7% | 0.816 / 1.737 |
| Unrestricted | 100 | pgbouncer-session | 153,587 | 153,767, 148,030, 153,587 | 2.1% | 0.814 / 1.182 |
| Unrestricted | 100 | pgbouncer-transaction | 148,533 | 148,533, 146,550, 150,548 | 1.3% | 0.990 / 1.437 |
| Unrestricted | 100 | rustybouncer-session | 185,762 | 185,762, 156,219, 186,330 | 9.8% | 0.908 / 1.415 |
| Unrestricted | 100 | rustybouncer-routed | 175,106 | 175,106, 176,522, 174,903 | 0.5% | 0.986 / 1.485 |
| One proxy CPU | 10 | direct | 227,342 | 217,541, 231,591, 227,342 | 3.2% | 0.069 / 0.108 |
| One proxy CPU | 10 | pgbouncer-session | 159,459 | 159,459, 158,948, 159,926 | 0.3% | 0.079 / 0.115 |
| One proxy CPU | 10 | pgbouncer-transaction | 155,522 | 157,794, 155,393, 155,522 | 0.9% | 0.081 / 0.119 |
| One proxy CPU | 10 | rustybouncer-session | 153,288 | 152,483, 153,288, 154,003 | 0.5% | 0.084 / 0.117 |
| One proxy CPU | 10 | rustybouncer-routed | 131,817 | 131,910, 131,352, 131,817 | 0.2% | 0.097 / 0.118 |
| One proxy CPU | 100 | direct | 328,219 | 327,545, 328,219, 328,832 | 0.2% | 0.823 / 1.765 |
| One proxy CPU | 100 | pgbouncer-session | 154,033 | 155,447, 154,033, 153,999 | 0.5% | 0.790 / 1.110 |
| One proxy CPU | 100 | pgbouncer-transaction | 153,146 | 154,075, 149,495, 153,146 | 1.6% | 0.918 / 1.243 |
| One proxy CPU | 100 | rustybouncer-session | 151,099 | 151,372, 151,099, 149,823 | 0.5% | 0.808 / 1.138 |
| One proxy CPU | 100 | rustybouncer-routed | 127,726 | 128,469, 127,726, 125,686 | 1.1% | 0.933 / 1.297 |

Against [PERF-001](perf-001.md), routed medians change from 67,776→147,508
and 89,778→175,106 Q/s unrestricted at 10/100 clients. On one proxy CPU they
change from 28,483→131,817 and 27,580→127,726 Q/s. One-core proxy CPU/Q changes
from 16.00/16.63µs to 4.26/4.38µs. These are before/after whole-candidate results;
the reader, writer and representation changed together, so their individual
marginal contributions are not isolated. The 10-client unrestricted candidate
range includes 110,654 Q/s; it is not discarded as an outlier.

Routed p99 changes from 0.419→0.162ms and 2.882→1.485ms unrestricted,
0.526→0.118ms and 5.348→1.297ms on one proxy CPU. PgBouncer retains a
100-client tail advantage in this candidate. Scheduling/mixed/slow-receiver
acceptance remains PERF-005; this does not establish starvation freedom.

## Allocation and I/O trade-offs

| Clients | Routed socket writes/Q before → after | Reads/Q before → after | Small allocations/Q before → after | Small allocated bytes/Q before → after |
| ---: | ---: | ---: | ---: | ---: |
| 10 | 15.133 → 2.043 | 15.251 → 4.140 | 14.154 → 10.150 | 711.9 → 687.2 |
| 100 | 15.117 → 2.038 | 15.146 → 4.122 | 14.082 → 10.078 | 685.2 → 658.0 |

Counts include startup, preflight and probes, amortized over controlled queries;
profiling timing is excluded from throughput claims. Returned syscall bytes and
full decoded reports are retained. All eight CPU/off-CPU captures report zero
lost samples. Updated symbol taxonomy combines response reads and assembly and
recognizes the new wire module; compare old/new raw stacks with the same taxonomy.

Large-result study: all four original and four candidate arms, 2,048 rows ×128-byte
payload, ten queries/client, exact ordered-row oracles before and after load.

| Candidate | Clients | Mode | Allocations/Q | Allocated bytes/Q | Lifetime instrumented VmHWM bytes |
| --- | ---: | --- | ---: | ---: | ---: |
| large-original-hwm | 10 | rustybouncer-session | 9.91 | 7,718 | 14,426,112 |
| large-original-hwm | 10 | rustybouncer-routed | 2,134.29 | 568,234 | 18,927,616 |
| large-original-hwm | 100 | rustybouncer-session | 3.14 | 4,399 | 17,059,840 |
| large-original-hwm | 100 | rustybouncer-routed | 2,088.71 | 555,338 | 57,192,448 |
| large-candidate-hwm | 10 | rustybouncer-session | 10.06 | 7,729 | 14,249,984 |
| large-candidate-hwm | 10 | rustybouncer-routed | 41.88 | 1,076,364 | 18,882,560 |
| large-candidate-hwm | 100 | rustybouncer-session | 3.15 | 4,398 | 17,358,848 |
| large-candidate-hwm | 100 | rustybouncer-routed | 33.74 | 1,054,360 | 28,958,720 |

The contiguous representation greatly reduces allocation calls, but cumulative
allocated bytes increase about 1.9× for large results because growth counts each
reallocation. This trade-off is explicit. VmHWM is the instrumented process lifetime
high-water mark, including startup and profiler overhead; it is not a native RSS
budget. Both old/new large studies use the same strengthened collector.

## Ownership and conflicting profiler evidence

Three complete load/disconnect cycles at both 10 and 100 clients verify exact
per-client/cycle rows and zero PostgreSQL workload backends after each cycle.
RSS grows modestly across these short cycles; it does not prove an RSS plateau.
One of eight candidate heaptrack captures reports live response allocation frames
at shutdown. That finding is retained in [the diagnostic](perf-wire-evidence/heaptrack-leak-diagnostic.json)
and the unsuppressed raw leak report; its cause is unresolved. It is not erased
or presented as proof that the allocator returned all pages to the OS.

An independent full **Valgrind Memcheck 3.27.1** run checks all four 10/100-client
passthrough/routed large-result arms without suppressions. All workload backends
close before process shutdown. All four pass with zero invalid accesses, definite,
indirect or possible leaks, and zero retained private response allocations,
including still-reachable allocations with wire response stacks. Other reachable
Rust/runtime globals remain visible in [complete results](perf-wire-evidence/memcheck-results.json).
This independent allocator audit, private attempt ownership and exact lifecycle
checks support response-buffer cleanup acceptance. The heaptrack discrepancy
remains a measurement limitation. No global RSS/aggregate-memory claim is made.

Memcheck uses predeclared instrumentation-only 10s probe/30s startup/route budgets;
native regression deadlines remain unchanged. A nonzero checker exit, incomplete
XML or private response finding fails acceptance. Four checker unit tests reject
incomplete reports, invalid accesses, lost blocks and reachable private buffers.
The heap collector now uses `--record-only` and requires clean profiler exits;
prior optional heaptrack GUI library failures are retained as diagnostics.

## Verification and reproduction

Full native verification passes **31 Rust +71 offline Python tests**, Ruff, Astral
ty, Clippy, Promtool, Grafana and complexity ≤30. The complete final-hash live
scope passes **7 failover +8 long-query +10 metrics +4 buffering =29 cases**,
zero failures/errors/skips. The latest buffering tests repeat three complete
load/disconnect cycles per client count, with five multirow query cycles each.
Wire unit tests cover partial/Pending writes, NULL/binary/error/pipeline framing,
truncation, exact size limits and no pre-Ready exposure. All original failure
diagnostics remain retained; none substitute for accepted complete reruns.

Frozen production SHA256: `7d6c1f01269e5e223cf4bc9fb87b95ed765f11dd156091c12caf0072b5813940`.
Separate frame-pointer/debug binary: `25daa96bf8f0d39dd62513d0bd8fbb210dcb8032a0d98e618a77834b4c511372`.
Exact evidence root: `/home/publicuser/dev/rustybouncer-evidence/perf-wire-20261108`.
[Interactive comparison](../benchmarks/perf-wire/chart.html),
[All timing/profile summaries](perf-wire-evidence/summary.json),
[large study](perf-wire-evidence/large-summary.json),
[raw archive hashes](perf-wire-evidence/raw-archives.json),
[raw-file inventory](perf-wire-evidence/raw-manifest.json.gz),
[native checks](perf-wire-evidence/acceptance-native.log.gz),
[runtime checks](perf-wire-evidence/soak-regressions.log.gz).

```sh
nix develop --no-update-lock-file --command bash scripts/verify
nix develop --no-update-lock-file --command python3 benchmarks/compare.py --output NEW_ORIGINAL --binary FROZEN_PRODUCTION
nix develop --no-update-lock-file --command python3 benchmarks/performance_profile.py --output NEW_NORMALIZED --binary FROZEN_PRODUCTION --profile-binary FROZEN_PROFILE
nix develop --no-update-lock-file --command python3 benchmarks/large_profile.py --output NEW_LARGE --binary FROZEN_PROFILE
nix develop --no-update-lock-file --command python3 benchmarks/memory_audit.py --output NEW_MEMCHECK --binary FROZEN_PROFILE
# Run each old/new large study separately with its frozen source binary.
# Full runtime environment/commands are preserved in soak-final/run.log.
```

The installed locked Nix hook passed normally for `3ab8c6b` (31 Rust +71 Python;
zero failures/errors/skips). [Hook log](perf-wire-evidence/acceptance-hook.log.gz).
This closes only PERF-002/003 acceptance.
PERF-004–009 and the canonical deployment/workload matrix remain open.
