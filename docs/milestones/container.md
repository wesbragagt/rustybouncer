# H06/G07 checkpoint: distroless, non-root image

Authority: the local feature checklist.
Date: 2026-11-07 UTC. Base: `c1215db8be9aafc843d9f768590f190d69cdb3cb`.
This adds the container deployment artifact; H06/G07 remain open for the complete
Kubernetes/CI/deployment and conformance matrix. No existing requirement is narrowed.

## Implemented contract

`nix build .#docker --no-update-lock-file --out-link result-image` assembles a
Docker-loadable archive from scratch, using the locked package's runtime closure
and minimal passwd/group/NSS files. No Docker daemon is needed to build. The image
has no shell, toolchain or package manager and defaults to UID/GID 10001:10001.
It uses the binary directly as entrypoint with a mounted config path as its
argument, exposes 6432/tcp and supports read-only rootfs, capability removal and
no-new-privileges. No credential/configuration files are baked in.

[Container guide](../../deploy/README.md) includes build, Docker/Podman loading,
network/configuration and hardening commands. A network-hostname example config
is in `deploy/container.toml`. Only packaging/configuration/docs/tests changed;
router behavior is unchanged.

## Verification

Environment: Linux x86_64, locked Nix development shell, Docker 28.5.2,
Podman 5.7.0. Podman reports rootless=true; Docker here is rootful, so its smoke
run proves a non-root process, not a rootless Docker daemon. Rootless Podman
provides actual user-namespace/rootless execution evidence.

Commands, all exit 0:

- `nix build .#docker --no-update-lock-file --out-link result-image`
- `docker load -i result-image`
- `podman load -i result-image`
- `nix develop --command python3 tests/container_image.py --runtime docker`:
  3 passed in 1.657s; [raw output](container/docker.log).
- `nix develop --command python3 tests/container_image.py --runtime podman --require-rootless`:
  3 passed in 11.605s; [raw output](container/rootless-podman.log).
- `nix develop --command bash scripts/verify`: 10 Rust + 42 offline Python passed;
  Ruff, Astral ty, rustfmt, strict Clippy/typechecking, nixfmt, shellcheck,
  actionlint and complexity maximum 24/30 passed; [raw output](container/verification.log).

All required suites have zero failures, errors, skips, expected failures,
unexpected successes or ignored Rust tests. The rootless run's ignored Nix SQLite
cache-contention message precedes the test run; it is retained verbatim and both
commands exited 0.

The complete three-case container suite checks SQL SELECT/CTE/write round trips,
namespace-mapped process UID/GID, zero effective capabilities, no-new-privileges,
read-only rootfs and the exported filesystem's absence of shells, package managers
and compilers. Tests provision and remove their own PostgreSQL/proxy containers
and network, with cleanup registered during setup. No shell is exec'd into the
image, no tests are filtered or skipped, and no concurrency requirement is reduced.

Tested archive:
`/nix/store/480mgww6gmkld9fl9j6xgdagsq28b5sv-rustybouncer.tar.gz`.
Image tag `rustybouncer:latest`, ID
`sha256:15d9bf2754b69d64e93ee3b23ed070340bb5247517b6581157a0b26fa67692df`.
Uncompressed size 59,784,023 bytes (approximately 57 MiB). The archive was rebuilt
with the final implementation/test/configuration sources and retested under both
runtimes before adding this evidence document. Subsequent source changes are
this evidence and logs only. No image was pushed to a registry.

The installed commit hook verifies the exact staged snapshot with locked Nix
checks before permitting the milestone commit. Resolve its SHA with
`git log -1 --format=%H -- docs/milestones/container.md`.

## Outstanding scope

H06/G07 stay unchecked: the smoke suite does not run the full conformance matrix,
Kubernetes, hosted CI, cross-platform/architecture validation, failover-under-load
or release acceptance. It does not implement any open pooling, authentication,
TLS, cancellation or draining requirements. Rootless Docker daemon installation
is environment-owned; see Docker's official rootless setup link in the guide.
