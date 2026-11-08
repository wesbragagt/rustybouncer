# Metrics operations milestone — delivery track 3

Scope: documentation and synthetic Prometheus validation only, based on reviewed
[metric contract](../plans/prometheus-metrics.md), base main
ad8aff86b1cff483a20a8b10d663208e5af54f20. The
[canonical feature checklist](../../FEATURE_COMPLETENESS.md) remains authoritative.
D04, H11, S10–S12, F11/F12 and Q01–Q08 are related requirements, not completion
claims. No canonical checkbox is changed.

## Deliverables and contract coordination

[Operator reference](../metrics.md) preserves the endpoint, inventory, bounded
labels, bucket boundaries, Q-message accounting, privacy and interpretation
contract. [Prometheus assets](../../deploy/prometheus/) supply native/private
Compose/core Kubernetes scrape examples, 13 recording rules, 11 alerts and
32 synthetic scenarios (42 alert assertions and 12 PromQL assertions).
The executable check wrapper needs only the locked shell's promtool.

No runtime metric names or meanings were changed. Discovery freshness explicitly
matches scrape-target profile interval500ms_timeout1000ms to a 15s threshold.
Current probes run concurrently with a 1s timeout followed by a 0.5s interval;
5s scrape/evaluation periods yield a conservative 11.5s budget, rounded up to 15s.
An unmatched profile alerts instead of silently claiming freshness coverage.
Coordinate profile/budget changes with runtime and deployment owners if cadence
or probe scheduling changes. No cadence metric has been invented.

Worktree: /home/publicuser/.t3/worktrees/rustybouncer/t3-28a521d5
Branch: t3/prometheus-ops. workmux registration command:

```sh
workmux open t3-28a521d5 --target-name rustybouncer-metrics-ops-t328a --run-hooks
```

Registration opened its tmux coordination session successfully. direnv reported
the worktree .envrc was unapproved; explicit locked Nix commands below worked
without enabling direnv. No additional agents were launched.

## Verification and retained evidence

Environment: x86_64-linux, locked flake.nix/flake.lock at the base commit.
Promtool 3.15.1 (Go 1.26.10) is provisioned in the existing shell through the
coordinator's Iceman change; this track changes no manifests.

```sh
nix develop --no-update-lock-file --command bash -c 'shellcheck deploy/prometheus/check.sh && bash deploy/prometheus/check.sh'
nix flake check --no-update-lock-file
# Build log retrieval for the exact verification derivation (see nix-check.log):
nix log /nix/store/mx41x58827zbs1gcyxp4x6fca4pzvyri-rustybouncer-0.1.0.drv
git diff --cached --check
git commit -m "docs(metrics): deliver operations rules and synthetic verification (D04 H11 S10 F11 Q01)"
```

[Promtool evidence](../../deploy/prometheus/evidence/promtool.log) contains the full
version, all three successful config checks, rule check (24 rules) and rule-test
success. Positive and negative assertions cover transient/sustained ambiguity and
no primary, discovery startup/restart, stale versus fresh unchanged topology,
disabled routing, scrape failure, missing targets/contract/profile, capacity
pressure, no traffic, SQL-only errors, counter reset, retry amplification and
attempt failures recovered before client-visible success. Numerical assertions
verify Q throughput, SQL ratio, mixed-outcome p50/p95/p99 and attempt/retry ratios.
The early synthetic run found and corrected a retry-rate label mismatch; only
final results are acceptance evidence.

[Locked Nix check log](../../deploy/prometheus/evidence/nix-check.log) records
successful x86_64-linux verification; [full build log](../../deploy/prometheus/evidence/nix-build.log)
retains compilation and all check output (terminal color/control sequences removed).
Results: 10 Rust tests passed, zero failed/ignored/filtered; 45 Python tests passed
with zero errors/skips/expected failures. Ruff and ty passed. Complexity checked
403 functions: maximum 24, limit 30. Formatting, Clippy, shellcheck and actionlint
passed through scripts/verify. The host check does not build incompatible Darwin
or aarch64 systems; Nix reports that explicitly. Promtool and shellcheck wrapper
also exited zero. The installed .githooks/pre-commit verifies the exact staged
snapshot with its own locked Nix check; the commit hash and hook outcome are
reported at handoff. No bypass or threshold changes are used.

## Remaining live integration dependency

The runtime metrics implementation has not landed in this base. These results
establish config/rule syntax and synthetic semantics only. They do not prove
runtime exposition, label cardinality, HTTP security/limits, real retry/failover
accounting, scrape reachability, NetworkPolicy enforcement or performance overhead.
No Docker/CNPG live suite is claimed or required for this documentation/rule
milestone. Root must integrate runtime and validation tracks, run the complete
metrics plus failover/long-query suites and enabled/disabled benchmarks, confirm
cadence matches the profile, and perform combined deployment acceptance. Root
owns shared README/deployment links, workmux merge and checklist registration.
