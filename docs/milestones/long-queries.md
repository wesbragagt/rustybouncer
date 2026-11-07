# Q01–Q04: bounded long reads

Authority: [FEATURE_COMPLETENESS.md](../../FEATURE_COMPLETENESS.md).
This track does not change the canonical checklist; coordinator review and merge
must record the implementation commit there. Q05–Q08 remain open.

## Policy and compatibility

Legacy TOML files continue to deserialize without new fields. New positive,
finite defaults are `query_timeout_ms = 300000`,
`read_retry_timeout_ms = 600000`, and `read_max_attempts = 3`.
Zero values are rejected. `wait_timeout_ms` still bounds routing and backend
setup, including startup and unsent writes. Each eligible read routing phase is
bounded by that wait and the remaining total deadline. Each dispatched eligible
read gets its own execution timeout, capped by the remaining total deadline.
The total read deadline starts when routing the client query begins, covering
routing, setup, execution, and all retries. Selected-backend attempts include
failed setup and topology races before dispatch. Failed nodes are excluded for
the remainder of this query, rather than reconnecting indefinitely to one node.
Writes, explicit transactions and pinned stateful sessions retain their existing
execution behavior and generation fencing; dispatched work is never replayed.

An in-flight eligible read watches the selected node's availability, rather than
primary generation or replica membership alone. A healthy replica promoted to
the unique primary may drain. Losing an unrelated primary or promoting another
replica does not abort the read. Ambiguity still interrupts work conservatively
and blocks new routing. Generation checks still retire cached backends before
new work following leadership changes. SQL/CTE classification and the function
allowlist are unchanged.

Results remain buffered through ReadyForQuery, with the existing 16 MiB message
and 32 MiB result bounds. Failed attempts expose no frames to clients. A successful
retry restarts SQL on a new backend with a new snapshot; it does not move query
execution state. SQL ErrorResponse cycles are returned without replay. Budget
exhaustion closes the frontend with SQLSTATE 08006. Interrupted writes retain an
unknown-outcome diagnostic; this does not prove whether a dispatched write committed.

Cancellation keys, user cancellation, server cancellation cleanup and reusable
backend cleanup races are not implemented (Q05–Q06). Dropping a failed TCP backend
is not a cancellation-cleanup guarantee. Q07's broader buffering/spooling contract
and Q08's load/race matrix also remain open. There is no invented cancellation
budget for an unsupported cancellation path.

## Integration coverage

`tests/long_queries.py` runs eight tests without capability switches or skips.
Every test owns a UUID Docker network, a primary and two streaming replicas,
temporary configuration/logs and a proxy process, using failure-safe fixture
cleanup. Read-only test views provide controlled delays without adding pg_sleep
to the production classifier. Observable pg_stat_activity barriers identify the
actual executing backend before fault injection.

Coverage: execution exceeding routing wait; unrelated primary loss/promotion;
promotion of the executing replica; termination after rows were generated with
no client frames exposed and exactly one complete replacement result; a one-attempt
limit; total read deadline; interrupted dispatched write; interrupted explicit
transaction. All seven existing failover tests additionally cover round robin,
queued writes, split brain, CTE safety and transaction non-replay.

## Verification evidence

Environment: Linux 7.0.3 x86_64, Docker Engine 28.5.2, PostgreSQL image
`postgres:16` ID
`sha256:74d436cb49d3398cf7b5d7b33635d43623d9e4da4b93c22db453f7d5d81181b1`.
Base commit: `2a8717544d1a3f196fa6342b74fbf44765e74a5b`.
Branch: `t3/bounded-long-query-retries`. Worktree:
`/home/publicuser/.t3/worktrees/rustybouncer/t3-706f789c`.
All commands ran in this worktree with the existing locked Nix toolchain;
no dependencies, thresholds, capabilities, concurrency settings or hooks changed.
The blocked direnv auto-loader emitted a warning; explicit `nix develop --command`
provided the toolchain and completed successfully.

Commands:

- `nix develop --command cargo build --locked`: exit 0.
- `nix develop --command python3 tests/long_queries.py`: exit 0,
  8 passed in 102.902 seconds; 0 failures/errors/skips/expected failures.
- `nix develop --command python3 tests/failover.py`: exit 0,
  all 7 passed in 89.397 seconds; 0 failures/errors/skips/expected failures.

The integration outputs below are retained from these final runs. Earlier full
runs also passed (8 in 87.075s, 7 in 81.796s); final runs follow the final behavior
change preserving the previous non-read execution policy.


### Dedicated long-query suite

```text
test_attempt_limit (__main__.LongQueries.test_attempt_limit) ... ok
test_dispatched_write_not_replayed (__main__.LongQueries.test_dispatched_write_not_replayed) ... ok
test_failed_read_retries_without_partial_results (__main__.LongQueries.test_failed_read_retries_without_partial_results) ... ok
test_healthy_execution_exceeds_routing_wait (__main__.LongQueries.test_healthy_execution_exceeds_routing_wait) ... ok
test_interrupted_transaction_not_replayed (__main__.LongQueries.test_interrupted_transaction_not_replayed) ... ok
test_promoted_backend_drains (__main__.LongQueries.test_promoted_backend_drains) ... ok
test_total_deadline (__main__.LongQueries.test_total_deadline) ... ok
test_unrelated_primary_loss_and_promotion (__main__.LongQueries.test_unrelated_primary_loss_and_promotion) ... ok

----------------------------------------------------------------------
Ran 8 tests in 102.902s

OK
```

