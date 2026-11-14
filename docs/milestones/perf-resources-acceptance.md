# PERF-006 — combined resource acceptance

Authority: performance tasks, PERF-006;
canonical checklist, L01/L03–L07,
Q01/Q04–Q08 and S11. This closes the scoped aggregate-result-memory task.
Shared backend ownership/lease handoff remains PERF-007/008. It does not close
broader canonical deployment, protocol or cancellation acceptance.

Implementation checkpoint `fe98354` and classifier integration `9682be2` were
merged through workmux with the installed hook and locked pre-merge verification.
The combined binary is built from `9682be20c3898c01b049a5d39f4e0ce87924682a`.
The accepted production SHA256 is
`98c402e1bd94d28396c4330c825b2023e241f8face4ad9edc9c71d7b463bb44c`;
profile SHA256 is
`4a008b7248aad918e519d932dedb54384e4cf63ce1cc12de276292057b8f94f8`.
[Provenance](perf-resources-evidence/combined/provenance.json) records runtime
source hashes. [Checkpoint evidence](perf-resources.md) describes the slab
ownership, aggregate bound, cancellation fencing and before/after storage study.

## Accepted verification

- Full locked native verification: **46 Rust +72 Python tests**, Ruff, Astral ty,
  Clippy, Promtool, Grafana and complexity ≤30. The installed hook passed normally
  for integration; workmux then passed the locked check before merging.
- Frozen combined release: **15 resource +7 failover +8 long-query +10 metrics
  +4 buffering cases**, zero failures/errors/skips. Both resource and buffering
  suites exercise complete 10/100-client workloads; buffering repeats three cycles.
- Independent unsuppressed Memcheck: **all four 10/100-client passthrough/routed
  arms**, exact result oracles, clean shutdown, no invalid access, definite,
  indirect or possible leaks or retained private response/slab storage.
- Original unchanged five-target comparison: **all 30 arms**, three repetitions
  at 10/100 clients, zero query errors. No source build or heavy verification ran
  concurrently with this final timing matrix. Shared-host scheduling remains a
  limitation; timing is separate from instrumented profiles/memory checks.

| Target | Clients | Median Q/s | Median p99 ms |
| --- | ---: | ---: | ---: |
| PgBouncer session | 10 | 151,590 | 0.149 |
| PgBouncer transaction | 10 | 149,923 | 0.147 |
| Rustybouncer passthrough | 10 | 154,690 | 0.165 |
| Rustybouncer routed | 10 | 139,690 | 0.161 |
| PgBouncer session | 100 | 148,194 | 1.344 |
| PgBouncer transaction | 100 | 147,315 | 1.402 |
| Rustybouncer passthrough | 100 | 185,225 | 1.457 |
| Rustybouncer routed | 100 | 166,127 | 1.711 |

[All repetitions and variation](perf-resources-evidence/combined/comparison-summary.json)
retain min/max and sample CV, including direct PostgreSQL. This table is scoped
candidate timing, not a claim that cancellation or slab accounting improves speed.
It shows the throughput/tail cost of the complete combined candidate. Unrestricted
results do not establish per-core efficiency. Equal-core/extended workloads and
final feature comparisons remain PERF-005/009.

## Storage and cancellation contract

The default 64-MiB bulk footprint includes leased and cached 64-KiB slabs; the
32-MiB test configuration stays within exactly that configured capacity in all
three 10/100-client saturation cycles. Leased charges return to zero after
success, rejection, disconnect, canceled execution/delivery, retry and abort.
Small inline results keep progressing while bulk storage is exhausted. Per-query
16-MiB message /32-MiB encoded-result boundaries remain. Capacity rejection is
FATAL 53200 before exposure, with no dispatched-write replay.

The ownership bound excludes documented fixed frontend/inline/index/task/socket
and allocator overhead; it is not a total process RSS limit. See
result-memory.md. The retained cache uses more
memory after an isolated burst; repeated slab cycles reduce large-allocation
churn compared with the contiguous-ledger development candidate. Three cycles
are not proof of an indefinitely flat RSS plateau.

Cancellation is positively tested for replica reads, primary writes, transactions,
savepoints, route/setup waits, delivery, full SQL capacity, and wrong/stale
credentials. Accepted native sends drain before a private synchronization barrier;
no detached operation or hidden replay is used. Simple Query remains buffered
until validated ReadyForQuery. Extended protocol lease/cancellation acceptance
is part of PERF-008 and is not claimed here.

## Reproduction and retained evidence

Environment is Linux x86_64, the repository's locked Nix toolchain and owned Docker
PostgreSQL fixtures. [Comparison environment](perf-resources-evidence/combined/comparison-environment.json)
records tool versions, host and configuration. All owned fixtures are destroyed.
The runtime tests default to no shared backend pooling, matching this milestone.

```sh
nix develop --no-update-lock-file --command bash scripts/verify
export RUSTYBOUNCER_BIN=/home/publicuser/dev/rustybouncer-evidence/perf-resources-20261108/combined/production
export RESOURCE_EVIDENCE=/home/publicuser/dev/rustybouncer-evidence/perf-resources-20261108/combined/resources
nix develop --no-update-lock-file --command python3 tests/resources.py
nix develop --no-update-lock-file --command python3 tests/failover.py
nix develop --no-update-lock-file --command python3 tests/long_queries.py
export METRICS_EVIDENCE=/home/publicuser/dev/rustybouncer-evidence/perf-resources-20261108/combined/metrics
nix develop --no-update-lock-file --command python3 tests/metrics.py
export BUFFER_EVIDENCE=/home/publicuser/dev/rustybouncer-evidence/perf-resources-20261108/combined/buffering
nix develop --no-update-lock-file --command python3 tests/buffering.py
nix develop --no-update-lock-file --command python3 benchmarks/memory_audit.py --output NEW_MEMORY_DIRECTORY --binary /home/publicuser/dev/rustybouncer-evidence/perf-resources-20261108/combined/profile
nix develop --no-update-lock-file --command python3 benchmarks/compare.py --output NEW_COMPARISON_DIRECTORY --binary "$RUSTYBOUNCER_BIN"
```

Output directories must be fresh. The recorded original directories above are
retained and must not be overwritten. Full [resource log](perf-resources-evidence/combined/resources.log),
[29-case regression log](perf-resources-evidence/combined/regressions.log),
[Memcheck log](perf-resources-evidence/combined/memcheck.log),
[30-arm comparison log](perf-resources-evidence/combined/original-comparison.log),
[integration hook](perf-resources-evidence/combined/combined-merge-hook.log) and
[workmux merge](perf-resources-evidence/combined/workmux-merge-retry.log) are retained.
[Archive hashes](perf-resources-evidence/combined/archives.json) and the
[per-file SHA256 manifest](perf-resources-evidence/combined/manifest.json.gz)
cover complete configs, scrapes, raw latency logs, memory XML and results.
Earlier failures remain historical diagnostics; none count as passing acceptance.

PERF-005/007/008/009 and all broader canonical acceptance remain open. The normal
installed hook verifies this acceptance commit; the task ledger records its hash
in the following registration commit.
