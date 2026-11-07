# H07/H10 — bounded harness lifecycle

Authority: [FEATURE_COMPLETENESS.md](../../FEATURE_COMPLETENESS.md).
Scope: the bounded harness lifecycle track in
[parallel-milestones.md](../parallel-milestones.md), based on coordination commit
`2a8717544d1a3f196fa6342b74fbf44765e74a5b`. The canonical checklist is owned
by the coordinator and is unchanged here. This is not release/matrix evidence.

Implementation:

- Connection attempts use one application deadline, independently of libpq's
  integer connect timeout. A connection returned during cancellation is disposed.
- Query and raw-protocol deadlines cancel and join their owned task before
  disposing libpq, so readiness cleanup cannot remove a newly reused descriptor.
  Ordinary completed SQL failures retain their previous semantics. There is no
  query replay or connection retry.
- Disposal has a separate `cleanup_timeout` budget on Case-owned clients (default
  one second); query timeouts use `operation_timeout`. A fresh schema-cleanup
  connection uses `connect_timeout` and its DROP uses `cleanup_timeout` (default
  ten seconds). All numeric deadline reads reject non-finite, non-positive,
  boolean and nonnumeric settings, including directly constructed contracts.
- A single async operation has its declared budget plus at most one second of
  cancellation join grace. Ignored cancellation raises a diagnostic error with
  the original timeout/cancellation retained. Deferred disposal runs only after
  such a task eventually stops. Python cannot forcibly stop a coroutine or
  synchronous C operation that refuses to cooperate; socket disposal is not
  forced while its task may still own descriptor registrations.
- Fixture cleanup is registered before connection/schema setup. All clients are
  attempted even after a close failure, then a fresh direct connection attempts
  DROP SCHEMA IF EXISTS. Query and close failures are retained together. unittest
  keeps body failures and cleanup failures as separate diagnostics.
- Pending-task cleanup is registered before observation; stop_task reports a
  task that fails to stop. Recovery polling bounds each predicate by the remaining
  overall recovery budget and caps the final polling delay.
- Authorized fault commands run in new POSIX sessions. Timeout/cancellation kills
  only the invocation's process group, including descendants holding inherited
  output pipes after the leader exits. Pipe readers are joined after termination.
  Successful actions retain their existing behavior. Explicit fault authorization
  and callers' paired restore registrations remain intact.

Verification environment: Linux x86_64 (kernel 7.0.3), locked Nix 2.31.4 devShell,
Python 3.14.8, psycopg from the locked flake, PostgreSQL 18.6. Integration creates
its own trust-authenticated PostgreSQL cluster on a dynamically allocated loopback
port and removes it afterwards. No external credentials, Docker, pooler process,
capability switches, or reduced concurrency are used. The dedicated suite tests
harness behavior directly against PostgreSQL; it does not establish pooler
lifecycle or the full conformance matrix.

## Retained results

- `nix develop --command python3 -W error::ResourceWarning -m unittest discover -s tests -p 'test_*.py'`
  — 30 tests, 1.173s, OK; failures=0, errors=0, skips=0, expected failures=0,
  unexpected successes=0. Includes all pre-existing offline tests and 14 new
  adversarial tests. Intentional inner body/cleanup errors are asserted by a
  passing outer test, and are not hidden outer-suite failures.
- `nix develop --command python3 -m tests.deadline_integration`
  — 5 tests, 1.200s, OK; failures=0, errors=0, skips=0, expected failures=0,
  unexpected successes=0. Covers cancellation, handshake deadline, raw protocol
  deadline, rollback of a real uncommitted write after query timeout, and schema
  cleanup after disconnect with an open transaction.
- `nix develop --command ty check conformance tests/test_conformance_deadlines.py tests/deadline_integration.py`
  — all checks passed.
- `nix flake check --no-update-lock-file --print-build-logs`
  — exit 0 on x86_64-linux. Rust typecheck, strict Clippy, rustfmt, Ruff,
  Astral ty, complexity (299 functions, maximum 23, threshold 30), offline tests,
  Nix formatting, shellcheck and actionlint all passed. Rust: 10 passed,
  0 failed, 0 ignored, 0 filtered; Python: 30 tests in 0.511s, OK,
  0 failures/errors/skips/expected failures/unexpected successes. Check phase
  completed in 42 seconds. Retained Nix output:
  `/nix/store/yc9nsah19kav36iz5g5h965mwv6g3ajb-rustybouncer-0.1.0`.
  Raw working-machine log: `/tmp/deadlines-verification.log`; the actual terminal
  summary is retained below so evidence survives temporary-log cleanup.
  Other CPU/OS systems were not run; Nix reported the standard incompatible-system
  omission for aarch64-darwin, aarch64-linux and x86_64-darwin.

```text
running 10 tests
test result: ok. 10 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out
All checks passed!  [Ruff]
All checks passed!  [ty]
Complexity: 299 functions checked; maximum 23 (src/router.rs:488, session); limit 30.
..............................
Ran 30 tests in 0.511s
OK
checkPhase completed in 42 seconds
```

Earlier development checks included a ty finding (optional unittest return
value) and an inherited-pipe transport ResourceWarning; both were corrected.
Two superseded locked Nix builds were interrupted when the implementation changed;
they are not passing completion evidence. Final offline execution above has no
ResourceWarning. The skip-heavy endpoint baseline was not run or used as proof.

## Open limits and handoff

H07/H10 remain subject to coordinator review and canonical checklist evidence
registration. Other checklist IDs remain outside this track. The Python
cooperative-cancellation limitation above is explicit: a malicious driver can
outlive its deadline, and an event loop blocked by synchronous code cannot enforce
wall-clock deadlines. The suite reports this uncertainty rather than claiming
forced task termination. Descendants deliberately escaping the action's owned
POSIX session are outside the adapter ownership contract; no system-wide service
or process-name termination is attempted. Process creation and synchronous libpq
finish still depend on the host OS scheduling calls to return.

The implementation/evidence commit is the commit introducing this file (obtain
with `git log -1 --format=%H -- docs/milestones/deadlines.md`); its exact SHA is
reported in the agent handoff. The installed pre-commit hook must pass normally.
