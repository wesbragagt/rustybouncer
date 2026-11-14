# Parallel milestone integration evidence

Authority: the local feature checklist.
Date: 2026-11-07 UTC. This establishes the assigned parallel batch and seven
checklist items, not overall conformance or release acceptance.

All three agents worked in isolated tool-provisioned worktrees with disjoint
ownership. The coordinator reviewed source, tests and evidence, then merged each
branch with workmux. The exact implementation/merge SHAs and workmux handles are
in the merge ledger. All three pre-merge
`nix flake check --no-update-lock-file` hooks exited 0; no conflicts, bypasses or
manual cherry-picks were used. Branches/worktrees were retained.

Combined code base: `fe8411d3af0893f8853e65fd05acfafe1aae3ed3`.
Coordinator review additionally registered class and fixture cleanup before
startup/setup in `tests/deadline_integration.py`; the entire five-test suite and
full verification script passed again after that change. Shared guides now
explain the implemented read budgets and strict acceptance policy.

## Environment and commands

Linux 7.0.3 x86_64, Nix 2.31.4, locked devShell, Python 3.14.8,
Psycopg 3.3.6, Docker Engine 28.5.2. Failover/long-query tests provision their own
Docker network and PostgreSQL 16 primary with two streaming replicas. Image ID:
`sha256:74d436cb49d3398cf7b5d7b33635d43623d9e4da4b93c22db453f7d5d81181b1`.
Deadline integration provisions a separate local PostgreSQL cluster with dynamic
port allocation; it tests harness behavior, not shared pooler capacity.

All commands below ran from the main checkout with no test filters, capability
switches, reduced concurrency, skip allowances or verification bypasses.

| Exact command | Observed result | Retained evidence |
| --- | --- | --- |
| `nix flake check --no-update-lock-file --print-build-logs` | Exit 0; Rust 10, offline Python 42; maximum complexity 24/30 | Nix output `/nix/store/2d5kwp0ml92lqfqw5h771ig9jqqxwv9p-rustybouncer-0.1.0`; equivalent verification output below |
| `nix develop --command cargo build --locked` | Exit 0; dev binary built in 5.81s | Terminal result recorded here |
| `nix develop --command bash scripts/verify` | Exit 0; Rust 10, offline Python 42 in 0.516s | [Full verification output](parallel-integration/verify.log) |
| `nix develop --command python3 tests/failover.py` | Exit 0; 7 tests in 57.771s | [Failover output](parallel-integration/failover.log) |
| `nix develop --command python3 tests/long_queries.py` | Exit 0; 8 tests in 62.682s | [Long-query output](parallel-integration/long-queries.log) |
| `nix develop --command python3 -m tests.deadline_integration` | Exit 0; 5 tests in 0.694s after review fix | [Deadline output](parallel-integration/deadlines.log) |

Every listed required test suite completed with zero failures, errors, skips,
expected failures, unexpected successes, Rust ignored tests or filtered tests.
The 42 offline tests include all 12 reporter and 14 adversarial deadline tests.
Simulated invalid outcomes are inputs to passing outer reporter tests, not skipped
verification cases. Verification includes Rust typechecking, strict Clippy,
rustfmt, Ruff, Astral ty, complexity <=30, nixfmt, shellcheck and actionlint.

Nix's initial combined check verified the merged source before the small fixture
review fix. The full verification script above verified the fix; the installed
pre-commit hook additionally verifies the exact staged integration snapshot with
`nix flake check --no-update-lock-file path:<snapshot>` before permitting the
integration commit. Its commit is discoverable via
`git log -1 --format=%H -- docs/milestones/parallel-integration.md`.
Nix's incompatible-platform notice is not a skipped test: this evidence covers
x86_64-linux only. Other architectures/platforms were not tested.

## Checklist registration and limits

- H07/H10: UUID schema isolation, cleanup registered before setup, all clients
  attempted despite cleanup failures, fresh direct schema disposal, explicit
  connection/operation/recovery/cleanup budgets, bounded predicates and owned
  adapter-process cleanup. [Track evidence](deadlines.md) documents the extra
  one-second task-join grace and cooperative-cancellation/OS limitations.
- H12/R12: per-invocation JSON/JUnit accounting includes setup, body, subtests,
  teardown and cleanup diagnostics; recursive credential redaction; skips and
  expected failures reject acceptance; focused runs cannot claim full inventory
  acceptance. [Track evidence](reporting.md) records the asserted bad-outcome cases.
- Q02–Q04: healthy reads survive unrelated primary changes and executing-replica
  promotion; retry attempts and total time are bounded; results stay private until
  completion, with no mixed/partial client results when a backend is killed.
  [Track evidence](long-queries.md) records exact policy and each scenario.

Q01 remains partial: routing, execution and retry budgets are separate, but
backend cancellation and its cleanup budget are not implemented. Q05–Q08 remain
open, including cancellation races, broader buffering policy and faults under
sustained load. Retrying a read creates a new snapshot; execution state does not
migrate. Writes/transactions are not replayed; ambiguous commits cannot be proven
uncommitted from transport errors.

H09/R08 remain open: baseline cases can still generate capability/fixture skips,
which now cause a nonzero exit rather than false acceptance. Historical skip-heavy
reports were not used as proof. The endpoint workflow cannot pass release
acceptance until its required features/fixtures are implemented and provisioned;
this batch does not weaken that gate. The complete PgBouncer/target deployment
matrix, sustained concurrency/resource tests and G01–G08 release acceptance remain
open. No performance or cross-platform conformance claim is made.
