# kind + CloudNativePG integration suite

This suite creates a real, disposable four-node kind Kubernetes cluster, installs
CloudNativePG, and bootstraps three streaming PostgreSQL instances with PVC-backed
storage on distinct workers. The distroless rustybouncer Deployment and CNPG
operator run on the control-plane node. The proxy runs as UID/GID 10001, drops all
capabilities, disables privilege escalation and uses a read-only root filesystem.

## Run the whole suite

Requirements: Linux, a working Docker daemon, internet access for the pinned node,
operator/PostgreSQL images and operator manifest, and enough capacity for four kind
nodes plus three PostgreSQL instances (allow at least 8 GiB available memory and
10 GiB disk). Nix supplies kind, kubectl, Python and Psycopg from `flake.lock`.

```sh
nix build .#docker --no-update-lock-file --out-link result-image
docker load -i result-image
nix develop --command python3 -m tests.kind_cnpg --output /tmp/rustybouncer-kind-results
```

Choose a new output directory for every invocation; existing evidence is never
overwritten. `--image` accepts another locally loaded rustybouncer image. The
runner imports it into its own kind cluster and uses `imagePullPolicy: Never`.
There is no registry push. There are no skip, capability, filter or downscaling
options; all five cases run. Missing tools, inaccessible images, setup failures,
failed cleanup or failed assertions make the command exit nonzero.

The harness uses a UUID cluster name and explicit private kubeconfig for every
kubectl command. It never changes the user's current Kubernetes context. Cleanup
is registered before provisioning, including for partially created kind clusters.
Crashed workers are restarted before cluster deletion; cleanup errors fail the run.
The kubeconfig is removed and is excluded from CI artifact paths.

## Required cases

1. Writes reach the independently observed CNPG primary, with rows verified using
   direct PostgreSQL SQL inside that instance.
2. Pure SELECT and read-only CTE queries alternate between both actual replicas
   on one frontend connection, identified by backend address and recovery state.
3. Deleting a replica Pod preserves reads on the same client connection, and CNPG
   creates a new Pod UID for that instance.
4. A continuous read stream starts before the primary's worker container is killed with SIGKILL.
   CNPG detects the node outage and promotes another instance. Every read must
   succeed, and a subsequent write on the same frontend connection must reach the
   newly observed primary. The client performs no query retries or reconnects.
5. An open transaction is interrupted by a separate primary-worker outage. COMMIT
   must fail, and its uncommitted marker must be absent on the promoted primary.
   This asserts non-replay, not the outcome of an ambiguous dispatched COMMIT.

Each case uses a unique schema, waits for relation replay on all instances, and
cleans up its clients/schema. Fault cases restore the worker and wait for all
three CNPG instances to become healthy with two actual streaming replicas. The tests do not depend on case ordering.

Per-instance Kubernetes Services select `cnpg.io/instanceName` and retain endpoints
for unready Pods. The router probes each instance and decides its role itself;
CNPG's `-rw` Service does not perform routing for the proxy. Fault injection only
crashes a worker whose name belongs to this invocation's cluster. The stopped node
cannot execute during promotion; recovery cold-starts that worker, providing
fencing for this simulation. This is not
a test of unfenced split brain or physical host failure.

Kubernetes normally needs tens of seconds to declare a stopped worker unreachable.
Tests wait for actual status/SQL conditions, with bounded command/poll budgets,
rather than assuming a fixed promotion delay. The routing wait budget is 180s to
accommodate this detection interval. Eligible reads have a 10s per-attempt
execution budget and a 180s total retry budget, with at most six backend selections. [CNPG failover documentation](https://cloudnative-pg.io/docs/1.30/failover/)
explains this sequence. The five tests are correctness checks, not performance or
availability-SLA measurements.

This isolated fixture uses a narrow trust rule for the `app` role/database on the
kind Pod CIDR, matching the prototype's routing contract. It does not validate
production authentication or TLS support. The Kubernetes setup is an optional
environment adapter; endpoint-only core conformance remains usable without it.

## Pins, evidence and CI

`pins.json` pins the kind node digest, tagged CNPG manifest URL/checksum and
operator image digest. `postgres.yaml` pins the PostgreSQL image digest. Update
these together and rerun the entire suite. The baseline follows the
[CNPG support matrix](https://cloudnative-pg.io/docs/1.30/supported_releases/).

Every invocation produces `results.json`, `junit.xml`, command logs, router logs,
Pod/Cluster snapshots, Kubernetes events, and timestamped fault/promotion evidence.
Reports use the strict shared reporter; fixture/cleanup errors cannot become a
passing run. The new `kind-cnpg` CI job builds the image, runs all five cases and
uploads diagnostics even on failure. CI wiring is not evidence of a hosted run.

Authority: [FEATURE_COMPLETENESS.md](../../FEATURE_COMPLETENESS.md), especially
H06, F11 and G07. See [milestone evidence](../../docs/milestones/kind-cnpg.md) for
actual execution results and remaining coverage. This suite does not replace the
complete PgBouncer/target conformance or deployment acceptance matrix.
