# CloudNativePG authentication and TLS checkpoint

Canonical scope: [A01–A04, A06–A09, P09, H03/H06 and G01–G08](../../FEATURE_COMPLETENESS.md).
This milestone removes the trust-only security blocker for routed/shared pooling
with CloudNativePG. It does not establish complete pooler/release conformance;
no canonical checkbox is closed by this checkpoint.

## Implemented contract

- Optional frontend TLS requires encryption for SQL startup. Backend TLS verifies
  CA chains and DNS/IP identities with no plaintext or insecure-verification fallback.
  A node's `tls_server_name` supports CNPG's shared server certificate while its
  `host` continues to address an individual instance.
- A certificate-authenticated, parameterized `auth_query` retrieves the client's
  PostgreSQL SCRAM verifier for every new frontend. The provided restricted
  SECURITY DEFINER function excludes NOLOGIN roles and nulls expired passwords.
  Missing, null, mismatched or malformed lookup results fail authentication.
- Frontends use SCRAM-SHA-256, including the `n,,` and `y,,` GS2 headers. Proofs and
  server signatures are checked, and backend SCRAM uses recovered client/server
  keys. Passwords are never collected in plaintext. Application SQL executes as
  the original startup role, rather than the lookup/probe role. Authentication
  messages have bounded lengths and deadlines; drivers are owned on cancellation.
- New handshakes reread PEM credentials, including Kubernetes projected Secrets.
  Rotation needs no process restart. Existing sessions retain their negotiated
  credentials; idle/lifetime limits retire existing pooled backends. This does
  not add TOML hot reload or certificate revocation infrastructure.
- TLS also covers probes and backend cancellation. Plaintext PostgreSQL
  CancelRequest remains accepted as a control packet carrying no SQL.
- The CNPG deployment example uses transaction pooling and mounted certificate
  Secrets. Its CA volume projects only `ca.crt`, never the CA signing key.
  [Configuration, SQL and operational instructions](../../deploy/cnpg/README.md)
  describe the SCRAM-only contract and independent Deployment integration.

Authentication lookups use short-lived connections separate from the application
pool. PostgreSQL connection capacity must include concurrent lookups and probes
as well as pooled backends. SCRAM-PLUS and MD5 remain outside this implementation;
A05 and the full canonical authentication/concurrency matrix remain open.

## Retained acceptance evidence

All final required scopes below completed with zero failures, errors, skips,
expected failures or unexpected successes. Historical failed setup/adapter runs
are retained separately as diagnostics and are not completion evidence.

| Scope | Passing cases | Evidence |
| --- | ---: | --- |
| Rust unit and independent PostgreSQL SCRAM-client interoperability tests | 71 | [Verification log](cnpg-security/offline-verification.log) |
| Complete offline Python inventory | 73 | Same verification log; the installed hook rechecks the final staged snapshot |
| Complete native failover, long-query, buffering, resource/cancellation, metrics, session/transaction/protocol and queue suites | 144 | [Per-suite results](cnpg-security/native-results.json), [complete raw evidence](cnpg-security/native-evidence.tar.gz) |
| Native PostgreSQL deadline integration | 5 | Same per-suite results and raw archive |
| Real kind/CNPG TLS, SCRAM/auth_query, certificate/password rotation, pooling, cancellation and promotion | 10 | [JSON](cnpg-security/security-results.json), [JUnit](cnpg-security/security-junit.xml), [events](cnpg-security/security-events.jsonl), [complete archive](cnpg-security/kind-security-evidence.tar.gz) |
| Existing real kind/CNPG routing, metrics, replica replacement and crash/promotion regressions | 6 | [JSON](cnpg-security/baseline-results.json), [JUnit](cnpg-security/baseline-junit.xml), [complete archive](cnpg-security/kind-baseline-evidence.tar.gz) |

