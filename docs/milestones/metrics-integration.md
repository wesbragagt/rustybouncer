# Prometheus metrics integration

Authority: [FEATURE_COMPLETENESS.md](../../FEATURE_COMPLETENESS.md), particularly
D04, H11, S10–S12, H06/F11 and Q01–Q08. This report follows the
[adversarially reviewed plan](../plans/prometheus-metrics.md). Existing checklist
items retain their full scope; this is the router metrics milestone, not release
conformance or shared-pool implementation.

## Integrated behavior

`metrics_listen` enables a process-owned registry and private HTTP listener;
omission selects a no-op context. The endpoint has a two-second connection
budget, 8-KiB header limit, 16 scrape tasks, no keepalive, and fail-fast listener
supervision. It exports the documented 117+29*N series with finite labels,
classic histogram buckets, typed terminal outcomes and abort-safe live gauges.

Logical frontend Q messages, backend dispatch attempts and read retry selections
are separate. SQL errors, router errors, delivery errors and external aborts are
visible. Passthrough exposes session/backend observations without inferring SQL
throughput. Probes are excluded from frontend work; topology freshness advances
for unchanged rounds. No query text, credentials or client identities are labels.

The [operations guide](../metrics.md), private scrape examples and 24 rules are
integrated. The [Grafana recommendations](../grafana.md) include the shipped
[14-panel Rustybouncer Overview](../../deploy/grafana/rustybouncer-overview.json)
and complementary CNPG, postgres_exporter, Kubernetes and node_exporter dashboards.
All 22 dashboard PromQL targets are parsed by Promtool in normal verification.
External dashboards retain their documented exporter/label requirements; a live
Grafana UI import is not asserted by the JSON/PromQL checks.

## Worktree review and merge ledger

All delegates used tool-provisioned worktrees registered with workmux. Reviewed
branches were merged sequentially with `--keep`; pre-merge locked Nix checks
passed and retained branches remain available.

| Track | Implementation/checkpoint | Root integration |
| --- | --- | --- |
| Operations | `e570252e09cc7a6176421f9084e3150c91486293` | `8575566` fixes passthrough topology alerts; `a1b7583` enforces Promtool |
| Validation/benchmark | `b88a82d501a56270b82e639225b79f80192b3e96` | merge `dae11a9`; review corrections `2ada3fa` |
| Runtime | `61c8c2fab089f93490c25d3c73a9e36c919e8d70` | workmux merge `3fb7eb8` |

Coordinator corrections preserve the planned contract: dedicated probe identity
for independent counts, all-state backend teardown checks, partial preflight
cleanup, timeout diagnostics, passthrough route-wait accounting, fresh evidence
paths, real write classification and the HTTP test's client lifetime. Required
benchmark concurrency/durations/repetitions were not reduced. Ruff/ty now cover
all benchmark files. Default generated evidence directories are excluded from
Git and Nix package inputs.

## Environment and reproducible commands

Locked flake.lock on x86_64 Linux. Full external evidence is retained at
`/home/publicuser/dev/rustybouncer-evidence/metrics-integration-20261108`.
The immutable release binary used for native tests and every benchmark arm has
SHA-256 `b58acae3b99be386af2640bdca00dc6efd1f7e42510655bdd2d9531d60a9bdab`.
[Environment and source hashes](metrics-integration-evidence/environment-parent.json)
record exact source hashes and PostgreSQL image digest. The host also runs other
development workloads; no exclusive CPU isolation or causal overhead estimate is
claimed. The measured workload is one simple-protocol SELECT per pgbench
transaction, so transaction throughput equals Q-message throughput in these runs.

From the repository root, choose fresh output directories and set the evidence
path and binary as above. The literal directory below retains these runs:

