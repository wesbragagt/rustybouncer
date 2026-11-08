# Getting-started and deployment documentation — H06/G07 checkpoint

Authority: [FEATURE_COMPLETENESS.md](../../FEATURE_COMPLETENESS.md).
This documentation/example milestone checks no additional completeness items.
It does not replace the full deployment/conformance matrix or hosted-CI evidence.

Delivered: a linked getting-started guide, deployment overview, self-contained
Docker Compose example and retained CloudNativePG manifests/instructions. Guides
cover prerequisites, local binaries, the distroless Docker/Podman image, ports,
configuration, persistence, registry adaptation, SQL verification and cleanup.
They distinguish a single-primary starter from managed replication/failover and
state the current trust/simple-query/unencrypted routing contract.

Validation on Linux x86_64 with Docker 28.5.2, Compose 2.40.3 and the locked Nix
shell (Python 3.14.8/Psycopg 3.3.6, kind 0.33.0, kubectl 1.37.2):

- `docker compose -f deploy/compose/compose.yaml config --quiet` exited 0.
- The Compose recipe was started under a unique project name with an ephemeral
  loopback port, then checked using a simple-query SQL client. PostgreSQL reported
  primary status, and CREATE/INSERT/SELECT returned 42. `down --volumes` removed
  only the owned demo resources. [Raw output](deployment-guides/compose.log).
- The CNPG recipe was applied to a separate owned four-node kind cluster using
  Kubernetes 1.36.4, CNPG 1.30.1 and PostgreSQL 16.13. After all three instances
  became ready, the router Deployment/Service was reachable over port forwarding.
  Six queries alternated between two actual replica addresses with recovery=true;
  an explicit transaction inserted/read 42 successfully. The cluster and private
  kubeconfig were removed afterwards. [Raw output](deployment-guides/cnpg.log).
  Kubernetes applied `router.yaml` through a temporary `router.json` symlink during
  the smoke run; the temporary alias was removed and is not shipped.
- Relative Markdown links in all six updated entry-point/deployment guides resolve.
  The native-binary path documents the existing supported command/configuration;
  Rust implementation was unchanged. The existing five-case CNPG fault suite was
  not rerun for documentation/example-only changes; its evidence remains linked.

Terminal-log trailing whitespace was normalized; command results were retained.

The smoke sequence follows the committed guide commands, with unique resource
names/ports for isolation. It waited for SQL readiness, checked SELECT recovery
state and backend addresses, and verified writes inside an explicit transaction
for CNPG. These are smoke checks, not new conformance certification or fault/load
coverage. Reproduce using the linked guides, keeping their stated prerequisites.

The installed hook runs the complete locked Nix verification on the exact staged
snapshot before permitting this milestone's commit. Resolve its SHA with
`git log -1 --format=%H -- docs/milestones/deployment-guides.md`.
H06/G07 and the remaining release checklist stay open.
