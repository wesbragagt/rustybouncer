# PERF-005/009 — complete timing checkpoint (INCOMPLETE acceptance)

Authority: performance tasks, PERF-005/009,
S06/S09–S12/H11/G06 in the local feature checklist.
Both performance tasks remain open. This checkpoint freezes completed normal
measurements and the remaining validation/reporting harness, not final delivery.

Runtime source `14f077e`, production SHA256
`103fb7c79cb53baebb629446b32481952fe3124a4cf3a7fc6e86bbd94c9b3e1f`,
passes all 144 live cases retained in the [runtime checkpoint](perf-scheduling-heads.md).
No Rust runtime source changed afterward. The pre-scheduler source is `487a7b4`,
production `208e98670db9a76d3ce4ca2528cedda4b8e4f1a27e1b162fcfe747ae7efef9b3`.

All four candidate timing matrices now complete with **zero query errors**:

- [144 small-pool arms](perf-scheduling-evidence/timings/pools-after/results.json),
  all 10/100/1,000 actual clients finishing, independent observed backends <=16.
- [30 unchanged original lookup arms](perf-scheduling-evidence/timings/original-after/results.json).
- [180 CTE/large/mixed arms](perf-scheduling-evidence/timings/workloads-after/results.json),
  every requested client represented in the complete measured logs.
- [96 before/after shared-pool duration-mix arms](perf-scheduling-evidence/timings/pool-mixed/results.json),
  all clients finishing, separate short/long tails, maximum latency and a 16-backend ceiling.

The complete driver exited zero. [Completion record](perf-scheduling-evidence/timings/timing-matrices.passed),
[exact commands](perf-scheduling-evidence/timings/run-timings.sh),
per-matrix environments, logs, all result rows and full summary repetitions/ranges/CV
are retained here. Raw logs/configurations remain at
`/home/publicuser/dev/rustybouncer-evidence/perf-scheduler-20261109/` until final
complete archiving. The earlier before matrices and raw profiles remain committed
in their existing evidence directories. The historical `c8997e8` source/data are
unchanged. No owned build, native test, profiler or archive job overlapped these
four normal timing matrices.

The completed observations show substantial high-concurrency churn/admission gains,
remaining low-concurrency/one-CPU/CTE costs, higher native RSS in several treatments,
and large-result batching benefits for this specific result size. Every repetition,
including slower results and substantial shared-host/control shifts, remains
included. These are not isolated marginal costs, statistical significance or a
production SLO. Session-pooling maximum waits/minimum completions remain visible;
aggregate p99 alone can hide queued sessions.

After all four matrices finished, the full locked development gate passed:
**66 Rust +73 offline Python tests**, zero failures/errors/skips/ignored/filtered
Rust tests, Ruff, Astral ty, Clippy, all Prometheus/Grafana checks and complexity
**28/30**. [Complete output](perf-scheduling-evidence/timings/final-native.log).
The installed hook must also pass normally before this checkpoint commit exists.

The future fault harness now records exact database INSERT execution statements,
rejecting any duplicate execution even if it would raise a primary-key error.
It retains actual rows before oracle assertions and partial fault timelines.
Both synchronous standbys and statement logging are verified before load. These
24 experiments have **not yet run**. Shared Memcheck arms have explicit VM-only
180s queue/60s read budgets without reducing clients/transactions or changing any
native deadline checks; all eight VM arms still require execution.

`final_report.py` requires every planned matrix, provenance binding, strict native,
live and kind results before producing JSON/Markdown/interactive HTML. The new
retention helper preserves complete raw archives/manifests and exact frozen ELFs.
Neither complete report generation, archive collection nor visual preview is yet
accepted at this checkpoint.

Remaining gates: all 30 normalized lookup +16 original attribution arms, 24 after
pool profiles, all eight Memcheck arms, all 24 crash/promotion experiments, six
kind/CNPG cases, retained full artifacts, final README publication and workmux
integration. Broader canonical release acceptance remains open regardless of the
performance-task outcome. See the [complete protocol](../benchmarks/performance-final.md).
