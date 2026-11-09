# Operating Prometheus metrics

This is the operations companion to the [reviewed contract](plans/prometheus-metrics.md).
The [canonical checklist](../FEATURE_COMPLETENESS.md) remains authoritative:
these assets support D04, H11, S10–S12, F11/F12 and Q01–Q08 without establishing
their broader acceptance. Runtime metrics are opt-in; the operations evidence
validates PromQL against synthetic series, independently of live endpoint tests.

Start with the [Grafana dashboard recommendations](grafana.md), including the
shipped [Rustybouncer Overview](../deploy/grafana/rustybouncer-overview.json).

## Measurement unit and privacy

Routed throughput counts frontend simple-protocol **Query (Q) messages**, including
fully framed rejected or malformed bodies. One message may contain several SQL
statements. It is neither statements/second nor committed transactions/second.
Passthrough is opaque: session/connection observations remain useful, but query
panels and alerts require routing_enabled=1. Probes never count as user work.

Metrics intentionally omit SQL, fingerprints, credentials, users, database names,
client addresses, backend PIDs, DSNs, raw SQLSTATEs and arbitrary error text.
Configured node names and build versions are visible: choose nonsecret stable node
names. Prometheus itself adds job/instance and the deployment discovery_profile
label; these are target metadata, not additions to the runtime contract. Restrict
access to Prometheus storage, backups, remote write and dashboards as well.

## Exact runtime contract

The following inventory and accounting boundaries reproduce the reviewed contract.
Changes must be coordinated with runtime and validation owners first.

- Optional top-level `metrics_listen` string; absent means disabled for backward
  compatibility. Disabled means a no-op runtime metrics context: no counters,
  histogram recording or HTTP listener. Enabled means full instrumentation and
  exposure. Examples use `127.0.0.1:9090`; container examples explicitly use
  `0.0.0.0:9090` behind private networking. No public host publication by default.
- Bind the metrics listener before accepting PostgreSQL clients. Bind failure is
  a startup error; unexpected listener-task exit terminates the process with an
  error so an apparently healthy instance cannot silently lose observability.
- `GET /metrics` returns Prometheus text 0.0.4 with correct content type, HELP/TYPE,
  properly escaped labels and fixed histogram boundaries. Unknown paths return
  404, unsupported methods 405. No TLS/auth/admin mutations on this endpoint;
  deploy private access or an authenticated TLS proxy/NetworkPolicy.
- Use a maintained Rust Prometheus client library and a bounded HTTP implementation.
  Per-connection read/write deadline 2 seconds, request headers at most 8 KiB,
  at most 16 concurrent scrapes, no unbounded accept-task queue, no keepalive.
  Saturation closes excess connections. Scrapes only snapshot local counters;
  no database queries, SQL parsing, topology waits or network probes on scrape.
- A process-owned registry is passed explicitly through runtime context, with no
  global recorder and no lock held over await. RAII guards decrement live gauges
  when tasks error, disconnect, time out, or are aborted. Instrumentation must not
  change query routing, retries, wire output, timeouts or write replay policy.
- Preinitialize finite enums and configured node series to zero. Node labels come
  only from configured names, bounded by configuration size. Never include SQL,
  fingerprints, users, databases, client addresses, backend PIDs, DSNs, secrets,
  arbitrary error text, raw SQLSTATE or dynamically generated identifiers.

## Initial metric inventory and exact boundaries

All names below have prefix `rustybouncer_`. Allowed label values are contract
values, not formatted error strings. No unlisted labels may be added implicitly.

