# H12/R12 reporting and strict acceptance evidence

Authority: the local feature checklist.
Scope: reporting/acceptance gate only; the coordinator owns checklist updates.

## Implemented behavior

One finalized record per test invocation, including repeated IDs and reuse of the
same TestCase object. Durations include setup, body, teardown and cleanup; evidence
is snapshotted after cleanup. Multiple errors and subtest outcomes aggregate into
the parent record, retaining every diagnostic. Outcome precedence is error,
failure, unexpected success, expected failure, skip, pass. Class/module fixture
errors without a test start produce explicit zero-duration fixture records.

JSON summaries and JUnit totals count finalized records, not error callbacks.
Expected failures retain their explicit JSON status and JUnit skipped type;
unexpected successes become JUnit failures. Raw unittest diagnostic lists remain
available, including skip counts; JSON diagnostic_counts separately preserves
callback totals even when a parent record aggregates several outcomes. Skips and expected failures reject acceptance
and CLI success, as do failures/errors/unexpected successes and empty runs.
No allow-skips bypass exists. Filtered developer runs are explicitly non-release
evidence and cannot set JSON accepted=true. Successful endpoint runs still prove
only their declared contract, not the global release matrix.

Credential redaction applies recursively before serialization to contract, evidence,
test names, diagnostics and mapping keys, including passwords containing quotes,
backslashes and newlines. JUnit replaces XML-forbidden control characters in
wire diagnostics so the report remains parseable; JSON retains those diagnostics.

## Dedicated complete reporting integration suite

Command: `nix develop --command python3 -m unittest discover -s tests -p test_conformance_reporting.py`

Result: 12 tests passed, 0 failures, 0 errors, 0 skips, 0 expected failures,
0 unexpected successes. Latest standalone run: 0.048 seconds.
All reporting scenarios run offline without network endpoints. Nested simulated
bad outcomes are test inputs whose rejection is asserted by passing outer tests.
Coverage includes repeated instances, full cleanup duration/evidence, setup and
teardown errors, multiple cleanup errors, class fixture errors, subtest errors /
failures / skips, all six outcomes, JSON/JUnit totals, serialized redaction,
control bytes, empty inventory, focused-run labeling and actual main() exit
propagation for pass / skip / failure / error / expected failure / unexpected
success with repeated inventory.

Development checks: `nix develop --command bash -c 'ruff check conformance/reporting.py conformance/__main__.py tests/test_conformance_reporting.py && ty check conformance/reporting.py conformance/__main__.py tests/test_conformance_reporting.py && python3 -m unittest discover -s tests -p test_conformance_reporting.py'`
passed Ruff, Astral ty and all 12 tests. These development checks do not replace
full verification.

Full offline scope command: `nix develop --command bash -c 'ruff check tests benchmarks/concurrency.py scripts conformance && ty check tests benchmarks/concurrency.py scripts conformance && python3 scripts/check_complexity.py && python3 -m unittest discover -s tests -p "test_*.py"'`
Result: Ruff and ty passed; complexity 301 functions, maximum 23, limit 30;
28 tests passed in 1.070 seconds, zero failures/errors/skips/expected failures/
unexpected successes. Diagnostic transcript: `/tmp/rustybouncer-reporting-offline.log`.

## Full locked verification

Earlier in-development full Nix runs were superseded and interrupted after source
updates; they are not completion evidence. The final source check uses
`nix flake check --no-update-lock-file --print-build-logs`, transcript
`/tmp/rustybouncer-reporting-verified-nix.log`.

Final source check exited 0 on 2026-11-07. Rust: 10 passed, 0 failed,
0 ignored, 0 measured, 0 filtered out. Offline Python: 28 passed in 0.270s,
zero failures/errors/skips/expected failures/unexpected successes. Rust typecheck,
strict Clippy/rustfmt, Ruff, Astral ty, complexity (301 functions, max 23 <= 30),
Nix formatting, shellcheck and actionlint all passed. This is the full installed
verification scope, without exclusions or lowered concurrency. Only the native
x86_64-linux flake check ran; other supported-platform builds were not executed.

The installed `.githooks/pre-commit` verifies the exact staged snapshot with
`nix flake check --no-update-lock-file "path:$snapshot"` before allowing this
milestone commit. It is run normally, without bypasses; its transcript is retained
at `/tmp/rustybouncer-reporting-commit.log`, and its final result / commit SHA is
reported in the handoff. The committed evidence summary is retained here; temporary
raw transcripts are supplemental local diagnostics, not durable release artifacts.

## Environment and limits

Base: `2a8717544d1a3f196fa6342b74fbf44765e74a5b`.
Branch: `t3/strict-conformance-reporting`.
Worktree: `/home/publicuser/.t3/worktrees/rustybouncer/t3-a4f7c4bf`.
Linux 7.0.3, x86_64, locked flake toolchain; file descriptor limit 524288.
No dependency, runtime pooler, capability manifest, inventory, shared test or
verification-script changes. No endpoint credentials or Docker required for this
reporter-only integration suite. Commit is the milestone commit containing this
file (reported by exact SHA in the handoff).

## Remaining global work

R12 acceptance gating is implemented here; H09/R08 capability skip-generation and
all positive endpoint coverage remain coordinator/other-track work. Existing
62-case capability-skipping runs remain diagnostic baselines and now reject
acceptance; they are not release proof. G01–G08 remain open. No global checklist
item is checked in this branch. Parent must review scope/evidence before recording
H12 and the R12 gate milestone; removing global skip generation requires further
implementation outside these owned files.
