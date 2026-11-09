# PERF-004 — allocation-light conservative classification

Authority: [performance tasks](../tasks/pgbouncer-performance.md), PERF-004, and
[canonical checklist](../../FEATURE_COMPLETENESS.md), W01/F06/F07/S10.
This milestone implements classifier allocation reduction; it does not establish
new SQL purity guarantees, a throughput/tail SLO or broader release acceptance.

The lexer now summarizes byte tokens using a fixed 32-byte stack scratch buffer.
It no longer builds a Vec of uppercase owned strings. Unknown/long function
names retain conservative outcomes. The existing CTE AST validator is unchanged.
Classification remains independent of per-execution transaction pins, topology
generation and retry decisions. There is no cache, retained SQL or normalization.

A test-only frozen prior classifier checks **4,864 generated cases** (mutations,
long identifiers and ASCII inputs). Existing cases and explicit unsafe examples
preserve outcomes for comments/quotes/unknown functions, modifying or nested CTEs,
locking SELECTs, multi-statement SQL and primary-only statements. Full native
verification passes **34 Rust +72 Python tests**, zero failures/errors/skips;
Ruff, Astral ty, Clippy, Promtool, Grafana and complexity ≤30 pass.

## Measured costs and variability

The complete original **30/30** and one-proxy-CPU **30/30** matrices pass with zero
SQL errors. The full **16/16** final attribution matrix passes, and all eight
CPU/off-CPU reports record zero lost samples. Heap amortization includes startup,
preflight and probes: routed small lookups move from 10.150/10.078 allocation
calls/Q in the wire candidate to **2.149/2.076** at 10/100 clients. Allocated bytes/Q
move from 687.2/658.0 to **365.1/335.6**. These are lifetime amortizations, not
isolation of the lexer alone.

Isolated release classification measurements alternate legacy/candidate order
across three repetitions and traverse a deterministic permutation of 10,000
uniform literal keys or 1,000 CTE keys. All calls must retain Read outcomes;
no speed assertion or invented performance budget is enforced.

| Workload | Calls/run | All legacy ns | All candidate ns |
| --- | ---: | --- | --- |
| Literal lookup | 100,000 | 13,331,991; 14,112,971; 14,286,891 | 9,175,990; 9,470,228; 9,868,492 |
| CTE lookup | 10,000 | 49,999,123; 49,600,338; 49,593,043 | 49,176,645; 49,364,480; 49,004,098 |

Literal scanning is cheaper in these paired measurements; CTE AST work dominates
and its times are close. This is not a full-proxy throughput result.

| Placement | Clients | Target | Median Q/s | All Q/s repetitions | TPS CV | Median p99 ms |
| --- | ---: | --- | ---: | --- | ---: | ---: |
| Unrestricted | 10 | direct | 215,931 | 215,931, 139,310, 225,057 | 24.3% | 0.127 |
| Unrestricted | 10 | pgbouncer-session | 159,058 | 159,255, 100,960, 159,058 | 24.0% | 0.114 |
| Unrestricted | 10 | pgbouncer-transaction | 153,210 | 153,210, 155,252, 107,739 | 19.4% | 0.132 |
| Unrestricted | 10 | rustybouncer-session | 121,932 | 163,000, 121,932, 120,191 | 17.9% | 0.353 |
| Unrestricted | 10 | rustybouncer-routed | 147,273 | 150,189, 98,471, 147,273 | 22.0% | 0.158 |
| Unrestricted | 100 | direct | 304,859 | 304,859, 326,128, 303,811 | 4.0% | 2.018 |
| Unrestricted | 100 | pgbouncer-session | 136,686 | 123,346, 142,150, 136,686 | 7.2% | 1.623 |
| Unrestricted | 100 | pgbouncer-transaction | 147,802 | 149,020, 147,802, 144,404 | 1.6% | 1.460 |
| Unrestricted | 100 | rustybouncer-session | 180,535 | 180,535, 183,843, 169,725 | 4.1% | 1.570 |
| Unrestricted | 100 | rustybouncer-routed | 170,889 | 170,889, 176,671, 161,325 | 4.6% | 1.659 |
| One proxy CPU | 10 | direct | 223,832 | 223,832, 195,134, 226,325 | 8.1% | 0.115 |
| One proxy CPU | 10 | pgbouncer-session | 141,563 | 128,988, 141,563, 159,425 | 10.7% | 0.233 |
| One proxy CPU | 10 | pgbouncer-transaction | 121,814 | 121,814, 93,223, 155,885 | 25.4% | 0.266 |
| One proxy CPU | 10 | rustybouncer-session | 145,606 | 117,421, 145,606, 152,155 | 13.3% | 0.147 |
| One proxy CPU | 10 | rustybouncer-routed | 120,531 | 118,072, 120,531, 127,351 | 3.9% | 0.194 |
| One proxy CPU | 100 | direct | 284,676 | 284,676, 238,615, 330,439 | 16.1% | 2.295 |
| One proxy CPU | 100 | pgbouncer-session | 154,845 | 154,845, 120,126, 155,010 | 14.0% | 1.115 |
| One proxy CPU | 100 | pgbouncer-transaction | 151,713 | 151,978, 108,826, 151,713 | 18.1% | 1.312 |
| One proxy CPU | 100 | rustybouncer-session | 124,735 | 150,211, 97,495, 124,735 | 21.2% | 1.871 |
| One proxy CPU | 100 | rustybouncer-routed | 98,617 | 98,617, 83,581, 124,619 | 20.3% | 2.937 |