| Family | Type/labels | Meaning |
| --- | --- | --- |
| `routing_enabled` | gauge | 1 when route_selects is enabled, otherwise 0; gates query panels and alerts |
| `build_info` | gauge, `version` | Constant 1 with compile-time package version |
| `clients_accepted_total` | counter | SQL admission sockets, including startup; reserved control-only sockets are excluded unless rejected as excess SQL clients |
| `clients_rejected_total` | counter | Accepted socket rejected for configured client capacity |
| `clients_active` | gauge | Admitted session tasks, including startup; RAII lifetime |
| `client_capacity` | gauge | Configured max_clients; no claim of backend pool size |
| `sessions_completed_total` | counter, `outcome=ok\|error\|aborted` | One terminal classification per admitted session; explicit proxy FATAL/rejection is error even when the handler returns Ok; ok is normal session termination, not query success |
| `session_duration_seconds` | histogram | Admission until session exit, including startup and abort |
| `backend_connections_active` | gauge, `node` | Live locally owned TCP backend connections, including setup, active leases, resets and shared idle backends; counts locally held streams, not proven live PostgreSQL sessions; excludes probes; passthrough and routed caches both counted |
| `pooling_enabled` | gauge | 1 for an enabled shared pool, 0 for none |
| `pooling_mode` | gauge, `mode=none\|session\|transaction` | One-hot configured mode; transaction is reserved and rejected until PERF-008 |
| `pool_backend_capacity` | gauge | Process-wide physical backend reservation limit; 0 when pooling is disabled |
| `pool_size` | gauge | Base per-exact-identity/node/generation capacity; 0 when disabled; reserve capacity is configured separately |
| `pool_reuses_total` | counter | Healthy idle backend checkouts across frontend owners |
| `pool_discards_total` | counter | Explicit unhealthy idle/return/reset discard decisions; does not count every task-abort socket close |
| `pool_resets_total` | counter, `outcome=ok\|error\|aborted` | Owned rollback/DISCARD/initialization reset attempts; setup/role failures never replay frontend work |
| `pool_reset_duration_seconds` | histogram | Reset wall time, including failed/aborted resets; excludes user execution |
| `backend_connects_total` | counter, `node,outcome=ok\|error\|aborted` | TCP connection attempts completed or failed, including timeouts; externally dropped unfinished attempts are aborted; ok means TCP connected, not authenticated |
| `queries_total` | counter, `kind=read\|write\|transaction\|stateful\|unsupported\|malformed`, `outcome=ok\|sql_error\|router_error\|delivery_error\|aborted` | Exactly once per fully received, validly framed routed Q message, including rejected COPY/malformed input; successful retry counts once; no passthrough series increments |
| `queries_in_flight` | gauge | Observed routed Q messages without terminal outcome |
| `query_duration_seconds` | histogram | Full routed Q receipt through final response delivery or terminal failure, including bounded frontend queue wait; quantiles mix all outcomes; includes routing/setup/retries and client backpressure |
| `query_attempts_total` | counter, `route=primary\|replica`, `outcome=ok\|sql_error\|error\|aborted` | Dispatch attempts, starting immediately before sending frontend query bytes; internal SET excluded; role at dispatch; SQL ErrorResponse is sql_error even if ReadyForQuery is received |
| `control_requests_total` | counter | Parsed CancelRequest packets, including unknown/wrong identities; no credential labels |
| `result_buffer_limit_bytes` | gauge | Configured process-wide bulk result capacity; excludes documented fixed frontend allowance/runtime overhead |
| `result_buffer_reserved_bytes` | gauge | Leased slab capacities, including slabs being allocated; zero after cleanup |
| `result_buffer_allocated_bytes` | gauge | Lazily allocated slab capacity, including leased and cached storage; retained within the limit after warmup |
| `result_buffer_highwater_bytes` | gauge | Largest observed leased slab capacity since startup; never exceeds limit |
| `result_buffer_rejections_total` | counter | Fail-fast bulk capacity rejections; no backend retry |
| `read_retries_total` | counter | Second and later eligible read selections actually begun, including setup failure; does not count merely deciding a retry is possible |
| `route_wait_duration_seconds` | histogram | Each wait_route/wait_backend selection phase, both successes and errors; excludes backend setup/execution |
| `topology_nodes` | gauge, `role=primary\|replica\|unavailable` | Last completed probe-round classification; one primary only when unambiguous; unavailable includes noneligible nodes |
| `node_role` | gauge, `node,role=primary\|replica\|unavailable` | One-hot eligibility per configured node; ambiguous writable candidates are unavailable, not elected primaries |
| `topology_ambiguous` | gauge | 1 when last round observed multiple writable nodes |
| `topology_discovery_completed` | gauge | 0 until first completed round, then 1 |
| `topology_last_completed_timestamp_seconds` | gauge | Unix time of last completed discovery round; 0 before first round |
| `topology_changes_total` | counter | Published topology changes, including initial discovery; not a claim of completed failover |
| `primary_changes_total` | counter | Change of selected primary identity, including transitions to/from none; not replica membership-only changes |
| `probes_total` | counter, `node,outcome=ok\|error` | Completed individual discovery probes; failure/timeout is error |
| `probe_duration_seconds` | histogram, `node` | Individual probe wall time, including timeout |

