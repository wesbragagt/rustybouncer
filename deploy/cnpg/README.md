# CloudNativePG deployment

This retained demo runs a three-instance CNPG cluster and the distroless router.
It is separate from the [automated kind suite](../../tests/kubernetes/README.md),
which injects failures and deletes its cluster afterwards. These manifests leave
the cluster and database running until you explicitly clean them up.

## Prerequisites and image

Use a Kubernetes cluster with the CNPG operator, a default StorageClass and three
schedulable workers. Required Pod anti-affinity spreads PostgreSQL across three
nodes; insufficient capacity leaves Pods Pending. The example namespace is
`rustybouncer-demo`, the CNPG Cluster is `postgres`, and its database/owner is `app`.
Reserve at least 8 GiB available memory and 10 GiB disk for the local kind option.

Build/load the router image on Linux:

```sh
nix build .#docker --no-update-lock-file --out-link result-image
docker load -i result-image
```

For a local retained kind cluster, use the tested four-node topology and explicit
private kubeconfig. From the repository root:

```sh
export KUBECONFIG="$PWD/config.demo.kubeconfig"
nix develop --command kind create cluster --name rustybouncer-demo \
  --config tests/kubernetes/kind.yaml \
  --image kindest/node:v1.36.4@sha256:099e049362a1526b2db71494e1947aae99bd16290d7c895f2b7ea312e3cbfaed \
  --kubeconfig "$KUBECONFIG" --wait 180s
nix develop --command kind load docker-image rustybouncer:latest --name rustybouncer-demo
```

Keep `KUBECONFIG` set for all subsequent commands. Use a new path/name if a demo
already exists. The kubeconfig contains cluster-admin credentials; do not commit
or share it. For an existing Kubernetes cluster, use your intended kubeconfig
instead and **do not run kind create/delete**. Verify your target before applying:

```sh
nix develop --command kubectl config current-context
```

The router manifest defaults to a kind-local image with `imagePullPolicy: Never`.
On an existing cluster, publish your image to your registry, replace the image
reference in `router.yaml` with its immutable digest, and use `IfNotPresent`.
Configure any needed `imagePullSecrets` through your deployment's secret mechanism.
This repository does not publish an image for you.

## Install the operator, if needed

Skip installation when your intended cluster already has a compatible operator.
For the retained demo, install the same pinned CNPG 1.30.1 release used by tests:

```sh
curl -fsSLo /tmp/cnpg-1.30.1.yaml \
  https://raw.githubusercontent.com/cloudnative-pg/cloudnative-pg/v1.30.1/releases/cnpg-1.30.1.yaml
printf '%s\n' '37237f145d8138256ea25ae830f87759255665ff08f8d552fdd8224a5ec032fb  /tmp/cnpg-1.30.1.yaml' | sha256sum -c -
nix develop --command kubectl apply --server-side -f /tmp/cnpg-1.30.1.yaml
nix develop --command kubectl -n cnpg-system rollout status \
  deployment/cnpg-controller-manager --timeout=240s
```

This installs cluster-scoped resources. Upstream
[installation instructions](https://cloudnative-pg.io/docs/1.30/installation_upgrade/)
and the [support matrix](https://cloudnative-pg.io/docs/1.30/supported_releases/)
cover operator upgrades and supported Kubernetes versions.

## Deploy PostgreSQL and the router

Review [cluster.yaml](cluster.yaml) first. It uses a narrow trust rule for the `app`
role/database on `10.244.0.0/16`, kind's Pod CIDR. **Change that CIDR for another
cluster.** Routing mode currently requires trust authentication for backend client
connections and has no frontend authentication/TLS. Use this example only on an
isolated test network; CNPG's default SCRAM configuration alone is not compatible
with query-routing mode.

```sh
nix develop --command kubectl apply -f deploy/cnpg/cluster.yaml
nix develop --command kubectl -n rustybouncer-demo wait cluster/postgres \
  --for=condition=Ready --timeout=360s
nix develop --command kubectl apply -f deploy/cnpg/router.yaml
nix develop --command kubectl -n rustybouncer-demo rollout status \
  deployment/rustybouncer --timeout=120s
nix develop --command kubectl -n rustybouncer-demo get cluster,pods,services
```

[router.yaml](router.yaml) includes per-instance Services for `postgres-1`, `-2`
and `-3`, a TOML ConfigMap, a hardened non-root Deployment and a client ClusterIP
Service. Services select CNPG's `cnpg.io/instanceName` label and publish unready
endpoints so rustybouncer can independently probe each member's actual SQL role.
CNPG elects/promotes; rustybouncer discovers the primary and chooses read replicas.
It does not watch CRDs, fence nodes or manage replication.

If adapting an existing CNPG cluster, change the namespace, cluster/instance names,
Services, probe identity/database and TOML together. Do not replace all instance
addresses with the `-rw` Service when you want replica balancing. Scaling or
rebuilding CNPG members can change instance names; update the configured membership
and restart the router. There is no dynamic Kubernetes membership discovery.

## Connect and verify

Keep port forwarding open in one terminal:

```sh
nix develop --command kubectl -n rustybouncer-demo port-forward service/rustybouncer 6432:6432
```

In another terminal, connect through the forwarded listener:

```sh
nix develop --command psql \
  'host=127.0.0.1 port=6432 user=app dbname=app sslmode=disable'
```

Send separate statements on the same connection:

```sql
SELECT inet_server_addr(), pg_is_in_recovery();
SELECT inet_server_addr(), pg_is_in_recovery();
WITH answer AS (SELECT 42 AS value) SELECT value FROM answer;
BEGIN;
CREATE TABLE IF NOT EXISTS example (id integer PRIMARY KEY);
INSERT INTO example VALUES (1) ON CONFLICT DO NOTHING;
SELECT * FROM example;
COMMIT;
```

With two healthy replicas, the first two queries show different addresses and `t`.
The explicit transaction uses the primary and provides a consistent write/read
sequence. Autocommit replica reads may lag new writes. Pod TCP readiness alone
does not prove SQL role/replication health. Observe the real topology and logs:

```sh
nix develop --command kubectl -n rustybouncer-demo get cluster postgres
nix develop --command kubectl -n rustybouncer-demo logs deployment/rustybouncer
```

Edit the ConfigMap in `router.yaml`, reapply it, then restart to load changes:

```sh
nix develop --command kubectl apply -f deploy/cnpg/router.yaml
nix develop --command kubectl -n rustybouncer-demo rollout restart deployment/rustybouncer
nix develop --command kubectl -n rustybouncer-demo rollout status deployment/rustybouncer
```

For repeatable failure injection, run the separate automated kind suite rather
than crashing nodes in a shared cluster. Its [evidence](../../docs/milestones/kind-cnpg.md)
records actual promotions, non-replayed transactions and replication checks.

## Cleanup

For the **owned local kind demo only**, deleting the cluster removes its nodes
and local database storage:

```sh
nix develop --command kind delete cluster --name rustybouncer-demo --kubeconfig "$KUBECONFIG"
rm -- "$KUBECONFIG"
unset KUBECONFIG
```

On an existing cluster, deleting only the router objects leaves PostgreSQL intact:

```sh
nix develop --command kubectl delete -f deploy/cnpg/router.yaml
```

Deleting the CNPG Cluster or namespace is a separate destructive database operation.
Review your storage/PVC retention policy first. Do not remove a shared CNPG operator
as part of application cleanup. Full H06/G07 deployment acceptance remains open in
[FEATURE_COMPLETENESS.md](../../FEATURE_COMPLETENESS.md).
