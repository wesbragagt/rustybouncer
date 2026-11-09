# Grafana dashboards

Start with the shipped **Rustybouncer Overview** for the router, then add the
PostgreSQL and infrastructure dashboard appropriate to your deployment. Reviewed
upstream sources on 2026-11-08. The [metrics contract](metrics.md) defines what
Rustybouncer observes; [FEATURE_COMPLETENESS.md](../FEATURE_COMPLETENESS.md)
remains the project acceptance target.

| Dashboard | Use it for | Required source and compatibility |
| --- | --- | --- |
| [Rustybouncer Overview](../deploy/grafana/rustybouncer-overview.json) — recommended first | Query throughput, mixed-outcome p50/p95/p99, router vs SQL errors, retries, admission, backend streams and topology | Rustybouncer `/metrics`, Prometheus and the [shipped recording rules](../deploy/prometheus/rules.yml). Native Grafana panels; no additional panel plugin. |
| [CloudNativePG Cluster](https://github.com/cloudnative-pg/grafana-dashboards) — recommended for CNPG | PostgreSQL/CNPG cluster health and database behavior alongside router observations | CNPG instance/operator metrics, their default monitoring queries and matching labels. Follow the [CNPG monitoring guide](https://cloudnative-pg.io/docs/1.30/monitoring/); these are separate scrape targets from Rustybouncer. Prefer the maintained project repository/chart and pin your chosen revision. |
| [PostgreSQL Database, Grafana ID 9628](https://grafana.com/grafana/dashboards/9628-postgresql-database/) — optional for native/Compose PostgreSQL | A starting database view when you use postgres_exporter | A separately deployed [Prometheus PostgreSQL exporter](https://github.com/prometheus-community/postgres_exporter). The catalog dashboard references that exporter's metrics; validate your exporter version, collectors and labels before adoption. It is a community template, not a verified CNPG default dashboard. |
| [Kubernetes mixin dashboards](https://github.com/kubernetes-sigs/kubernetes-mixin) — recommended for Kubernetes | Workload CPU/memory, resource requests, nodes and Kubernetes context for the router and CNPG | The mixin's Kubernetes collection/recording rules, normally supplied by kube-prometheus-stack, including kube-state-metrics and kubelet/cAdvisor metrics. These metrics are not supplied by Rustybouncer. |
| [Node Exporter Full, Grafana ID 1860](https://grafana.com/grafana/dashboards/1860-node-exporter-full/) — recommended for native Linux hosts | Host CPU, memory, disk and network saturation | node_exporter, normally scraped as job `node`; some panels need optional collectors. This covers the host, not per-container resource usage. |

The [CNPG project](https://github.com/cloudnative-pg/grafana-dashboards) distributes
its dashboards independently of the operator. Select a release compatible with
your installed CNPG monitoring metrics and review changes when upgrading. A
catalog dashboard ID alone does not establish version compatibility. The
PostgreSQL catalog page explicitly requires postgres_exporter, and the Node
Exporter Full page specifies its node_exporter/job assumptions.

## Import Rustybouncer Overview

1. Enable the private endpoint and Prometheus scrape configuration from the
   [metrics guide](metrics.md). Load `deploy/prometheus/rules.yml` in Prometheus.
2. Add that Prometheus server as a Grafana Prometheus data source. Grafana talks
   to Prometheus; it does not query the router's HTTP endpoint as a data source.
3. Follow Grafana's [dashboard import instructions](https://grafana.com/docs/grafana/latest/visualizations/dashboards/build-dashboards/import-dashboards/)
   and upload `deploy/grafana/rustybouncer-overview.json`.
4. Choose the **Prometheus**, **job**, and **instance** variables at the top. The
   source defaults to job `rustybouncer`; select each router independently or
   choose all instances to compare their lines. Choices are derived from `up`,
   so a known down target remains selectable.
5. Check **HTTP scrape up** and **SELECT routing enabled** before interpreting
   query panels. Generate some routed SQL traffic and allow two scrapes plus a
   recording-rule evaluation. The examples use five-second scrape/evaluation
   intervals; rates and quantiles use five-minute windows.

The file uses Grafana's classic JSON dashboard model with native time-series and
table panels. Its Prometheus data source is a selectable [data source variable](https://grafana.com/docs/grafana/latest/visualizations/dashboards/variables/add-template-variables/).
For file provisioning, retain the variable and configure your Prometheus data
source separately; no credentials or fixed data source UID are embedded. Pin
external dashboard JSON/chart versions in your deployment rather than fetching a
moving latest revision at startup.

## Reading the panels correctly

- Throughput is frontend **simple Query** messages per second. A Q message may
  contain several statements; this is not a committed-transaction rate. Shared
  pools expose extended frame/cycle/exchange metrics separately; the shipped
  query panels do not include them. Use the [extended metrics contract](metrics.md)
  for prepared-driver traffic, and keep cycle duration (including client think
  time) distinct from execution latency.
- Latency quantiles include failed queries, route waits, setup, retries and client
  backpressure. They do not measure only successful PostgreSQL execution.
- A retry may recover a logical query. Compare attempt failures/retry amplification
  with client-visible errors; keep SQL/application errors and external aborts
  visible separately. Amplification can exceed 100%.
- With routing disabled, query panels show no observations. No traffic or no data
  is not proof of availability. A separate end-to-end SQL probe is necessary.
- Backend streams include cached connections in unshared mode and leased, idle
  or resetting shared connections in pooled modes; they are not active SQL counts.
  Use the separate pool reservation/phase/waiter metrics for capacity. Discovery
  probes are excluded. Use PostgreSQL/CNPG dashboards for replication/WAL/lag, and host or
  Kubernetes dashboards for CPU/memory/storage.
- Each router independently observes the same primary and replicas. Do not sum
  primary counts across routers or treat transition counters as failover duration.
  Topology eligibility is not a fencing guarantee.

PgBouncer exporter dashboards expect exporter-specific `pgbouncer_*` series or
administrative statistics. Rustybouncer exposes its documented `rustybouncer_*`
contract, so those templates require explicit query adaptation.

## Verification

`python3 scripts/check_grafana.py` extracts all panel expressions, substitutes job
and instance selectors, and validates them with the actual Prometheus parser.
It runs in the normal locked verification hook. This establishes JSON/PromQL
validity; the external dashboards above retain their own exporter and deployment
requirements. Dashboard appearance depends on your Grafana version and theme.
