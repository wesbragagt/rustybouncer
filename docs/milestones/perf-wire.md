# PERF-002 / PERF-003 — INCOMPLETE wire candidate checkpoint

Authority: [task backlog](../tasks/pgbouncer-performance.md) and
[canonical checklist](../../FEATURE_COMPLETENESS.md), W01/W09–W11, Q04/Q07,
F06/F07, S06/S11. No task/canonical checkbox is closed by this checkpoint.
Measured profiling dependency: `2f33b63` (PERF-001).

The candidate uses a vectored header/body writer with correct partial-write,
Pending, zero-write and disconnect behavior. A bounded 8-KiB backend reader
reduces repeated small socket reads. Each query attempt owns one contiguous
encoded response, SQL-error flag and verified transaction status. Its bytes stay
private until ReadyForQuery; delivery writes the complete representation without
another full-result copy. A failed/aborted attempt drops its output and backend
before an eligible retry. Writes/transactions keep their non-replay behavior.

Both existing limits remain: declared message size ≤16 MiB, encoded result ≤32 MiB.
The result capacity also stays ≤32 MiB instead of allowing default Vec growth to
overshoot it. Malformed ReadyForQuery status is rejected. TCP_NODELAY settings,
classifier outcomes, retry budgets, metrics boundaries and deployment defaults
are unchanged. A global result-memory bound and cancellation remain PERF-006 work.

Verification completed against the frozen production binary:

- 31 Rust tests passed, including five new wire/ownership/boundary tests.
- 65 offline Python tests and native static checks passed; complexity ≤26/30.
- Complete 7 failover + 8 long-query + 10 metrics + 4 buffering cases passed,
  zero failures/errors/skips. The buffering cases provision 10 and 100 clients,
  hold all backend owners at a barrier, verify exact multirow/NULL/client-specific
  data over five cycles each, recover after pipelined SQL error, and reject
  message/result overflow without exposing a byte of partial output.

```sh
nix develop --no-update-lock-file --command bash scripts/verify
nix develop --no-update-lock-file --command cargo build --release --locked --offline
export RUSTYBOUNCER_BIN=/home/publicuser/dev/rustybouncer-evidence/perf-wire-20261108/production
export METRICS_EVIDENCE=/home/publicuser/dev/rustybouncer-evidence/perf-wire-20261108/final/metrics
export BUFFER_EVIDENCE=/home/publicuser/dev/rustybouncer-evidence/perf-wire-20261108/final/buffering
nix develop --no-update-lock-file --command python3 tests/failover.py
nix develop --no-update-lock-file --command python3 tests/long_queries.py
nix develop --no-update-lock-file --command python3 tests/metrics.py
nix develop --no-update-lock-file --command python3 tests/buffering.py
```

Exact retained root: `/home/publicuser/dev/rustybouncer-evidence/perf-wire-20261108`.
Final accepted suite logs/configs/scrapes/observations are in `final/`;
`METRICS_EVIDENCE=.../final/metrics` and
`BUFFER_EVIDENCE=.../final/buffering`. Production executable is `production`;
separate symbol/frame-pointer executable is `profile`. Environment, binary/source
hashes and complete native commands are retained with the evidence.
[Accepted logs](perf-wire-evidence/final-regressions.log.gz),
[buffering observations](perf-wire-evidence/buffering-observations.json),
[raw runtime manifest](perf-wire-evidence/runtime-manifest.json.gz).

The initial additional buffering run failed three assertions because its observer
counted PostgreSQL's postgres-owned logical replication launcher as a client
backend. [Direct evidence](perf-wire-evidence/counter-diagnostic.txt) confirms that
background process. The observer now filters backend_type='client backend', as the
original comparison already did. The complete 29-case runtime scope reran fresh;
all failures and their original logs remain diagnostic evidence. No concurrency,
case or required behavior was removed.

Before PERF-002/003 can close: run the complete original 30-arm candidate
comparison, normalized/profile comparisons and small/large allocation/RSS study;
retain all repetitions and report throughput/tails/calls/bytes/ownership trade-offs.
The additional CTE/large/mixed baseline also remains open. No performance gain is
claimed by this checkpoint. The mandatory installed commit hook runs normally;
checkpoint hash and outcome are recorded at handoff.

After adding test-only Pending coverage, the final rebuilt production hash changed.
The full 29-case scope ran again against that frozen final hash and passed. Earlier
passes for the initial hash remain under `accepted/`; they do not substitute for
final-hash acceptance. [Provenance](perf-wire-evidence/provenance.json) records both.