Histogram boundaries in seconds: 0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25,
0.5, 1, 2.5, 5, 10, 30, 60, 120, 300, 600 plus +Inf. Counters are monotonic
within one process lifetime; process restarts reset counters. Every completed discovery round updates freshness and node eligibility, even
when no topology change is published. Preinitialize all three per-node roles.
With N configured nodes, the inventory has 154 + 29*N exposed sample series
(including histogram buckets/sum/count; excluding HELP/TYPE). Disable implicit
library collectors/created timestamps or explicitly revise and test this budget.
Cross-family scrape snapshots are not atomic; reconciliation assertions run after quiescence.

A truncated/oversized frame is a session protocol error and is not a query
observation; a fully framed invalid UTF-8 or invalid NUL body is a malformed query.
A session error includes explicit startup-unavailable and unsupported-protocol
FATAL replies even if existing control flow returns Ok. Clean termination is ok;
external task cancellation/panic is aborted. Do not classify arbitrary handler
Result values without preserving these explicit terminal dispositions.

Topology, deadline and transport interruption finalize attempts as error even
when tokio select drops an execution future; aborted is reserved for external task
cancellation/panic. Start the dispatch guard inside the execution future when it
is first polled, immediately before sending query bytes. TCP connect guards obey
the same distinction. Explicit typed outcomes must replace error-text guessing.

SQL ErrorResponse anywhere in a buffered response marks sql_error, including
multi-statement messages and failed transactions. Terminal transport/delivery
failure takes precedence over buffered SQL outcome. An unfinished guard records
aborted, never success. An outcome-unknown write contributes router_error; metrics
must not assert that it rolled back or that replay is safe. Actual retry reason
classification and transaction commit/rollback counts are deferred until typed
runtime events can distinguish them without guessing from error strings.


## Enable and scrape privately

The default is disabled: omit metrics_listen. Existing deployment manifests
continue to leave it disabled. Add this top-level TOML setting to a private copy of your actual configuration,
before any [[nodes]] tables:

```toml
metrics_listen = "127.0.0.1:9090"
```

For native same-host Prometheus, use
[the native scrape configuration](../deploy/prometheus/prometheus.yml).
From the repository root:

```sh
nix develop --no-update-lock-file --command promtool check config deploy/prometheus/prometheus.yml
# Requires a Prometheus server installed separately; prometheus.cli supplies promtool only.
prometheus --config.file=deploy/prometheus/prometheus.yml --storage.tsdb.path=/var/lib/prometheus --web.listen-address=127.0.0.1:9091
curl --fail http://127.0.0.1:9090/metrics
```

Do not expose 9090 on a public interface. The endpoint has no TLS, authentication
or administrative functions. For remote scraping, use an explicitly private
interface/firewall, a private container network or a NetworkPolicy-enforced
cluster network. If that trust boundary is insufficient, place an authenticated
TLS proxy in front and configure Prometheus credentials outside version control.
Scrape health (up) proves HTTP collection only, never successful SQL execution.

### Docker Compose

