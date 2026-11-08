# PERF-001 — complete baseline profiling and equal-core study

Authority: [performance tasks](../tasks/pgbouncer-performance.md) and
[canonical checklist](../../FEATURE_COMPLETENESS.md), S10–S12/R11.
Implementation/evidence committed as `2f33b63` through the mandatory installed hook.
This establishes PERF-001's profiling scope, not a runtime optimization or release
acceptance. PERF-002–009 and wider canonical scope remain open.

## Results

The unchanged original comparison passed all **30/30** arms. A separately labeled
one-proxy-CPU comparison passed **30/30** arms. Complete monotonic-clock attribution
passed **16/16** arms, and the large-result allocation baseline passed **4/4**.
All measured transactions reported zero errors. No valid repetition was excluded from either complete timing matrix.
The profiling runs are separate from normal timing and are not throughput evidence.

| Placement | Clients | Target | Median Q/s | Run min–max Q/s | TPS sample CV | Median p99 ms |
| --- | ---: | --- | ---: | ---: | ---: | ---: |
| Unrestricted | 10 | direct | 219,591 | 215,697–222,915 | 1.6% | 0.119 |
| Unrestricted | 10 | pgbouncer-session | 156,967 | 154,653–159,862 | 1.7% | 0.123 |
| Unrestricted | 10 | pgbouncer-transaction | 155,839 | 155,561–157,591 | 0.7% | 0.116 |
| Unrestricted | 10 | rustybouncer-session | 162,736 | 160,139–163,463 | 1.1% | 0.139 |
| Unrestricted | 10 | rustybouncer-routed | 67,776 | 67,029–68,133 | 0.8% | 0.419 |
| Unrestricted | 100 | direct | 334,655 | 333,558–335,536 | 0.3% | 1.693 |
| Unrestricted | 100 | pgbouncer-session | 154,899 | 154,142–155,308 | 0.4% | 1.127 |
| Unrestricted | 100 | pgbouncer-transaction | 151,702 | 148,702–151,872 | 1.2% | 1.319 |
| Unrestricted | 100 | rustybouncer-session | 186,463 | 184,261–186,467 | 0.7% | 1.412 |
| Unrestricted | 100 | rustybouncer-routed | 89,778 | 88,046–90,156 | 1.3% | 2.882 |
| One proxy CPU | 10 | direct | 230,292 | 203,688–230,991 | 7.0% | 0.105 |
| One proxy CPU | 10 | pgbouncer-session | 157,236 | 153,802–159,222 | 1.7% | 0.120 |
| One proxy CPU | 10 | pgbouncer-transaction | 156,861 | 151,593–157,438 | 2.1% | 0.117 |
| One proxy CPU | 10 | rustybouncer-session | 151,751 | 145,220–153,260 | 2.8% | 0.123 |
| One proxy CPU | 10 | rustybouncer-routed | 28,483 | 28,104–28,528 | 0.8% | 0.526 |
| One proxy CPU | 100 | direct | 334,739 | 317,943–335,250 | 3.0% | 1.681 |
| One proxy CPU | 100 | pgbouncer-session | 149,011 | 144,928–149,053 | 1.6% | 1.268 |
| One proxy CPU | 100 | pgbouncer-transaction | 148,824 | 147,383–151,694 | 1.5% | 1.345 |
| One proxy CPU | 100 | rustybouncer-session | 146,341 | 146,119–149,207 | 1.2% | 1.287 |
| One proxy CPU | 100 | rustybouncer-routed | 27,580 | 26,871–27,673 | 1.6% | 5.348 |

`rustybouncer-session` is the historical passthrough target name; it does not
mean shared cross-client pooling. All targets reached 10/100 PostgreSQL backends
at the corresponding levels. [Interactive runs/ranges](../benchmarks/perf-001/chart.html)
preserve every timing repetition.

## Ranked attribution

The observed bottleneck evidence supports this implementation order:

1. **Wire I/O:** about 15 socket sends and 15 receives per routed lookup versus
   about two of each in passthrough. Returned socket bytes/Q are comparable; the
   difference is call granularity. Exact counts below include readiness/preflight
   and probes, so they are lifetime amortizations rather than purified steady
   costs. CPU callchains independently place substantial work in frame I/O.
   Coalesce writes (PERF-002) and reduce repeated frame reads (PERF-003).
