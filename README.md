# RustyBouncer

Keep connection pooling and database-aware routing in one process. RustyBouncer
adds automatic primary discovery, query-level replica routing, and bounded failover
waiting to a PostgreSQL pooler built for CloudNativePG.

[PgBouncer](https://www.pgbouncer.org/config.html#databases) pools connections to configured targets. RustyBouncer also inspects SQL
and database roles to decide where work belongs:

- **Use your replicas:** unshared routing sends eligible reads to healthy replicas
  and writes to the primary. Shared pools currently keep all SQL on the primary.
- **Handle primary changes:** idle, stateless clients can keep their connection
  through failover. Undispatched writes can wait for the replacement primary.
- **Keep pooling secure:** shared session and transaction pools support TLS,
  SCRAM through `auth_query`, and rotating PEM certificates without a restart.
- **Validate the integration:** real CloudNativePG tests in kind exercise routing,
  failover, TLS, certificate rotation, and authentication.

Version **1.0.1** does not promote PostgreSQL nodes
or replay dispatched writes or interrupted transactions. It is not a complete
PgBouncer replacement.

## Getting started

Install Nix with flakes enabled and Docker Engine with Compose v2. Start a local
PostgreSQL instance and RustyBouncer:

```sh
git clone https://github.com/wesbragagt/rustybouncer.git
cd rustybouncer
git checkout v1.0.1
nix build .#docker --no-update-lock-file --out-link result-image
docker load -i result-image
docker compose -f deploy/compose/compose.yaml up -d
nix develop --command pg_isready -h 127.0.0.1 -p 6432 -U postgres -d postgres
```

Once readiness reports `accepting connections`, run a query:

```sh
nix develop --command psql \
  'host=127.0.0.1 port=6432 user=postgres dbname=postgres sslmode=disable' \
  -c 'SELECT 42;'
```

This loopback-only demo uses trust authentication and one primary. For TLS, SCRAM,
certificate rotation, and three PostgreSQL instances, follow the
[CloudNativePG guide](deploy/cnpg/README.md).

Stop the demo while keeping its database volume:

```sh
docker compose -f deploy/compose/compose.yaml down
```

## Documentation and validation

- [Routing and failover behavior](docs/routing.md)
- [Configuration and native setup](docs/getting-started.md)
- [Deployment options](docs/deployments.md)
- [Prometheus metrics](docs/metrics.md) and [Grafana dashboards](docs/grafana.md)
- [CloudNativePG tests in kind](tests/kubernetes/README.md), including TLS, certificate rotation, and authentication
- [Benchmark results and limitations](docs/benchmarks/performance-final.md)
- [Publication audit and historical evidence](PUBLICATION.md)

Run local verification with `nix flake check --no-update-lock-file`.
Broader production and release acceptance remains incomplete.

## License and feedback

Licensed under [Apache 2.0](LICENSE). Report bugs and request features through
[GitHub issues](https://github.com/wesbragagt/rustybouncer/issues).
[Contributions are issues-only](CONTRIBUTING.md); pull requests are disabled.