[compose.yml](../deploy/prometheus/compose.yml) runs only a private scraper,
using [prometheus.compose.yml](../deploy/prometheus/prometheus.compose.yml).
Create a dedicated network and attach an existing owned router container:

```sh
docker network create --internal rustybouncer-metrics
docker network connect --alias rustybouncer rustybouncer-metrics ROUTER_CONTAINER
docker compose -f deploy/prometheus/compose.yml up -d
```

Replace ROUTER_CONTAINER with the real container name. In a private copy of its
mounted TOML, set metrics_listen="0.0.0.0:9090" at top level and recreate the router
with that copy. The router retains its database network; the internal metrics
network needs no outbound Internet access. For repeatable deployment, declare the
same external network and alias in your local router Compose overlay. Neither
router port 9090 nor Prometheus port 9090 is published. Do not add a host ports
mapping to make the scrape work. Pull the scraper image before offline use; pin a
reviewed digest in production. This is an example image tag, not supply-chain
attestation. No existing Compose files need to be changed in this milestone.

### Kubernetes / CloudNativePG

CNPG manages PostgreSQL, while these metrics describe rustybouncer. Use
[cnpg.yml](../deploy/prometheus/cnpg.yml) for a ClusterIP Service and an example
restrictive ingress policy, and
[prometheus.cnpg.yml](../deploy/prometheus/prometheus.cnpg.yml) for a plain
Prometheus scrape job. No ServiceMonitor, PodMonitor or Prometheus Operator CRD
is required. Mount the config and rules into your existing Prometheus server in
the monitoring namespace with pod label app=prometheus.

First copy the router ConfigMap TOML, add top-level
metrics_listen="0.0.0.0:9090", and apply your local copy of the ConfigMap.
Restart the router deployment to reload it. Configure real SQL client selectors
in the example policy before applying: the supplied app=sql-client selector permits
same-namespace clients only; other clients will be blocked unless separately
allowed. NetworkPolicies are additive: audit other allow policies, which may
reopen 9090. Use a CNI that enforces them; ClusterIP alone is not access control.

```sh
# After applying your local metrics-enabled ConfigMap and reviewing policy selectors:
kubectl -n rustybouncer-demo rollout restart deployment/rustybouncer
kubectl -n rustybouncer-demo rollout status deployment/rustybouncer
kubectl apply -f deploy/prometheus/cnpg.yml
```

This single-Service target is appropriate only for the supplied one-replica router.
For multiple router replicas, discover and scrape each pod IP independently
(plain kubernetes_sd_configs role: pod with port/app relabeling, or static per-pod
targets). Do not scrape a load-balanced Service as though it were one process:
counter resets, mixed gauges and missing pods would be concealed. Apply the
discovery_profile target label to every discovered instance. CNPG/PostgreSQL
exporters and end-to-end SQL probes remain separate requirements.

## Recording rules, interpretation and alerts

[rules.yml](../deploy/prometheus/rules.yml) uses five-minute rates calculated
before aggregation, so process counter resets are handled per series. Aggregation
is per job/instance; retain per-node raw series for diagnosis. Quantiles are
histogram_quantile over sum by(job,instance,le) bucket rates, with all outcomes
mixed. They include routing, setup, retries, execution, response delivery and client
backpressure; they do not isolate successful-query backend time. Empty windows
omit ratios and quantiles rather than claim perfect availability.

Useful queries (seconds unless otherwise stated):