### Existing failover suite

```text
test_01_same_connection_reads_round_robin_through_primary_failover (__main__.FailoverContinuity.test_01_same_connection_reads_round_robin_through_primary_failover)
No client retries/reconnects: reads continue throughout leader loss. ... ok
test_02_unsent_write_waits_then_continues_on_same_connection (__main__.FailoverContinuity.test_02_unsent_write_waits_then_continues_on_same_connection)
An autocommit statement received during the leader gap waits safely. ... ok
test_03_concurrent_writes_recover_and_split_brain_is_refused (__main__.FailoverContinuity.test_03_concurrent_writes_recover_and_split_brain_is_refused)
Concurrent queued clients resume once; ambiguous leaders cannot accept writes. ... ok
test_04_interrupted_transaction_fails_without_replay (__main__.FailoverContinuity.test_04_interrupted_transaction_fails_without_replay)
Transactions cannot migrate: rollback and uncertain outcomes stay explicit. ... ok
test_05_failed_replica_leaves_rotation_without_disrupting_reads (__main__.FailoverContinuity.test_05_failed_replica_leaves_rotation_without_disrupting_reads)
A replica failure is removed; same-client reads survive primary loss too. ... ok
test_06_read_only_ctes_round_robin_and_continue_through_failover (__main__.FailoverContinuity.test_06_read_only_ctes_round_robin_and_continue_through_failover) ... ok
test_07_data_modifying_ctes_execute_on_primary (__main__.FailoverContinuity.test_07_data_modifying_ctes_execute_on_primary) ... ok

----------------------------------------------------------------------
Ran 7 tests in 89.397s

OK
```

### Full installed verification script

`nix develop --command bash scripts/verify` exited 0: rustfmt, locked offline
all-target/all-feature Cargo check and strict Clippy, all 10 Rust tests (0 failed,
ignored or filtered), Ruff, Astral ty, complexity (277 functions; maximum 24,
limit 30), all 16 offline Python tests (0 failed/errors/skips), nixfmt, shellcheck
and actionlint passed. No capability-filtered 62-case baseline was used.

### Locked Nix verification

`nix flake check --no-update-lock-file` exited 0 on x86_64-linux. Verified derivation:
`/nix/store/zk4pmmn4ss8842hd48hfrx5qbwn5yn7l-rustybouncer-0.1.0.drv`.
`nix log` retained these results: all 10 Rust tests passed, 0 failed/ignored/filtered;
all 16 offline Python tests passed in 0.258s, 0 failures/errors/skips; Ruff and ty
passed; complexity maximum 24/30. Entire checkPhase completed in 46 seconds.
The command also built the locked release package. Other architecture/platform
outputs were not evaluated; this is x86_64-linux evidence only.

An obsolete intermediate Nix invocation was interrupted after being superseded;
it is not acceptance evidence. The complete final-source invocation above passed.
The installed pre-commit hook additionally verifies the exact staged snapshot
with `nix flake check --no-update-lock-file path:<temporary snapshot>`; commit
success is conditional on that complete check passing, without bypasses.

### Strengthened buffering fixture: final dedicated run

The final fixture adds a 1000-byte payload per row so PostgreSQL's output buffer
flushes well before termination, exercising partially received backend results.
`nix develop --command python3 tests/long_queries.py` exited 0: 8 passed in
69.846 seconds, 0 failures/errors/skips/expected failures. This supersedes the
smaller-payload dedicated result above; all eight tests ran again.

```text
test_attempt_limit (__main__.LongQueries.test_attempt_limit) ... ok
test_dispatched_write_not_replayed (__main__.LongQueries.test_dispatched_write_not_replayed) ... ok
test_failed_read_retries_without_partial_results (__main__.LongQueries.test_failed_read_retries_without_partial_results) ... ok
test_healthy_execution_exceeds_routing_wait (__main__.LongQueries.test_healthy_execution_exceeds_routing_wait) ... ok
test_interrupted_transaction_not_replayed (__main__.LongQueries.test_interrupted_transaction_not_replayed) ... ok
test_promoted_backend_drains (__main__.LongQueries.test_promoted_backend_drains) ... ok
test_total_deadline (__main__.LongQueries.test_total_deadline) ... ok
test_unrelated_primary_loss_and_promotion (__main__.LongQueries.test_unrelated_primary_loss_and_promotion) ... ok

----------------------------------------------------------------------
Ran 8 tests in 69.846s

OK
```

Implementation/evidence commit: the milestone commit introducing this document
(subject `Q01-Q04: separate read execution and bound buffered retries`); resolve
its exact SHA with `git log -1 --format=%H -- docs/milestones/long-queries.md`.
The handoff records the actual resulting SHA after the installed hook passes.
Coordinator action remains: review, merge through workmux, rerun combined-branch
integration, then update canonical Q01–Q04 evidence. Q05–Q08 stay unchecked.