The security suite observes encryption on both connection legs, rejects wrong
passwords/unknown/expired/NOLOGIN roles and unauthorized database access, verifies
CA/hostname failures and plaintext rejection, executes a named prepared query,
proves transaction-pool PID reuse and role isolation, and checks real SQL recovery
after primary-worker SIGKILL. Certificate rotation records different server
fingerprints and the same pod UID with zero restarts. A wrong certificate CN must
break auth_query, and the replacement must restore SQL. TLS cancellation must
produce SQLSTATE 57014 and preserve the authenticated connection.

The security adapter publishes a dedicated NodePort through kind port mappings;
no shared port-forward tunnel can turn one TLS rejection into endpoint refusal.
Its application fixture role is separate from CNPG's bootstrap-secret-managed
owner, and WAL replay barriers protect role/schema setup before promotion. All
assertions remain enabled. Both owned kind clusters and their kubeconfigs were
removed; retained archives were scanned for private keys and fixture passwords.

## Commands and environment

Locked Nix on Linux x86_64; Rust 1.99.0; kind 0.33.0; Kubernetes 1.36.4;
CNPG 1.30.1; CNPG PostgreSQL 16.13; Docker-native PostgreSQL 16.15. Image IDs,
archive paths and runtime/fixture source hashes are in
[artifacts.json](cnpg-security/artifacts.json). Both kind suites loaded image
`sha256:c88a1cd3d96deff2325c66bf9eaa7580befa8c8fd2bdd5a6e002ebbc146500b0`.

The normal installed `.githooks/pre-commit` verifies the staged snapshot using the
locked flake. No checks, thresholds, concurrency levels or capability requirements
were disabled. `openssl` was added to the dev environment using `iceman`.

Build/verification commands:

```sh
nix develop --command bash -c 'cargo fmt --all && cargo build --locked && bash scripts/verify'
sudo -n nix build .#docker --no-update-lock-file \
  --option build-dir /var/cache/rustybouncer-nix-build \
  --out-link /tmp/rustybouncer-cnpg-security-image-final
docker load -i /tmp/rustybouncer-cnpg-security-image-final
nix develop --command python3 -m tests.kind_cnpg_security \
  --output /tmp/rustybouncer-cnpg-security-nodeport
nix develop --command python3 -m tests.kind_cnpg \
  --output /tmp/rustybouncer-cnpg-baseline-final
```

The live native modules were each run in full through the locked dev shell with
`RUSTYBOUNCER_BIN=$PWD/target/debug/rustybouncer`. The evidence root was
`/tmp/rustybouncer-security-regressions-final`; `SESSION_POOL_EVIDENCE`,
`TRANSACTION_POOL_EVIDENCE`, `PROTOCOL_POOL_EVIDENCE`, `QUEUE_POOL_EVIDENCE`,
`BUFFER_EVIDENCE`, `RESOURCE_EVIDENCE` and `METRICS_EVIDENCE` selected its
`session`, `transaction`, `protocol`, `queue`, `buffer-verified`, `resource` and
`metrics` directories respectively. Full module commands, in order:

```sh
python3 tests/failover.py
python3 tests/long_queries.py
python3 tests/buffering.py
python3 tests/resources.py
python3 tests/metrics.py
python3 tests/session_pool.py
python3 tests/transaction_pool.py
python3 tests/protocol_pool.py
python3 tests/queue_pool.py
python3 -m tests.deadline_integration
```

[Diagnostic records](cnpg-security/diagnostics) preserve tmpfs build exhaustion,
contended Docker/failover attempts, missing buffering environment setup, initial
certificate/API/role fixtures, and the failed shared port-forward adapter. They
are superseded only for acceptance by the complete passing scopes above, not
erased. Build scratch space was provisioned on disk; the installed commit hook
uses a disk-backed `TMPDIR`. Nix/Docker cleanup reclaimed 17.7 GiB/20.8 GB,
plus six unused, detached kind volumes accounting for 6.39 GiB. Attached volumes
were preserved.

CI runs both kind suites as a matrix and retains their reports. Observed hosted-CI
execution, the reference-PgBouncer security matrix, broader A/P scopes and G01–G08
release acceptance remain outstanding. The milestone commit introducing this
record identifies its implementation and evidence; no future commit hash is guessed.
