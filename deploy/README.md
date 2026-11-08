# Distroless, non-root container

The image contains rustybouncer and its runtime library closure, assembled from
scratch by the pinned Nix toolchain. No shell, compiler or package manager ships
in the image. It defaults to UID/GID **10001:10001**, listens on an unprivileged
port and needs no writable filesystem. Build/load requires no registry push.
Linux builds produce a Docker archive for the build machine's architecture.

```sh
nix build .#docker --no-update-lock-file --out-link result-image
docker load -i result-image
# Rootless Podman can load the same archive:
podman load -i result-image
```

Nix assembles the image without a Docker daemon. A non-root container user is
separate from a rootless runtime: Docker's daemon must also be configured for
[rootless mode](https://docs.docker.com/engine/security/rootless/) if that is
required. This project does not change your daemon configuration.

Mount a readable TOML config at `/etc/rustybouncer/rustybouncer.toml`. The example
[container.toml](container.toml) listens on `0.0.0.0:6432` and resolves PostgreSQL
as `postgres` on the container network. Replace/add node hostnames for your actual
primary and replicas; the host-side example's `127.0.0.1` nodes would refer to the
container itself. Existing authentication/TLS limitations still apply; see the
[project guide](../README.md). Keep the listener on a trusted network.

For an existing PostgreSQL container named `postgres` on a `database` network:

```sh
docker run --rm --name rustybouncer --network database \
  --read-only --cap-drop=ALL --security-opt=no-new-privileges \
  -p 127.0.0.1:6432:6432 \
  --mount "type=bind,src=$PWD/deploy/container.toml,dst=/etc/rustybouncer/rustybouncer.toml,readonly" \
  rustybouncer:latest
```

Use `podman` in place of `docker` for rootless Podman. Set any probe credential
variable referenced by `probe_password_env` through your deployment's secret
mechanism. Secrets/configuration are not baked into the image. The executable
is the direct entrypoint; an optional final argument overrides the config path.
Logs go to stdout/stderr. There is no shell healthcheck; check SQL from an external
PostgreSQL client. Mounts must be readable by container UID 10001, including under
user namespace mappings. On SELinux hosts apply the appropriate mount label.

## Verification

After loading the image, run the entire three-test suite for each runtime:

```sh
nix develop --command python3 tests/container_image.py --runtime docker
nix develop --command python3 tests/container_image.py --runtime podman --require-rootless
```

The suite owns an isolated network, PostgreSQL and proxy containers. It checks
live SELECT/CTE/write operations, actual process UID/GID via namespace mappings,
zero effective capabilities, no-new-privileges, a read-only root filesystem, and
the absence of shells/toolchains/package managers in the exported filesystem.
`--require-rootless` fails if the runtime is not rootless. It never skips tests.
Host `/proc` access is required for the process identity checks.

Authority: [FEATURE_COMPLETENESS.md](../FEATURE_COMPLETENESS.md). This is a
container checkpoint toward H06/G07; hosted CI, full conformance and the complete
deployment matrix remain open. Local kind/CNPG validation is linked below. See [milestone evidence](../docs/milestones/container.md).

## Complete deployments

[Prometheus operations](../docs/metrics.md) provides private native, Compose and
CNPG scrape examples and tested rules. Runtime metrics integration and live
deployment validation remain pending.

For a self-contained local stack, use the [Compose starter](compose/README.md).
For three managed PostgreSQL instances and Kubernetes resources, use the
[CloudNativePG deployment](cnpg/README.md). The [getting-started guide](../docs/getting-started.md)
and [deployment guide](../docs/deployments.md) explain the different entry points.
Local Kubernetes failover has [verified evidence](../docs/milestones/kind-cnpg.md);
the full H06/G07 deployment/hosted-CI matrix remains open.
