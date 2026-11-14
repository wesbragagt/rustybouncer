# Public repository and historical evidence

The source is published at <https://github.com/wesbragagt/rustybouncer> and mirrored
to Forgejo. The public history retains the original 59 source commits, with new
commit IDs after removing private workstation paths and infrastructure hostnames.
[Commit mappings](docs/publication/commit-map.json) relate the original IDs cited
in historical reports to their sanitized public ancestors. A later publication
commit records the public export itself. Private recovery branches retain the
original history locally; do not push them or merge pre-publication branches into
public `main`. Rebase or apply reviewed patches onto the sanitized history.

No measured timings, test outcomes, required checklist items or acceptance
thresholds are changed by this publication. Historical failed/skipped diagnostic
runs remain diagnostics; they are not promoted to passing release evidence.
[FEATURE_COMPLETENESS.md](FEATURE_COMPLETENESS.md) remains authoritative.

Workstation path prefixes were replaced with generic public paths in text and
compressed evidence. Equal-width replacements preserve offsets in retained ELF
and profiling data. Archive user metadata was generalized, and archives were
recompressed. Historical source/binary/archive hashes still describe the original
measured artifacts. They must not be mistaken for hashes of sanitized copies.
The [artifact manifest](docs/publication/artifact-manifest.json) records original
and public SHA-256 values, public sizes, and download URLs for release assets.

Ten archives exceed GitHub's per-file limit. Their audited public copies are
preserved as assets in the `evidence-publication-20261114` release, rather than
Git blobs. Documentation links point directly to those assets. Download an asset
and verify its public SHA-256 from the manifest before unpacking it. If a retained
command expects its original relative path, place the downloaded archive at the
manifest's `path`. Smaller evidence archives remain in Git. No Git LFS subscription
is required.

The publication audit includes Git-history and tracked-tree secret scanning,
recursive archive traversal, historical blobs absent from the current tree,
manual review of scanner findings, and a scan for private metadata. Audit results
and tool versions are recorded in [the publication audit](docs/publication/audit.json).
Public reports contain counts and classifications, not credentials or private
infrastructure values. No ignored local configuration, working-tree credentials,
private keys, or live kubeconfigs are included in the exported Git history.

The current project license is Apache-2.0 (`LICENSE` and Cargo metadata), with
contributor attribution retained in `NOTICE`. Historical commits retain their
then-current license. GitHub accepts issues; pull requests, Discussions, projects
and wikis are disabled.

The executable's Rust sources, dependency lock and runtime behavior are unchanged
from the validated CNPG milestone. The installed verification hook checks the
publication commit normally. Full platform/security/release acceptance remains
open as recorded in the completeness checklist.

## Large evidence downloads

| Original path | Public download | Public SHA-256 |
| --- | --- | --- |
| `docs/benchmarks/perf-final/normalized-after.tar.zst` | [Download](https://github.com/wesbragagt/rustybouncer/releases/download/evidence-publication-20261114/a4b725a57e350d31-normalized-after.tar.zst) | `ba04d9a58fab999f686641a225f0874563ea94230da98207e51583fcf49639de` |
| `docs/benchmarks/perf-final/original-after.tar.zst` | [Download](https://github.com/wesbragagt/rustybouncer/releases/download/evidence-publication-20261114/d0fef391743729d8-original-after.tar.zst) | `10a879aebbe942b853bfed79a0e277fe62635548fe89ee5cea29c0e7a9155b21` |
| `docs/benchmarks/perf-final/pools-after.tar.zst` | [Download](https://github.com/wesbragagt/rustybouncer/releases/download/evidence-publication-20261114/ca6a534793dc3761-pools-after.tar.zst) | `9fd431b93881ce57244b9da39dd99517b83208398a307e38876e17eb7f230ebf` |
| `docs/benchmarks/perf-final/profiles-after.tar.zst` | [Download](https://github.com/wesbragagt/rustybouncer/releases/download/evidence-publication-20261114/ea54b2103ba01854-profiles-after.tar.zst) | `36e58e7f81c5c197b2b3b688d8407e353c3bc89e0f0981ff8e0e51ff0ef1e432` |
| `docs/benchmarks/perf-final/workloads-after.tar.zst` | [Download](https://github.com/wesbragagt/rustybouncer/releases/download/evidence-publication-20261114/b306f85eb0654063-workloads-after.tar.zst) | `05a97759baf79da3394c9d47fbd8707f603b1b2bb5c9a476366fd923deb868de` |
| `docs/benchmarks/perf-pools/raw.tar.zst` | [Download](https://github.com/wesbragagt/rustybouncer/releases/download/evidence-publication-20261114/29f916737a5320ca-raw.tar.zst) | `fa710695456f10beee93c713b4eefac28093e297c98282a8c0a460e2e3106bf0` |
| `docs/milestones/perf-resources-evidence/combined/original-comparison.tar.zst` | [Download](https://github.com/wesbragagt/rustybouncer/releases/download/evidence-publication-20261114/26d5ad7ab98584a4-original-comparison.tar.zst) | `f96b097a7962cdea5607d4440a7d50d84c3ecdb5f57781b878b107bed2790571` |
| `docs/milestones/perf-scheduling-evidence/before/raw.tar.zst` | [Download](https://github.com/wesbragagt/rustybouncer/releases/download/evidence-publication-20261114/cf0fde328b108edf-raw.tar.zst) | `d6fb447808f562ee84d532e75e882b048198a31712a3baf9f4bf5ab62810098a` |
| `docs/milestones/perf-scheduling-evidence/original-before/raw.tar.zst` | [Download](https://github.com/wesbragagt/rustybouncer/releases/download/evidence-publication-20261114/b68c6e2c417f368d-raw.tar.zst) | `a398d6d1240ce2d5c3db4c2df2dc281d8e7bdf9ecebc7e7dafd8b65f2ee84be5` |
| `docs/milestones/perf-session-evidence/original-comparison/raw.tar.zst` | [Download](https://github.com/wesbragagt/rustybouncer/releases/download/evidence-publication-20261114/875f56f3443b5b5c-raw.tar.zst) | `763940dc0345c7da6b92fa79f8a41a7ea94dbee2f6b0f1a18082770183aa0276` |