| Purpose | PromQL |
| --- | --- |
| Q messages/second | rustybouncer:q_messages_per_second:rate5m |
| Router/delivery errors per Q | rustybouncer:router_delivery_error_ratio:rate5m |
| SQL/application errors per Q | rustybouncer:sql_error_ratio:rate5m |
| External aborts per Q | rustybouncer:aborted_ratio:rate5m |
| Mixed-outcome p50 | rustybouncer:q_duration_p50_seconds:rate5m |
| Mixed-outcome p95 | rustybouncer:q_duration_p95_seconds:rate5m |
| Mixed-outcome p99 | rustybouncer:q_duration_p99_seconds:rate5m |
| Admitted clients / max_clients | rustybouncer:client_utilization:ratio |
| Locally owned backend streams | rustybouncer:backend_connections:sum |
| Attempts per Q (not always >=1) | rustybouncer:attempts_per_q_message:rate5m |
| Additional read selections / read Q | rustybouncer:read_retry_amplification:rate5m |
| Failed/aborted attempts / all attempts | rustybouncer:attempt_failure_ratio:rate5m |
| Seconds since a completed discovery round | rustybouncer:discovery_age_seconds |
| Eligible configured nodes by role | rustybouncer_node_role == 1 |
| Last round role counts | rustybouncer_topology_nodes |

Attempts per Q can be below one because malformed/unsupported queries and setup
failures need not dispatch. Additional read selections include failed setup and
can exceed dispatch retries. A successful read after retry increments one
queries_total outcome=ok but can include failed attempts. Compare attempt failures
with client-visible errors rather than treating them as equal. Aborted and SQL
ratios are separate from the router/delivery alert, never silently counted as
availability success. An uncertain write is router_error, not proof of rollback.

Client capacity is admission capacity, not a shared backend pool or queue-depth
metric. Active backend streams may outlive remote sockets until locally detected.
Node-role gauges encode eligibility, not merely PostgreSQL role: ambiguous
writable candidates are unavailable. topology_changes_total and
primary_changes_total do not measure recovery duration. A stable healthy topology
need never increment either counter, while its completed timestamp must advance.

Alerts are example thresholds, not an SLO: scrape down/missing for 1m, no primary
or ambiguous after discovery for 1m, initial discovery pending for 1m, stale
discovery for 30s, >90% admission for 5m, >5% router/delivery errors and >20%
additional read selections for 5m with >1 Q/s. Brief promotions should remain
pending. Query alerts require routing enabled and up=1. Topology, discovery and
capacity alerts apply to both routed and passthrough modes: both depend on
primary discovery. These alerts require up=1. Missing all targets and a missing
routing contract gauge
have separate alerts. Per-target up=0 catches known failed targets. To detect one
target removed from service discovery while others remain, maintain an external
expected-instance inventory and compare it to up; absent(up) only detects an
entirely missing job. No expected-target metric is invented here.

The supplied discovery_profile=interval500ms_timeout1000ms selects a 15s stale
threshold. Current concurrent probes complete within the 1s probe timeout; the
next round follows a 0.5s sleep. With 5s scrape/evaluation intervals, a conservative
budget is 1+0.5+2*5=11.5 seconds, rounded up to 15 for scheduling jitter. This is
not derived from an invented runtime cadence metric. If interval, timeout,
scrape cadence or probe execution strategy changes, update both the matching
profile label and rule selector/threshold together, recalculate the budget and
rerun the tests. An unmatched/missing profile alerts after 1m. A 30s pending period
adds to the stale threshold. Startup completion is separately gated; timestamps
are wall time and require synchronized clocks. Rules use completion timestamp,
never topology changes, to detect stale unchanged topology.

Use Alertmanager routing/inhibition for duplicate no-primary/ambiguous or
scrape-related notifications. Supply your own receiver and contact policy;
these assets do not send notifications. Add end-to-end SQL, CNPG lag/WAL,
PostgreSQL, node CPU/memory/storage monitoring for actual service assurance.

## Reproducible rule verification

Run from the repository root with the checked-in flake.lock. The repeatable
wrapper is `nix develop --no-update-lock-file --command bash deploy/prometheus/check.sh`;
it checks all three scrape configurations, rules and unit fixtures. Individual commands:

