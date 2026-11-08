# PERF-001 profiling harness — INCOMPLETE checkpoint

Authority: [task backlog](../tasks/pgbouncer-performance.md) and
[canonical feature checklist](../../FEATURE_COMPLETENESS.md), especially S10–S12.
No task/canonical checkbox is completed by this preparation checkpoint.

Implemented: Linux profiling tools via Iceman in the locked development shell,
with platform-specific packages kept conditional; a separate harness that executes
all 30 proxy-affinity-controlled arms followed by all 16 CPU/off-CPU/heap/syscall
attribution arms. The original compare.py and its historical evidence are unchanged.
Only the actual proxy launch is wrapped (real subprocesses and real SQL); no query,
result, metric or profiler output is mocked. Proxy affinity, effective threads,
PostgreSQL and harness affinity are recorded; CPU/RSS/context switches are read
from /proc. Controlled timing uses the production release binary; instrumentation
uses a separately built symbol/frame-pointer binary. Resource/profile collection
failures fail acceptance, and profiler groups have bounded shutdown and escalation.

CPU and switch-event access probes succeeded for owned processes with the existing
perf_event_paranoid=2/ptrace_scope=1 policies. No global security setting or sudo
privilege was used. Off-CPU evidence is process switch intervals; exact kernel
blocking stacks are not promised by userspace-only sampling. Syscall traces supply
separate I/O wait/call/returned-byte evidence. Heap counts are whole-profiler-lifetime
amortization including startup/readiness/preflight/probes, not isolated steady-state
allocation costs. These limitations must remain explicit in the final attribution.

Tools were discovered with Iceman and added using `iceman add linuxPackages.perf
heaptrack strace util-linux`, then the renamed canonical `perf` attribute; deprecated
alias removed. Native tools: perf 7.3.1, heaptrack 1.6.80, strace 7.3. Linux-only
packages are conditional in the cross-platform shell. [Access evidence](perf-001-evidence/profiler-probe.log.gz)
and [switch access](perf-001-evidence/offcpu-access.log.gz) are retained.

The complete unchanged baseline was rerun:

```sh
nix develop --no-update-lock-file --command python3 benchmarks/compare.py --output /home/publicuser/dev/rustybouncer-evidence/perf-001-20261108/baseline
```

[All 30 results](perf-001-evidence/baseline-results.json),
[environment](perf-001-evidence/baseline-environment.json) and
[complete output](perf-001-evidence/baseline.log.gz) are retained. All arms passed,
zero transaction errors; no valid repetition was removed. The shared host was
not isolated; tool cache provisioning overlapped early baseline work. No causal
performance claim or regression budget is inferred from this rerun.

Full external evidence directory:
`/home/publicuser/dev/rustybouncer-evidence/perf-001-20261108`.
The next required commands build the separate profiling binary and execute every
normalized/attribution case (fresh output path required):

```sh
nix develop --no-update-lock-file --command env CARGO_TARGET_DIR=target/perf-001 CARGO_PROFILE_RELEASE_DEBUG=2 RUSTFLAGS=-Cforce-frame-pointers=yes cargo build --release --locked --offline
nix develop --no-update-lock-file --command python3 benchmarks/performance_profile.py --output /home/publicuser/dev/rustybouncer-evidence/perf-001-20261108/normalized-and-profiles --binary target/release/rustybouncer --profile-binary target/perf-001/release/rustybouncer
```

PERF-001 remains incomplete until all required normalized/profile runs, ranked
attribution, raw artifacts and methodology/variability report are committed.
PERF-002–004 optimization decisions remain gated. PERF-007 has an independently
prepared [design draft](../design/backend-pools.md); its review, implementation
and acceptance are not established. PERF-005–009 retain their full original scope.

Verification for this checkpoint: focused Ruff/ty/complexity and safety tests during
development, followed by the mandatory complete locked Nix verification in the
normal installed commit hook. Exact resulting commit and hook outcome are reported
at handoff. No runtime/benchmark behavior was altered to obtain a passing result.