```sh
nix develop --no-update-lock-file --command bash -c 'cargo fetch --locked && cargo build --release --locked --offline'
mkdir -p /home/publicuser/dev/rustybouncer-evidence/metrics-integration-20261108
cp target/release/rustybouncer /home/publicuser/dev/rustybouncer-evidence/metrics-integration-20261108/rustybouncer-release
nix develop --no-update-lock-file --command env RUSTYBOUNCER_BIN=/home/publicuser/dev/rustybouncer-evidence/metrics-integration-20261108/rustybouncer-release METRICS_EVIDENCE=/home/publicuser/dev/rustybouncer-evidence/metrics-integration-20261108/metrics-run-2 python3 tests/metrics.py
nix develop --no-update-lock-file --command env RUSTYBOUNCER_BIN=/home/publicuser/dev/rustybouncer-evidence/metrics-integration-20261108/rustybouncer-release METRICS_BENCH_EVIDENCE=/home/publicuser/dev/rustybouncer-evidence/metrics-integration-20261108/benchmark-run-1 python3 benchmarks/metrics.py
nix develop --no-update-lock-file --command env RUSTYBOUNCER_BIN=/home/publicuser/dev/rustybouncer-evidence/metrics-integration-20261108/rustybouncer-release bash -c 'python3 tests/failover.py && python3 tests/long_queries.py'
nix develop --no-update-lock-file --command env RUSTYBOUNCER_BIN=/home/publicuser/dev/rustybouncer-evidence/metrics-integration-20261108/rustybouncer-release PROMETHEUS_BIN=/nix/store/sabjm70myixy8p76cs2xb4n928ml5w6x-prometheus-3.15.1/bin/prometheus PROMETHEUS_EVIDENCE=/home/publicuser/dev/rustybouncer-evidence/metrics-integration-20261108/prometheus-run-1 python3 tests/prometheus_smoke.py
nix build .#docker --no-update-lock-file --out-link result-image --print-build-logs
docker load -i result-image
nix develop --no-update-lock-file --command python3 -m tests.kind_cnpg --output /home/publicuser/dev/rustybouncer-evidence/metrics-integration-20261108/kind-run-2
nix flake check --no-update-lock-file --print-build-logs
```

For a different machine, obtain the server path from the pinned input rather than
copying the store path: `nix eval --raw --impure --expr 'let f = builtins.getFlake
(toString ./.); p = import f.inputs.nixpkgs { system = builtins.currentSystem; };
in "${p.prometheus}/bin/prometheus"'`. The shell supplies promtool; the server
output is a separate optional tool for this smoke test. Build/fetch that locked
output if it is not already present. All listeners in the smoke test are loopback
and all fixtures/processes belong to the invocation.

## Native and Prometheus results

- [Ten-case metrics report](metrics-integration-evidence/metrics-results.json):
  **10 passed**, zero failures/errors/skips/expected failures/unexpected successes,
  82.525s. [Complete unittest output](metrics-integration-evidence/metrics-unittest.log.gz).
  Real primary/physical replicas, termination, promotion, transaction/write
  non-replay, exact outcomes, independent connection counts, malformed framing,
  passthrough, fixed cardinality, freshness and HTTP capacity/deadline abuse.
- [Regression output](metrics-integration-evidence/regressions.log.gz): all
  **7 failover cases** passed in 60.726s; all **8 long-query cases** passed in
  64.092s. No skips/failures/errors.
- [Prometheus ingestion result](metrics-integration-evidence/prometheus-results.json):
  real Prometheus 3.15.1 ingested exactly **50 additional Q messages** and evaluated
  shipped throughput/p95 recording rules; primary/replica counts matched. Passed
  in 14.405s. This checks ingestion and rule evaluation, not external notification
  delivery or Grafana browser rendering.

The first metrics development run had two harness assertion failures: a sleeping
INSERT was conservatively classified stateful, and the HTTP case retained its
own SQL client before asserting zero live gauges. Changed the stimulus to a
recognized INSERT blocked by an explicitly held table lock, and closed that SQL
client. The full ten-case suite was rerun unchanged in scope and passed.
[Original failed report](metrics-integration-evidence/development-metrics-results.json)
and [tracebacks](metrics-integration-evidence/development-metrics-unittest.log.gz)
remain diagnostic evidence, not acceptance.

## Benchmark observations

All **18 measured arms** completed: 10/100 clients, disabled/no-op versus enabled
without scrapes versus enabled/1Hz, three alternating-order repetitions, 5s warmup
and 30s measurement per arm. Independent PostgreSQL connection observations run
every five seconds in every treatment. Each arm preflights the full client count.
Raw pgbench logs randomly sample 10% of transactions; error totals come from the
full pgbench output. Latency quantiles below are medians of the three per-run
sample quantiles, not pooled percentiles.