```sh
nix develop --no-update-lock-file --command promtool --version
nix develop --no-update-lock-file --command promtool check config deploy/prometheus/prometheus.yml
nix develop --no-update-lock-file --command promtool check config deploy/prometheus/prometheus.compose.yml
nix develop --no-update-lock-file --command promtool check config deploy/prometheus/prometheus.cnpg.yml
nix develop --no-update-lock-file --command promtool check rules deploy/prometheus/rules.yml
nix develop --no-update-lock-file --command promtool test rules deploy/prometheus/rules.test.yml
nix flake check --no-update-lock-file
```

The JSON-form YAML test file is directly accepted by promtool; no fixture generator
or Python dependency is required. Tests cover firing and suppressed cases,
no traffic, SQL-only errors, counter resets, disabled routing, missing targets,
missing contract, initial discovery, transient/sustained ambiguity and no primary,
stale/fresh unchanged topology, capacity and retry/error amplification. A mixed
histogram checks p50/p95/p99; positive traffic and SQL ratio values are asserted.
Synthetic timestamps start at Unix zero, matching promtool's test clock.

See [operations evidence](milestones/metrics-operations.md) for exact results and
live integration limitations. These checks validate configuration/PromQL, not the
future runtime endpoint or deployment reachability.

## Cancellation and bounded result storage

PERF-006 adds a 64-MiB default bulk-result capacity (`result_buffer_bytes`, minimum
64 KiB, a whole slab multiple), 30-second frontend delivery deadline (`delivery_timeout_ms`) and five-second
cancellation cleanup deadline (`cancel_cleanup_timeout_ms`). These are positive
configuration values, independent of route/execution/retry budgets. Bulk storage uses reusable 64-KiB slabs: allocated includes both leased and cached
storage, while reserved counts currently leased slabs. Both remain within the
configured footprint; cached storage is not treated as free budget. Capacity errors return FATAL 53200 and discard the backend before any
result exposure; they never trigger a read retry or imply a dispatched write
rolled back. Allocator-retained pages are not an RSS hard limit.

A bounded reader remains active during execution/delivery: at most eight queued
frames and 16 MiB of active-plus-queued bodies per frontend, plus one incoming
16-MiB frame. Fully received Q messages begin accounting immediately, including
queued messages later rejected on disconnect/overflow. Internal cancellation and
protocol rejection finalize explicit outcomes; external task abort remains
`aborted`. Two 256-byte private response allowances per frontend cover ordinary
small output and the cancellation synchronization barrier. Backend read buffers,
startup/configuration data, queue metadata, allocator/runtime/socket overhead and
probe tasks are separate fixed bounds; see [result ownership](design/result-memory.md).

Routed clients receive virtual PID/secret credentials. Cancellation targets only
their active operation, stops retries, and awaits native cancel EOF plus the
original response drain and a private synchronization cycle before another user
command. Actual PostgreSQL I/T/E status is retained; a canceled transaction is
never moved to a fresh backend. Cleanup failure closes the session. Cancellation
after response delivery starts closes the stream instead of appending an error
inside a frame or replaying the query. A completed query may win the cancel race.
A packet arriving during a later query on the same frontend has the same native
PostgreSQL ambiguity; operation epochs fence accepted earlier sends and backend
handoff, not a query identifier absent from the wire protocol.

SQL admission remains bounded by max_clients. Up to 32 separately bounded startup
slots recognize cancellation when SQL capacity is full; excess SQL startup is
rejected without acquiring a SQL permit. These control-only tasks are excluded
from clients_active/session duration. Cancel packets admitted through an available
SQL slot retain ordinary session accounting. Both paths increment
control_requests_total. Pending native sends remain owned by bounded connection
tasks. Opaque passthrough has no virtual mapping and does not gain routed
cancellation support from these changes.

Shared session pools use the same backend stream gauge for idle and active ownership.
Do not infer active leases from that total. The transaction slice PERF-008 adds
separate queue/lease/phase accounting before claiming capacity/queue acceptance.
Exact startup identities stay out of labels. Internal reset commands do not
increment logical-Q/attempt counters. Pool reset failure discards the connection
without rewriting a previously known frontend result.
