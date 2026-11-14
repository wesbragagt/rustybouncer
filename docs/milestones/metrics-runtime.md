# Prometheus runtime delivery track

This evidence is subordinate to the local feature checklist
and implements delivery track 1 of the reviewed
Prometheus metric contract. Relevant stable IDs
are D04, H11, S10–S12, F11/F12 and Q01–Q08; no canonical item is checked or
redefined by this runtime milestone. Combined metrics black-box, operations and
benchmark acceptance belong to the coordinator after integration.

Implemented: optional top-level `metrics_listen` with a registry-free disabled
context; process-owned Prometheus 0.14 registry; private example configuration;
pre-bound HTTP endpoint with fatal listener-exit supervision; 2-second total
connection deadline, 8-KiB headers, 16 task slots, immediate excess-connection
closure, no keepalive, 404/405 and Prometheus 0.0.4 encoding. No Nix dependency
changes were necessary.

Runtime accounting includes admission/rejection, session terminal dispositions,
frontend-owned TCP backends including setup, routed fully framed Q messages,
backend dispatch attempts and actual retry selections. Explicit typed outcomes
keep SQL errors, router errors, delivery errors and external aborts distinct.
FATAL rejection paths now return explicit errors. Dispatch guards start inside
the polled execution future; their owner survives internally dropped execution
futures and marks deadline/topology interruptions as errors. TCP attempt tickets
similarly survive enclosing setup deadlines. Backend/session/query live gauges
use RAII. SQL classification occurs inside the query duration observation.
Internal SET and discovery traffic do not increment frontend work.

Every completed discovery round updates per-node eligibility and freshness,
including unchanged and unavailable rounds. All finite label combinations are
initialized. Tests check the exact `117 + 29*N` sample budget, all histogram
boundaries, escaping of configured node names, and the disabled context's empty
registry. No SQL, user, database, address, credentials or error text becomes a
label. Routing, result buffering, retry budgets, backend selection, and write
non-replay rules are retained.

The isolated worktree is
`/home/publicuser/.t3/worktrees/rustybouncer/t3-f5c39d98`, branch
`t3/add-prometheus-metrics`, based on
`ad8aff86b1cff483a20a8b10d663208e5af54f20`. Registration command:

```sh
workmux open t3-f5c39d98 --target-name rustybouncer-metrics-runtime --run-hooks
```

Workmux reported setup complete and opened its tmux session. Its shell emitted
an existing direnv authorization warning; explicit locked `nix develop` commands
worked normally. No delegates were launched, and no push, PR, workmux merge,
Python/operations edits or canonical checklist edits were performed.

Full retained evidence is outside the shared source tree at
`/home/publicuser/dev/rustybouncer-evidence/metrics-runtime-20261108/`.
`environment.log` records x86_64 Linux, Rust/Cargo 1.99.0, Ruff 0.17.3,
ty 0.0.95, Promtool 3.15.1 and Docker 28.5.2; PostgreSQL fixture image is
`postgres:16`. The final retained debug binary SHA-256 is
`4da8a4d164956becdc718010efde49ff6ea2c896b90a7b53fd03c290acf08979`.

Required verification commands:

```sh
nix develop --command bash scripts/verify
nix flake check --no-update-lock-file --print-build-logs
nix develop --command bash -c 'cargo build --locked && cp target/debug/rustybouncer /home/publicuser/dev/rustybouncer-evidence/metrics-runtime-20261108/rustybouncer-debug && sha256sum /home/publicuser/dev/rustybouncer-evidence/metrics-runtime-20261108/rustybouncer-debug && export RUSTYBOUNCER_BIN=/home/publicuser/dev/rustybouncer-evidence/metrics-runtime-20261108/rustybouncer-debug && python3 tests/failover.py && python3 tests/long_queries.py'
nix develop --command python3 /home/publicuser/dev/rustybouncer-evidence/metrics-runtime-20261108/runtime-smoke.py
```

`verify.log`: Rust formatting, locked offline all-target/all-feature check,
Clippy with warnings denied, **26 Rust tests**, Ruff, ty, **45 Python unit tests**,
complexity (**maximum 26; limit 30**, 463 functions), Nix formatting, ShellCheck
and actionlint all pass. There are zero failed, ignored/skipped or filtered tests.
New deterministic tests cover task abort and gauge reconciliation, explicit
terminal precedence, malformed/full-frame boundaries, SQL-error attempts,
deadline/topology interruption versus external abort, failed TCP setup,
startup FATAL accounting, unchanged topology freshness, escaped node labels,
HTTP parser/status/header limits, slow headers, scrape saturation, bind conflict,
and fatal supervision of unexpected listener termination.

`nix-flake-check-final.log`: full native locked Nix package verification passes.
Non-native Darwin/aarch64 systems are outside this host's execution scope; no
cross-platform execution is claimed. The normal installed pre-commit hook also
runs `nix flake check --no-update-lock-file` on the exact staged snapshot;
`commit-hook.log` retains that verification and the resulting milestone commit.

`regressions-final.log`: complete **7 failover + 8 long-query** cases pass with
zero failures, errors or skips, using the immutable retained binary and uniquely
named owned Docker fixtures. Earlier development runs remain as diagnostic logs.

`smoke-final.log`, `initial-exposition.prom`, `promtool.log`, and
`bind-conflict.log`: a real enabled process exposes 204 series for three nodes;
`promtool check metrics` parses the full live exposition successfully, and an
occupied metrics port makes startup fail before PostgreSQL acceptance.
`runtime-smoke.py` retains the reproducible smoke command implementation.

The milestone commit contains the runtime, configuration, dependencies and this
evidence document; obtain its exact hash from `git log -1 --format=%H --
docs/milestones/metrics-runtime.md` or the external `handoff.json`. No full
metrics black-box integration, comparative overhead benchmark, Prometheus rule
acceptance, 1,000-client acceptance, or broader D04/S10/G01–G08 completion is
claimed here. Those remain coordinator-owned acceptance work after merging the
other delivery tracks. There are no runtime contract changes to coordinate.

One repeated smoke setup initially attempted to rebind its just-closed HTTP port
without handling TCP TIME_WAIT. That fixture-only failure is retained in
`smoke-fixture-time-wait.log`. The retained smoke script now allocates a separate
owned occupied port for its bind-conflict assertion; the complete final smoke
passes. No runtime workaround or test exclusion was used.
