# Metrics validation — INCOMPLETE prepared checkpoint

Authority: reviewed metric contract and
canonical checklist, particularly D04, H11,
S10–S12, F11/F12 and Q01–Q08. No checklist item is established by this checkpoint.

Delivery track 2 only. Base: `ad8aff86b1cff483a20a8b10d663208e5af54f20`.
Branch: `t3/metrics-test-harness`. Tool-provisioned worktree:
`/home/publicuser/.t3/worktrees/rustybouncer/t3-41cf4b05`.
Registered with `workmux open t3-41cf4b05 --target-name
rustybouncer-metrics-validation --run-hooks`. Registration succeeded; direnv
reported a blocked `.envrc`, so commands use explicit locked `nix develop`.
The coordinator owns integration and workmux merge. No push or PR.

## Implemented scope

- `tests/metrics.py`: ten mandatory black-box cases, without skips. Exact
  inventory/types/HELP/label domains and 117+29*N series budget, escaping, finite
  nonnegative values, monotonic counters, fixed histogram buckets and +Inf/count
  equality. Each retained scrape is checked by real `promtool check metrics`.
  Unused query domains start at zero; known Q messages reconcile terminal totals,
  attempt SQL outcomes and duration counts.
- Real owned PostgreSQL primary and two physical streaming replicas; composes
  the existing wire client, lifecycle, fault and promotion methods without editing
  their fixture. Independent config includes `metrics_listen`, frontend capacity
  256 and PostgreSQL `max_connections=400` on every node. Teardown is registered
  before setup, retaining proxy/PostgreSQL logs and generated configs on failure.
- SQL/read-only CTE/write/transaction/stateful/SQL-error/multi-statement/COPY,
  malformed complete vs truncated/oversized frames, clean termination, abandoned
  startup, unsupported startup, unavailable startup, capacity rejection, client
  disconnect, RST delivery failure and independent `pg_stat_activity` accounting.
  Reconciliation runs after bounded eventual teardown; topology probes and the
  verifier's own activity are excluded. Internal task cancellation is not simulated
  by a client disconnect.
- Actual eligible-read backend termination verifies a recovered result, failed
  attempts and retries without duplicated Q totals. Real promotion verifies
  topology and primary changes, interrupted transaction non-replay and dispatched
  write non-replay. Concurrent clients verify distinct correct results. Freshness
  advances for unchanged and failed rounds. SQL sentinels cannot appear as labels
  or increase cardinality.
- Disabled/default endpoint absence, occupied bind failure, opaque passthrough
  zeros, 404/405, malformed/oversized headers, slow request deadline, 16-connection
  admission cap, concurrent valid scrapes, no keepalive, and SQL during abuse.
- `tests/test_metrics_contract.py`: four offline oracle tests; these do not
  establish runtime endpoint behavior.
- `benchmarks/metrics.py`: identical binary SHA-256 recorded; no-op disabled,
  enabled/no scrape, enabled/1Hz arms at both 10 and 100 clients. Three alternating
  forward/reverse repetitions per concurrency, 5s warmup and 30s measurement;
  all 18 measured arms are mandatory. Each arm preflights all clients concurrently.
  Uses identical `SELECT 1` simple-protocol pgbench workload, 10% random transaction
  latency sampling, and records raw transaction logs, stdout/stderr, full commands,
  durations, transaction/sample/error counts, throughput, p50/p95/p99 milliseconds,
  and raw scrape responses/errors. Percentiles use sorted sample floor indices;
  no performance budget or S01–S12 completion claim is invented.

## Static verification and retained evidence

`metrics-validation-evidence/static-verification.log` records
`nix develop --command bash scripts/verify`: locked offline Cargo format/check/
clippy/tests, Ruff, ty, complexity, full Python discovery, Nix/shell/CI syntax.
The final checkpoint is committed through the normal installed `.githooks/pre-commit`
which executes `nix flake check --no-update-lock-file` against the staged snapshot.
The commit hash is reported at handoff rather than self-referenced in its tree.

Additional owned-file checks (the existing verify script explicitly names only
`benchmarks/concurrency.py`, so it does not cover the new benchmark's Ruff/ty):

```sh
nix develop --command ruff check tests/metrics.py tests/metrics_support.py tests/test_metrics_contract.py benchmarks/metrics.py
nix develop --command ty check tests/metrics.py tests/metrics_support.py tests/test_metrics_contract.py benchmarks/metrics.py
nix develop --command python3 scripts/check_complexity.py tests/metrics.py tests/metrics_support.py tests/test_metrics_contract.py benchmarks/metrics.py
nix develop --command python3 -m unittest discover -s tests -p test_metrics_contract.py
```

Their complete output is retained in `metrics-validation-evidence/owned-static.log`.

## Runtime dependency and exact integration commands

Baseline main has no endpoint. No runtime suite or benchmark has been run here;
these are **pending**, not skipped or passing. No runtime branch was inspected or
modified. Static preparation cannot validate the metric names against the eventual
implementation. Coordinate any contract discrepancy with the reviewed plan before
changing expectations. Deterministic unexpected listener-task exit and session-task
abort injection belong to runtime-owned unit tests: final HTTP counters may be
inaccessible after process shutdown.

After integrating the runtime, the coordinator runs:

```sh
nix develop --command cargo build --release --locked --offline
nix develop --command env RUSTYBOUNCER_BIN="$PWD/target/release/rustybouncer" METRICS_EVIDENCE="$PWD/metrics-evidence" python3 tests/metrics.py
nix develop --command env RUSTYBOUNCER_BIN="$PWD/target/release/rustybouncer" METRICS_BENCH_EVIDENCE="$PWD/metrics-benchmark-evidence" python3 benchmarks/metrics.py
nix develop --command env RUSTYBOUNCER_BIN="$PWD/target/release/rustybouncer" python3 tests/failover.py
nix develop --command env RUSTYBOUNCER_BIN="$PWD/target/release/rustybouncer" python3 tests/long_queries.py
nix develop --command bash scripts/verify
```

Use a fresh benchmark output path (the runner refuses overwrites). Metrics runner
retains `result.json` plus complete unittest log, configs, scrapes, promtool results,
and proxy/PostgreSQL logs under `METRICS_EVIDENCE`. The benchmark retains partial
results after each arm and raw failing pgbench output before raising. A missing
tool, failed setup, nonzero SQL error count, missing samples or scrape failure is
an error, never grounds for reducing capacity or dropping an arm. No credentials
are needed for the owned isolated trust-auth fixture; do not point fault injection
at external resources. Root also owns operations rule/unit checks, existing full
7-case failover and 8-case long-query regressions and combined release acceptance.
All runtime behavior, performance observations and combined acceptance remain open.
