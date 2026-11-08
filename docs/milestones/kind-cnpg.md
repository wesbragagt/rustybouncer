# F11; H06/G07 checkpoint — real kind and CloudNativePG integration

Authority: [FEATURE_COMPLETENESS.md](../../FEATURE_COMPLETENESS.md).
Execution date: 2026-11-08 UTC (2026-11-07 America/Chicago).
Base commit: `cda6d14`. This milestone adds real Kubernetes integration and
establishes F11's automatic-primary-discovery/promotion behavior. H06/G07 remain
open for the complete deployment/conformance matrix and hosted CI execution.

## Delivered

- The locked Nix devShell includes kind and kubectl, discovered/added with Iceman.
- An isolated four-node kind cluster runs the actual CNPG operator and three
  PostgreSQL instances on separate workers with PVC-backed storage.
- The existing distroless image runs as UID/GID 10001 with a read-only filesystem,
  no extra capabilities or privilege escalation. Operator/router placement on the
  control plane keeps failover control alive during a database-worker crash.
- Per-instance Services expose individual PostgreSQL roles to rustybouncer's
  discovery; CNPG's primary Service does not route the proxy's backend traffic.
- Five mandatory black-box cases check primary writes, replica/CTE rotation,
  replica Pod replacement, same-connection continuity through node failure and
  transaction non-replay. Three offline safety regressions guard fault ownership,
  cleanup after failed restart and preservation of existing output directories.
- A separate `kind-cnpg` workflow job builds the image, runs the complete suite
  and uploads non-credential diagnostics. Hosted execution has not been observed.

[Run guide and fixture contract](../../tests/kubernetes/README.md).

## Exact environment and commands

Linux x86_64, Docker 28.5.2, locked Nix devShell, Python 3.14.8/Psycopg 3.3.6,
kind v0.33.0, kubectl v1.37.2, Kubernetes v1.36.4, CNPG v1.30.1,
PostgreSQL 16.13. Node/operator/PostgreSQL image digests and the operator manifest
checksum are committed in `tests/kubernetes/pins.json` and `postgres.yaml`.
The [versions/topology/fault events](kind-cnpg/events.jsonl) retain actual versions
and backend Pod identities. Tested proxy image ID:
`sha256:306c807b68b79d81ac7ecc0868c17db1ba453adc4513c6795ba129b1270c158c`.
The Rust router source did not change during this milestone.

Commands:

```sh
nix build .#docker --no-update-lock-file --out-link result-image
docker load -i result-image
nix develop --command python3 -m tests.kind_cnpg --output /tmp/rustybouncer-kind-crash-final
nix develop --command bash scripts/verify
```

All exited 0. Full kind suite: **5 passed in 542.917s**, zero failures, errors,
skips, expected failures or unexpected successes. Full verification: **10 Rust +
45 offline Python passed**, zero ignored/filtered/skipped tests; Rust typechecking,
strict Clippy, rustfmt, Ruff, Astral ty, nixfmt, shellcheck and actionlint passed.
Cyclomatic complexity maximum **24/30**, including all new Python helpers/tests.

The installed hook additionally runs `nix flake check --no-update-lock-file`
against the exact staged snapshot before allowing the implementation commit.
Its SHA is the commit introducing this document:
`git log -1 --format=%H -- docs/milestones/kind-cnpg.md`.
The canonical checklist is registered only after that implementation commit exists.

Retained evidence:

- [Suite output](kind-cnpg/suite.log), [verification output](kind-cnpg/verification.log).
- [Readable report summary](kind-cnpg/summary.json).
- Complete [JSON report, gzip](kind-cnpg/results.json.gz) and
  [JUnit report, gzip](kind-cnpg/junit.xml.gz). The summary includes SHA-256 hashes
  of their decompressed contents. No test/evidence records were omitted.
- [Timestamped events](kind-cnpg/events.jsonl), final
  [Cluster status](kind-cnpg/cluster.log), [Pod identities](kind-cnpg/pods.log) and
  [router log](kind-cnpg/router.log).

## How failure was tested

The fixture verifies the selected node belongs to its own kind workers, then
sends SIGKILL to the primary's entire worker container. This kills PostgreSQL,
its instance manager and kubelet. The old primary cannot execute while CNPG
observes the node outage and promotes a surviving replica. No manual SQL promotion
or edited primary status is used. The operator/router remain on the control plane.

The first outage promoted **postgres-1 → postgres-2**. **1,323 reads** succeeded
on the original frontend connection without client retries or reconnects, and a
subsequent INSERT reached the independently identified new primary. Recovery
cold-started the old worker and waited for three ready instances **and two actual
streaming replicas**. A deleted replica was recreated with a different Pod UID;
the same client subsequently observed both replicas again.

The second outage promoted **postgres-2 → postgres-1** during an open transaction.
COMMIT failed as required, and the promoted server contained **zero** rows for its
uncommitted marker. This proves non-replay for that transaction, not that a lost
acknowledgement for a dispatched COMMIT can establish non-commit.

Before each case a schema-replay barrier checks every actual instance. After each
fault the worker is restarted and physical streaming is checked, preserving case
independence. The final run removed all four kind nodes, stopped port-forwarding
and removed the private kubeconfig. Post-run Docker inspection found no remaining
containers for `rustybouncer-c5b28bc5b8`.

## Development findings and limits

An initial startup attempt exposed a resource whose status had not been populated;
status polling now handles that normal reconciliation interval. That attempt failed
and was cleaned up; it is not passing evidence.

A superseded pause/resume fault exposed a standby on the old WAL timeline after
resuming the frozen primary, despite ready conditions. The schema barrier caught
it; [retained diagnostic](kind-cnpg/development-timeline-diagnostic.log). Those
investigation runs were interrupted and cleaned up, not accepted. The final fault
model is explicitly **worker crash/cold restart**, with physical-streaming recovery
checks. It does not claim pause/resume, arbitrary partitions or unfenced split-brain
recovery; F03/F12 remain open. No cases were excluded to get the final five-case pass.

This is not complete PgBouncer/target conformance, TLS/authentication validation,
production durability proof, sustained load testing or a Kubernetes availability
SLA. The local fixture deliberately uses a narrow app-role/database trust rule on
the kind Pod CIDR. H06/G07 and release acceptance remain open; CI wiring alone is
not evidence that hosted execution passed.
