# PERF-006 — INCOMPLETE aggregate-memory and cancellation checkpoint

Historical checkpoint; final combined acceptance is recorded in
[PERF-006 acceptance](perf-resources-acceptance.md). The open gates below describe
this checkpoint at `fe98354`, not the later acceptance.

Authority: performance tasks, PERF-006, and
canonical checklist, L01/L03–L07, Q01/Q04–Q08, S11.
This preserves an implemented and functionally verified candidate. PERF-006 is
**not complete**: classifier integration, combined verification, complete candidate
30-arm measurement and final acceptance remain pending. Shared backend pools and
cross-frontend lease handoff acceptance remain PERF-007/008 work.

## Implemented behavior

- A process-owned pool of 64-KiB slabs bounds leased plus cached bulk response
  capacity. Default footprint 64 MiB, configurable as a positive slab multiple.
  Inline 256-byte results keep small work progressing under bulk saturation.
  Slot/index metadata is bounded; freed slabs retain their footprint charge.
  Every result/attempt owns exclusive storage; drop returns slots, including abort.
- Preserve exact 16-MiB messages and 32-MiB encoded results/capacity. Promote only
  a 256-byte prefix, then assemble into slabs and deliver after validated Ready.
  No full large-result duplicate and no early Simple Query streaming. Assembly
  yields every 256 KiB; scheduling trade-offs remain PERF-005/009 measurements.
- Capacity exhaustion is nonretryable FATAL 53200 before result exposure. A
  dispatched write can remain outcome-unknown; it is never silently replayed.
- Routed clients receive virtual PID/random-secret credentials. An operation
  epoch covers route/setup, execution, retries and frontend delivery. Accepted
  cancellation stops retries. Native sends are fenced under the operation lock
  and awaited through EOF before a private synchronization barrier/next command.
  Preserve the original response future and actual I/T/E state while draining.
  Savepoint recovery retains prior transaction writes. Failed cleanup closes.
- Reader and worker are bounded futures under one session owner. EOF stays
  observable during execution; queue overflow closes explicitly. Every complete
  Q begins accounting at receipt; queued rejection has a terminal router_error.
  Partial-delivery cancellation closes rather than corrupting a frame or replaying.
- Separate delivery/cleanup deadlines default to 30s/5s. Up to 32 bounded reserve
  startup tasks recognize cancellation at full SQL admission capacity. Persistent
  shutdown registration avoids recreating a signal receiver each select loop.
  Completed JoinSet entries are drained before more work is admitted.

Memory ownership and [metric contract](../metrics.md)
document fixed input/metadata/runtime overhead and six additional unlabelled
families (123+29*N series). The footprint is not a total RSS limit. Opaque
passthrough does not gain virtual cancellation. Authentication/TLS remain open.

## Functional verification

**43 Rust +72 Python tests** and complete native checks pass: Ruff, Astral ty,
Clippy, Promtool, Grafana and complexity maximum 28/30. The final frozen slab
release binary passes **15 resource cases +7 failover +8 long-query +10 metrics
+4 buffering =44 live cases**, zero failures/errors/skips. Buffering repeats
full 10/100-client cycles three times; the resource suite provisions 10/100 mixed
clients and independently checks exact outputs, capacity and complete cleanup.

Positive cases cover replica/primary-write cancellation; native transaction abort
and savepoint recovery; cancellation while route selection or startup handshake
is held; wrong/stale secrets; 40 cancel/Ready races; SQL-capacity-full cancellation;
EOF with pipelined input; partial-delivery cancellation; delivery deadline; and
one-signal shutdown during 100-client churn. No unsupported rejection substitutes
for these positive cases. Unnamed/named extended protocol remains PERF-008 work.

Independent unsuppressed **Memcheck: all four 10/100-client passthrough/routed
large-result arms pass**, with zero invalid accesses, definite/indirect/possible
leaks or retained private response/pool storage at shutdown. Runtime globals stay
visible. Complete XML is retained; incomplete XML, checker errors and private
storage findings are rejected by tested checks. Four separate heaptrack large
profiles also complete with exact row oracles before/after controlled load.

## Repeated saturation and storage trade-offs

The contiguous-ledger candidate returned its live charges but RSS continued to
grow as large allocations were freed. Those complete 14+29-case development runs
are retained. Reusable slabs were chosen from that observation. All source
limits, test levels and cases remain; the final slab scope was rerun in full.