2. **Allocation/classification:** routed lookups allocate about 14 times/Q versus
   much lower passthrough amortization. Heap callchains include message bodies,
   result collection and word scanning. Allocation-light framing and tokenization
   are justified candidates; a classification cache is not established as useful.
3. **Runtime/routing:** caller sampling retains substantial runtime/other and
   dispatch work. One-core routed throughput is lower than unrestricted mode;
   switching the default to one worker is not supported as an optimization.
   PERF-005 still needs mixed-duration/slow-receiver evidence.
4. **Metrics/topology:** metrics-disabled guard paths are sampled; enabled-metrics
   overhead is not isolated here. Discovery runs at 500ms and has no distinct
   sampled bucket in this short capture. Absence of samples does not prove no cost.

CPU shares below use exclusive observed-callchain buckets in documented precedence,
weighted by event periods. They are user-mode sample shares, not fractions of
whole-system elapsed time or isolated marginal costs. Inlining/unresolved callers
and kernel execution limit attribution; raw self/caller reports are retained.

| Routed category | 10 clients | 100 clients |
| --- | ---: | ---: |
| frame_write | 24.8% | 21.7% |
| frame_read | 16.4% | 22.0% |
| routing_and_dispatch | 19.7% | 23.2% |
| classification | 6.3% | 8.9% |
| result_accumulation | 0.9% | 1.8% |
| response_delivery | 3.5% | 2.3% |
| metrics | 0.2% | 0.6% |
| runtime_or_other | 28.3% | 19.5% |

| Clients | Target | Socket sends/Q | Socket receives/Q | Allocation calls/Q | Allocated bytes/Q |
| ---: | --- | ---: | ---: | ---: | ---: |
| 10 | rustybouncer-session | 2.010 | 2.020 | 0.098 | 75.2 |
| 10 | rustybouncer-routed | 15.133 | 15.251 | 14.154 | 711.9 |
| 100 | rustybouncer-session | 2.008 | 2.018 | 0.032 | 44.1 |
| 100 | rustybouncer-routed | 15.117 | 15.146 | 14.082 | 685.2 |

Allocation sizes count every allocation/reallocation, not just live memory.
Startup, preflight, probes and profiler lifetime are included in heap/syscall
amortization; `EAGAIN` and futex syscall errors are distinct from SQL failures.
Large-result baseline: 2,048 rows with 128-byte payloads, ten transactions/client;
exact row oracles precede/follow load. Routed allocation amortization is 2,134/Q
at ten clients and 2,089/Q at 100, with observed RSS peaks 17.1/54.5 MiB. Short
passthrough captures can finish between 250ms RSS samples; missing peaks remain
unreported, not invented as zero. This motivates buffering work, not a memory-win
claim for PgBouncer. [Large results](perf-001-evidence/large-summary.json).

Off-CPU interval sums for 16-worker processes range about 123–154 thread-seconds
inside ten-second windows. Sleeping/runnable intervals overlap across threads,
so they cannot be reported as process blocked percentages or exact kernel wait
causes. Partial edge intervals are excluded. Syscall wall durations also sum over
threads. Context switches and measured CPU/RSS for normalized timing arms are in
[summary](perf-001-evidence/summary.json) and
[timing snapshots](perf-001-evidence/timing-metadata.json.gz).

## Method, reproducibility and evidence

Locked Nix, Linux x86_64, Ryzen 7 9700X (8 cores/16 logical CPUs). One-CPU treatment
pins only the proxy to CPU 0; Rust has exactly one observed Tokio worker plus its
main thread. PostgreSQL/pgbench keep affinity 0–15. Ancestor cgroup controls have
no CPU quota; affinity is the treatment, not dedicated/exclusive capacity.
All actual placements and observed ancestor controls are retained. The host is
shared; three runs give descriptive ranges/CV, not production confidence/SLOs.
Tool cache provisioning overlapped early unrestricted work. Every valid run stays
in the result. No performance pass/fail budget is invented or imposed.

All configurations preserve the original workload: indexed random lookup over
10,000 rows, 128-byte payload, simple protocol, trust on owned loopback fixtures,
no TLS/metrics; frontend/pool ceilings 256, PostgreSQL 400; 5s warmup/30s timing.
Profiling uses 10s CPU/off-CPU runs or exact 1,000 transactions/client heap/syscall
runs. Symbol/frame-pointer profiling binaries are separate from timing binaries.

