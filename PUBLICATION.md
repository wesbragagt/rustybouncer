# Public repository and historical evidence

The source is published at <https://github.com/wesbragagt/rustybouncer> and mirrored
to Forgejo. The public history retains the original 59 source commits, with new
commit IDs after removing private workstation paths and infrastructure hostnames.
[Commit mappings](docs/publication/commit-map.json) relate the original IDs cited
in historical reports to their sanitized public ancestors. Later publication
commits record the public export itself. Design specifications, agent instructions, task plans and the working feature
checklist are excluded from published history and retained only locally. A recursive
check of 121 evidence archives found no embedded copies of those documents.
Private recovery branches retain the
original history locally; do not push them or merge pre-publication branches into
public `main`. Rebase or apply reviewed patches onto the sanitized history.

No measured timings, test outcomes, required checklist items or acceptance
thresholds are changed by this publication. Historical failed/skipped diagnostic
runs remain diagnostics; they are not promoted to passing release evidence.
The full feature checklist remains authoritative and is retained locally.

Workstation path prefixes were replaced with generic public paths in text and
compressed evidence. Equal-width replacements preserve offsets in retained ELF
and profiling data. Archive user metadata was generalized, and archives were
recompressed. Historical source/binary/archive hashes still describe the original
measured artifacts. They must not be mistaken for hashes of sanitized copies.
The [artifact manifest](docs/publication/artifact-manifest.json) records original
and public SHA-256 values and public sizes.

Ten archives exceed GitHub's per-file limit and are not published.

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

The initial public package version is **1.0.1**, tagged `v1.0.1`. The executable's
Rust implementation and dependency versions are unchanged from the validated CNPG
milestone; package version and license metadata have been updated. Each release
publishes only `linux/amd64` and `linux/arm64` images to
`ghcr.io/wesbragagt/rustybouncer`.
The installed verification hook checks publication commits normally. Full platform/security/release acceptance remains
open as recorded in the completeness checklist.