| Clients | Metrics mode | Median Q/s | Median p50 ms | Median p95 ms | Median p99 ms | Peak observed backend streams |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| 10 | disabled | 74,984 | 0.111 | 0.244 | 0.386 | 30 |
| 10 | enabled | 74,977 | 0.112 | 0.243 | 0.382 | 30 |
| 10 | scraped | 75,122 | 0.111 | 0.241 | 0.376 | 30 |
| 100 | disabled | 104,216 | 0.869 | 1.757 | 2.623 | 300 |
| 100 | enabled | 103,702 | 0.877 | 1.753 | 2.595 | 300 |
| 100 | scraped | 104,084 | 0.879 | 1.737 | 2.545 | 300 |

**48,138,418 successful measured transactions**, zero query errors and zero scrape
errors. At 100 clients, medians are close across treatments; this shared-host
three-repetition observation is not a statistical equivalence result or a
performance SLA. Query mix is a small cached SELECT workload, not sustained
transaction/1,000-client or prepared-statement acceptance.

[Interactive chart](metrics-integration-evidence/benchmark.html),
[all arm results](metrics-integration-evidence/benchmark-results.json),
[summary](metrics-integration-evidence/benchmark-summary.json), and
[environment](metrics-integration-evidence/benchmark-environment.json) are committed.
[Raw artifact hashes](metrics-integration-evidence/benchmark-raw-manifest.json.gz)
identify 1,142 retained files outside the source tree, including per-transaction
latencies, pgbench output, per-node connection observations, every HTTP scrape and
fixture logs/configurations. Connection minima can include the measured process's
startup; reported peaks are observations, not configured limits.

## Kubernetes and final verification

[Full Kubernetes report](metrics-integration-evidence/kind-results.json.gz) and
[JUnit](metrics-integration-evidence/kind-junit.xml) and
[readable summary](metrics-integration-evidence/kind-summary.json): **6 passed** in 553.676s,
zero failures/errors/skips/expected failures/unexpected successes. The updated
non-root distroless image passed the metrics case plus all five original cases.
Promtool parsed the in-cluster endpoint's snapshots; SELECT/CTE produced exactly
two successful replica dispatches, observed node labels matched CNPG and live
gauges returned to zero. Actual worker SIGKILLs caused two promotions
postgres-1 → postgres-2 → postgres-1. All 1,374 continuous reads succeeded on
one frontend before a write reached the promoted primary. Interrupted transaction
marker count remained zero. [Events](metrics-integration-evidence/kind-events.jsonl.gz)
and [complete output](metrics-integration-evidence/kind.log.gz) are retained.

The first Kubernetes attempt failed setup because Deployment readiness preceded
Service connectivity to CNPG's admission webhook. A bounded retry of the owned,
idempotent declarative manifest application handles that startup boundary.
The entire six-case suite was then rerun; no case was filtered or skipped.
[Failed setup report](metrics-integration-evidence/development-kind-results.json)
and [log](metrics-integration-evidence/development-kind.log.gz) remain diagnostic.
The owned clusters were deleted and private kubeconfigs removed. Existing user
clusters were untouched. The kind default CNI does not establish enforcement of
the example NetworkPolicy; no network isolation certification is claimed.

Normal staged-snapshot verification is required before the integration commit:
26 Rust tests, 52 offline Python tests, all three scrape configs, 24 rules/35
synthetic scenarios and all 22 Grafana PromQL targets, plus formatting, Clippy,
Ruff, ty, complexity ≤30, ShellCheck and Actionlint. Actual final hook result and
commit hash are reported at handoff; the hook is not bypassed. The preceding
image build already passed its full native check. Non-native platforms and hosted
CI remain outside this observed execution scope.

The benchmark HTML is retained as a local artifact. T3's screenshot preview was
unavailable because the host lacks its browser shared libraries; no browser-render
verification is asserted. Data, medians and ranges are also available in JSON and
the table above. Core metric/Prometheus/Kubernetes validation does not depend on
that optional visualization.

D04 still includes shared-pool statistics, H11 includes wider conformance evidence,
and S10–S12 retain their complete required workload scope. These observations do
not establish 1,000-client, mixed/prepared/long-running-load, arbitrary partition,
unfenced split-brain, cancellation or full deployment/release conformance.
The existing canonical checkboxes remain unchanged.