| Storage | Clients | Cycle | Lifetime VmHWM bytes | Bulk allocated after cleanup |
| --- | ---: | ---: | ---: | ---: |
| Contiguous ledger | 100 | 1 | 51,019,776 | not cached by application |
| Contiguous ledger | 100 | 2 | 74,346,496 | not cached by application |
| Contiguous ledger | 100 | 3 | 92,504,064 | not cached by application |
| Contiguous ledger | 10 | 1 | 23,408,640 | not cached by application |
| Contiguous ledger | 10 | 2 | 34,824,192 | not cached by application |
| Contiguous ledger | 10 | 3 | 55,943,168 | not cached by application |
| Slab pool | 100 | 1 | 44,007,424 | 33,554,432 |
| Slab pool | 100 | 2 | 45,117,440 | 33,554,432 |
| Slab pool | 100 | 3 | 46,489,600 | 33,554,432 |
| Slab pool | 10 | 1 | 41,095,168 | 33,554,432 |
| Slab pool | 10 | 2 | 41,426,944 | 33,554,432 |
| Slab pool | 10 | 3 | 41,467,904 | 33,554,432 |

All slab cycles hit the configured **32-MiB** footprint, retain exactly that
allocated/cached capacity afterward, and return leased bytes to zero. Small
queries complete while large slow consumers hold buffers. Each cycle records
raw small-query latencies, VmHWM/RSS and capacity observations. Three cycles are
not a general long-soak RSS proof; the exact storage bound follows pool ownership.
Slabs retain more memory after an isolated small burst than freeing everything;
under repeated load they reuse the bounded footprint rather than accumulating
large allocation churn. Small-body storage stays inline.

| Clients | Target | Allocation calls/Q | Allocated bytes/Q | Instrumented lifetime VmHWM bytes |
| ---: | --- | ---: | ---: | ---: |
| 10 | rustybouncer-session | 9.610 | 9,493.4 | 14,598,144 |
| 10 | rustybouncer-routed | 37.010 | 35,548.9 | 17,305,600 |
| 100 | rustybouncer-session | 2.834 | 5,149.4 | 17,911,808 |
| 100 | rustybouncer-routed | 29.047 | 34,326.3 | 45,682,688 |

Profiling amortizes startup, preflight and probes over ten 2,048-row queries
per client. It is not throughput evidence. Before/after comparison belongs with
[the wire study](perf-wire.md); final combined performance acceptance is pending.

## Evidence and remaining gates

Root: `/home/publicuser/dev/rustybouncer-evidence/perf-resources-20261108`.
Frozen production: `frozen/slab-production`, SHA256
`1a164ae334aaacc95a5df7e5afb7754901b1d12b988dcc922bdf0a64c63d78d9`.
[Source/binary hashes](perf-resources-evidence/slab-provenance.json),
[complete live tests](perf-resources-evidence/slab-resources.log.gz),
[full regression log](perf-resources-evidence/slab-regressions.log.gz),
[independent memory summary](perf-resources-evidence/memcheck-summary.json),
[heap summary](perf-resources-evidence/slab-large-summary.json),
[raw archive hashes](perf-resources-evidence/raw-archives.json),
[raw-file manifest](perf-resources-evidence/raw-manifest.json.gz).

```sh
nix develop --no-update-lock-file --command bash scripts/verify
export RUSTYBOUNCER_BIN=/home/publicuser/dev/rustybouncer-evidence/perf-resources-20261108/frozen/slab-production
export RESOURCE_EVIDENCE=/home/publicuser/dev/rustybouncer-evidence/perf-resources-20261108/slab-resources
nix develop --no-update-lock-file --command python3 tests/resources.py
# Complete failover.py, long_queries.py, metrics.py and buffering.py logs above.
nix develop --no-update-lock-file --command python3 benchmarks/memory_audit.py --output NEW_MEMORY --binary FROZEN_PROFILE
nix develop --no-update-lock-file --command python3 benchmarks/large_profile.py --output NEW_LARGE --binary FROZEN_PROFILE
```

The installed locked hook must pass normally for this INCOMPLETE checkpoint.
After workmux integration, rebuild/freeze combined code, repeat full positive and
regression scope and run the original 30-arm candidate matrix. Record actual
commits and commands before closing PERF-006. PERF-005/007–009 and all broad
canonical deployment/feature acceptance remain open. Earlier developmental
failures (a missing test import and lost malformed-frame disposition) remain
retained; neither is counted as passing acceptance evidence.
