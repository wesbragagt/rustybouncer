# Failover integration suite

Build the router and run seven independent scenarios:

```sh
cargo build --locked
python3 tests/failover.py
```

Requirements: Python 3, Docker daemon access, and `postgres:16`. Each scenario
creates an isolated network, a primary, two real streaming replicas, and a proxy.
It tears down its processes, containers, anonymous volumes, and network even when
an assertion fails. All ports bind to loopback. Test-only PostgreSQL uses trust
authentication. Nothing is installed into the application database.

| Test | What it establishes |
| --- | --- |
| `test_01_same_connection_reads_round_robin_through_primary_failover` | Pure SELECTs alternate between replicas and continue on the same frontend socket after discovery withdraws the primary and while a replica is promoted. No client retry loop hides errors. |
| `test_02_unsent_write_waits_then_continues_on_same_connection` | A write submitted after primary loss waits, then executes against the promoted primary, followed by another write on the same connection. |
| `test_03_concurrent_writes_recover_and_split_brain_is_refused` | Six concurrent queued writes each return once after promotion; restoring the unfenced old primary blocks further writes with a bounded failure. |
| `test_04_interrupted_transaction_fails_without_replay` | SELECT inside BEGIN stays on the primary; failover breaks the transaction, and its uncommitted insert never reappears after promotion. A new client can write. |
| `test_05_failed_replica_leaves_rotation_without_disrupting_reads` | A replica can disappear before discovery notices without surfacing a read error. Reads continue through primary loss and use the promoted primary when no replica remains. |
| `test_06_read_only_ctes_round_robin_and_continue_through_failover` | Ordinary, nested, materialized and recursive CTEs alternate replicas, continue without a primary, and use the remaining replica after promotion. A CTE inside BEGIN stays on the primary. |
| `test_07_data_modifying_ctes_execute_on_primary` | INSERT/UPDATE/DELETE CTEs, unreferenced writes, outer writes and locking CTEs execute on the primary, verified on fresh sessions. |

Run one case while developing:

```sh
python3 tests/failover.py FailoverContinuity.test_02_unsent_write_waits_then_continues_on_same_connection
```

Override `RUSTYBOUNCER_BIN` to select a compiled binary or
`RUSTYBOUNCER_POSTGRES_IMAGE` to select a compatible PostgreSQL image.

These tests validate recoverable continuity, not an impossible guarantee that all
queries always succeed. An interrupted transaction deliberately fails. They do
not prove an unacknowledged COMMIT did not commit or establish exactly-once
business execution. Real production failover still needs durable replication,
leader fencing, and application idempotency for uncertain write outcomes.