Tools: perf 7.3.1, heaptrack 1.6.80, strace 7.3. Iceman provisioned Linux-only
profilers in the shell. User-owned perf works with existing paranoid=2/ptrace=1;
no sudo or global security-policy changes. Explicit `--clockid mono` aligns perf
with Python CLOCK_MONOTONIC. Userspace sampling omits kernel stacks. See primary
[perf access documentation](https://man7.org/linux/man-pages/man2/perf_event_open.2.html)
and [heaptrack](https://github.com/KDE/heaptrack).

```sh
nix develop --no-update-lock-file --command python3 benchmarks/compare.py --output /home/publicuser/dev/rustybouncer-evidence/perf-001-20261108/baseline
nix develop --no-update-lock-file --command env CARGO_TARGET_DIR=target/perf-001 CARGO_PROFILE_RELEASE_DEBUG=2 RUSTFLAGS=-Cforce-frame-pointers=yes cargo build --release --locked --offline
nix develop --no-update-lock-file --command python3 benchmarks/performance_profile.py --output /home/publicuser/dev/rustybouncer-evidence/perf-001-20261108/normalized-and-profiles-rerun --binary target/release/rustybouncer --profile-binary target/perf-001/release/rustybouncer
nix develop --no-update-lock-file --command python3 benchmarks/attribution.py --output /home/publicuser/dev/rustybouncer-evidence/perf-001-20261108/attribution-monotonic --binary target/perf-001/release/rustybouncer
nix develop --no-update-lock-file --command python3 benchmarks/large_profile.py --output /home/publicuser/dev/rustybouncer-evidence/perf-001-20261108/large-baseline --binary target/perf-001/release/rustybouncer
nix develop --no-update-lock-file --command python3 benchmarks/profile_report.py --baseline /home/publicuser/dev/rustybouncer-evidence/perf-001-20261108/baseline --normalized /home/publicuser/dev/rustybouncer-evidence/perf-001-20261108/normalized-and-profiles-rerun --profiles /home/publicuser/dev/rustybouncer-evidence/perf-001-20261108/attribution-monotonic --output /home/publicuser/dev/rustybouncer-evidence/perf-001-20261108/summary.json
```

Fresh output directories are required. Reproducers using the current harness get
the explicit monotonic clock in both integrated and standalone attribution runs.
[Binary/source hashes](perf-001-evidence/source-provenance.json),
[normalized environment](perf-001-evidence/normalized-environment.json),
[cgroup controls](perf-001-evidence/cgroup-controls.json),
[all normalized runs](perf-001-evidence/normalized-results.json),
[all original reruns](perf-001-evidence/baseline-results.json),
[attribution inventory](perf-001-evidence/attribution-results.json),
[raw profile archives/hash index](perf-001-evidence/profile-archives.json) and
[full raw-file manifest](perf-001-evidence/full-raw-manifest.json.gz) are committed.
Each archive contains actual perf/heaptrack/strace input plus commands, placement,
resources, logs and decoded reports. Extract with `tar --zstd -xf ARCHIVE.tar.zst`.
Full raw timing latencies/configs and both executable binaries remain under
`/home/publicuser/dev/rustybouncer-evidence/perf-001-20261108`.

Historical diagnostics are retained: the first normalized attempt stopped on an
older Tokio worker-name assertion (actual name `tokio-rt-worker`); the complete
matrix restarted fresh. Initial profiles used perf's uncalibrated native clock;
window-aligned attribution was rejected and the entire 16-arm set repeated with
explicit mono. Neither failed analysis nor incomplete attempts are acceptance
runs. Logs/manifests retain them; no repetition was removed from the complete accepted matrices.

## Verification and remaining work

Full locked installed-hook verification passed: **26 Rust + 65 Python tests**,
zero failures/errors/skips, Ruff, Astral ty, Clippy, Promtool and Grafana checks;
complexity maximum **26/30**. [Hook](perf-001-evidence/milestone-hook.log.gz) and
[complete verification](perf-001-evidence/milestone-verification.log.gz) are retained. No router source was modified for this baseline milestone.

PERF-007's [pool design](../design/backend-pools.md) has a documented coordinator
review, committed before implementation; session reuse and all acceptance remain
open. [Memory ownership design](../design/result-memory.md) is a plan only.
Additional CTE/large/mixed workload tooling defines **180 mandatory arms**; its
execution and before/after interpretation remain PERF-005/009 work. It does not
replace the original comparison or establish slow-receiver/idle/churn/1,000-client,
prepared-query, cancellation, failover-under-load or release acceptance.
