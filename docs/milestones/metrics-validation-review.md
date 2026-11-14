# Metrics validation integration — INCOMPLETE checkpoint

Authority: the local feature checklist and the
reviewed plan. D04, H11 and S10–S12 remain open.
This is harness preparation and offline verification, not runtime acceptance.

The validation delegate committed `b88a82d501a56270b82e639225b79f80192b3e96`
on `t3/metrics-test-harness` in tool-provisioned worktree `t3-41cf4b05`.
Its [original report and retained logs](metrics-validation.md) remain unchanged.
Coordinator reviewed the complete fixture, oracle, ten black-box cases and
18-arm benchmark, then merged via:

```sh
workmux merge t3-41cf4b05 --into main --keep
```

Merge `dae11a9` retained the worktree and passed the configured locked Nix
pre-merge verification. No hook bypass, skip, capacity reduction, push or PR.

## Review corrections

- Passthrough still selects its initial primary through wait_backend. Its routing
  wait histogram must record that phase; only opaque query metrics stay zero.
  Corrected the contradictory expectation without changing the runtime contract.
- Benchmark preflight now owns every successful connection immediately, joins
  connecting workers before cleanup, and closes all clients even if a sibling
  connect/query fails. Two offline failure-injection tests cover both paths.
- A pgbench timeout now retains partial stdout/stderr, including byte output from
  TimeoutExpired, before propagating the error. A regression test checks this.
- Added unsupported extended-protocol rejection accounting, explicit truncated
  frame/abandoned startup session errors, and an observed completion barrier for
  abandoned startup instead of assuming a completed TCP handshake means accept
  has already run. These live assertions remain pending endpoint integration.
- The runtime suite refuses reused evidence directories. Required benchmark arms
  remain 10 and 100 clients, three repetitions, all three treatments, 5s warmup
  plus 30s measurement. Fixture capacity remains 256 frontend/400 per PostgreSQL
  node, not the older 64-client fixture limit.
- Discovery uses a dedicated probe role created before replication. Independent
  pg_stat_activity counts filter by frontend identity, so probes are excluded even
  during their brief idle transition before disconnect; no probe-SQL matching or
  timing assumption is used. Quiescence checks include every frontend backend
  state, so active or idle-in-transaction leaks cannot evade the independent count.
- Ruff and ty now cover all benchmarks through scripts/verify, so the new runner
  is enforced in ordinary staged-snapshot verification.

## Reproducible offline verification

Environment: repository's unchanged flake.lock, x86_64-linux. Run:

```sh
nix develop --no-update-lock-file --command bash -c 'ruff check tests benchmarks scripts conformance && ty check tests benchmarks scripts conformance && python3 scripts/check_complexity.py && python3 -m unittest discover -s tests -p "test_*.py"'
git diff --cached --check
git commit -m 'D04 S10 INCOMPLETE: integrate and harden metrics validation harness'
```

The first command passed Ruff, ty and complexity (453 functions; maximum 25/30),
and all 52 offline Python tests passed with zero failures/errors/skips. The normal
installed commit hook must also pass locked Nix verification, including Rust checks,
all offline Python tests and the integrated complete Prometheus rule suite, before
this checkpoint is committed. Its result and actual commit are reported at handoff.

## Remaining acceptance

Runtime metrics have not been integrated on this branch. No live metrics test or
benchmark arm has been executed or passed. Ten black-box cases, all 18 benchmark
arms, complete existing seven failover/eight long-query regressions, runtime abort
and listener-failure unit coverage, and combined deployment accounting remain
mandatory. The exact runtime execution commands are in the original report. These
pending checks are blockers to feature completion, not skipped tests or acceptance
exceptions. No canonical checkbox is changed by this checkpoint.