Host verification/builds and owned functional fixtures overlapped parts of these
runs. Every repetition is retained, including the slow 98,471-Q/s 10-client run.
Normalized 100-client routed TPS ranges 83,581–124,619; p99 ranges 1.387–5.171ms.
These observations do **not** establish a separate classifier throughput or tail
improvement over the wire candidate. Ranges/CV describe a shared host, not
confidence intervals or production behavior. Final uncontaminated combined
comparisons remain PERF-009 work. The historical session target means opaque
passthrough; all targets still reach 10/100 physical backends at those loads.

## Runtime acceptance and profiling diagnostic

The frozen production hash passed all **7 failover +8 long-query +10 metrics +4
buffering =29 live cases**, zero failures/errors/skips. Buffering covers three
complete load/disconnect cycles at 10 and 100 clients, exact rows/NULL/client
identities, ordering, overflow boundaries and no partial result exposure.

The first integrated attribution attempt stopped on a 100-client passthrough
shutdown timeout after two completed profiles. It is not accepted as a complete
matrix. Its raw data/logs remain archived; the entire 16-arm attribution matrix
was repeated fresh. The 30+30 timing matrices had already completed successfully
and are retained unchanged. Cleanup still requires exit status zero and the
original 20-second bound; a second owned-process/group SIGINT fits inside that
bound. Safety tests verify fallback and forced termination remain failures.
Tokio documents that ctrl_c registers on first poll and receives later signals;
recreating that receiver each select iteration can leave a listener gap. The
exact lost event was not observed in the failed capture. PERF-006 separately
pins a persistent shutdown future and tests one-signal shutdown during churn.

## Reproduction and retained evidence

Production SHA256: `4dea55a0cdb615f3bcad2276c3fedb5a841eb0e7f6cfb5ca2fbfeca23c041ebb`.
Debug/frame-pointer SHA256: `886eb7f74cb081009a48e6f73ef967ea18e7e3c6ac3ab6b3fd49e07dd2001d8b`.
Root: `/home/publicuser/dev/rustybouncer-evidence/perf-classifier-20261108`.
[Full summaries](perf-classifier-evidence/summary.json),
[source/binary hashes](perf-classifier-evidence/sources.json),
[raw profile archives](perf-classifier-evidence/raw-archives.json),
[all raw file hashes](perf-classifier-evidence/raw-manifest.json.gz),
[release microbench output](perf-classifier-evidence/build.log.gz),
[native verification](perf-classifier-evidence/acceptance-native.log.gz).

```sh
nix develop --no-update-lock-file --command bash scripts/verify
nix develop --no-update-lock-file --command cargo test --release --locked --offline -- --nocapture
nix develop --no-update-lock-file --command python3 benchmarks/compare.py --output NEW_ORIGINAL --binary FROZEN_PRODUCTION
nix develop --no-update-lock-file --command python3 benchmarks/performance_profile.py --output NEW_NORMALIZED --binary FROZEN_PRODUCTION --profile-binary FROZEN_PROFILE
nix develop --no-update-lock-file --command python3 benchmarks/attribution.py --output NEW_ATTRIBUTION --binary FROZEN_PROFILE
# Run the entire tests/failover.py, long_queries.py, metrics.py and buffering.py
# with RUSTYBOUNCER_BIN=FROZEN_PRODUCTION and fresh metrics/buffer evidence roots.
```

Installed locked Nix verification must pass normally before this milestone commit.
Task registration records the actual commit and hook log. PERF-005–009 and all
broader canonical acceptance remain open.
