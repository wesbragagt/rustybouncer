# Deployment guide

Start with [Getting started](getting-started.md) for your first SQL connection.
The deployment examples share the same TOML configuration and PostgreSQL protocol.

## Native binary

Run the built executable on a host that can reach every PostgreSQL member:

```sh
nix develop --command cargo build --release --locked
RUST_LOG=rustybouncer=info ./target/release/rustybouncer config.local.toml
```

Use `listen = "127.0.0.1:6432"` for host-local clients. Configure real backend
addresses; `127.0.0.1` means the machine running the binary. A process supervisor
can restart the executable, but rustybouncer does not elect/promote PostgreSQL
leaders. That belongs to CNPG, Patroni or your existing cluster manager.

## Single Docker or Podman container

The [container guide](../deploy/README.md) documents the distroless image build,
read-only config mount and complete `docker run` command. The image runs as
UID/GID 10001 with no shell or package manager. It supports a read-only root
filesystem, removed capabilities and no-new-privileges. Rootless Podman was
validated; using a non-root image alone does not make the Docker daemon rootless.

Use container-network DNS names for backends and `listen = "0.0.0.0:6432"` inside
the container. Host-published ports can still be limited to `127.0.0.1`. Mount
configuration read-only and readable by UID 10001. Readiness comes from an
external SQL client; there is no in-image shell healthcheck.

## Docker Compose

The [Compose starter](../deploy/compose/README.md) provisions one PostgreSQL
primary plus the local distroless image. It includes database health-based startup,
a persistent volume, loopback-only publication and router hardening. It is the
quickest self-contained demo. To exercise balancing/failover, connect the router
to an existing managed streaming-replication cluster or use the CNPG examples.
Compose itself does not supply PostgreSQL leader election or promotion.

## Kubernetes with CloudNativePG

The [CNPG manifests and instructions](../deploy/cnpg/README.md) create three
PostgreSQL instances and deploy rustybouncer with a ConfigMap, per-instance
Services, a hardened Deployment and a client Service. The example includes a
retained kind setup for local experiments and explains image publication and
namespace/Pod-CIDR changes for an existing cluster.

CNPG manages replication and promotion. Rustybouncer probes each instance's actual
PostgreSQL role to choose writes/reads. Listing only CNPG's `postgres-rw` Service
would delegate backend selection to that Service and would not provide replica
round robin. This prototype does not watch Kubernetes resources: update the TOML
and per-instance Services when membership changes, then restart the Deployment.

For automated fault injection and cleanup, use the separate
[kind/CNPG test suite](../tests/kubernetes/README.md). Its two primary promotions
and same-connection continuity have [retained evidence](milestones/kind-cnpg.md).

## Configuration, operations and current limits

- `route_selects = true` enables conservative SELECT/read-only CTE routing; trust
  authentication and simple Query protocol are required. Omit it or set `false`
  for session passthrough. Session mode closes on leadership changes and does not
  automatically route reads/replay queries.
- `wait_timeout_ms` bounds routing/setup waiting. `query_timeout_ms` bounds each
  eligible read attempt; `read_retry_timeout_ms` and `read_max_attempts` bound the
  whole read retry cycle. Allow for your cluster manager's detection interval.
- Probe passwords may come from the environment variable named by
  `probe_password_env`. Probe credentials do not authenticate frontend users or
  execute their queries. Supply secrets through deployment-owned mechanisms.
- Configuration reload is not implemented. Restart the native process/container
  or run `kubectl rollout restart deployment/rustybouncer` after a ConfigMap edit.
- Logs go to stdout/stderr; enable `RUST_LOG=rustybouncer=debug` when diagnosing.
  Replica reads can be stale, and interrupted transactions/writes require
  application handling of failure or uncertain outcomes. No transparent replay
  of dispatched writes is promised.

These examples are isolated development/test deployments. Frontend authentication,
TLS, cancellation routing, shared transaction pooling and graceful draining remain
open features. Maintain network isolation; external fencing and replication
durability remain the cluster manager's responsibility. A Pod TCP readiness probe
only confirms a listener; verify SQL and actual backend roles as well.

[FEATURE_COMPLETENESS.md](../FEATURE_COMPLETENESS.md) remains authoritative.
Documentation and example smoke checks do not establish full H06/G07 deployment
or release acceptance, and hosted CI execution is not yet established.
