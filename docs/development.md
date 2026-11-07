# Development and commit verification

The flake was bootstrapped with Iceman (`iceman init`) and package additions use
`iceman add`. `AGENTS.md` records the generated Iceman conventions. `flake.lock`
pins nixpkgs and flake-utils; the pinned nixpkgs supplies one matching Rust/Cargo,
Clippy and rustfmt toolchain plus Ruff, Astral ty, and rust-code-analysis.

## Enter the environment

```sh
nix develop
# Or, with direnv installed:
direnv allow
```

Entering the shell installs the tracked `.githooks` directory via this checkout's
local `core.hooksPath`. No global Git setting is changed and an existing custom
hooksPath is not overwritten. To explicitly install without entering the shell:

```sh
bash scripts/install-hooks
```

During initial setup in an existing checkout, stage the new verification files
along with other changes before using the hook. Fresh clones already have these
tracked files in their index. The hook requires the staged flake and lock file. It fails if Nix
is missing, inputs/checks fail, or the snapshot lacks the lock file. Install hooks
in each clone; Git does not distribute local configuration. Like other Git hooks,
this can be bypassed explicitly by Git users (`--no-verify`); a protected CI gate
would be needed to enforce server-side policy.

## Run the canonical checks

```sh
nix flake check --no-update-lock-file
```

This builds the Rust package and runs verification in Nix's build environment,
with Cargo dependencies vendored from `Cargo.lock`. First use needs network access
to fetch pinned Nix inputs, tools and crate sources; subsequent builds can use the
Nix cache. The checks are:

- `cargo fmt --all -- --check`
- `cargo check --locked --offline --all-targets --all-features` (Rust typechecking)
- `cargo clippy --locked --offline --all-targets --all-features -- -D warnings`
- `cargo test --locked --offline --all-targets --all-features`
- `ruff check tests benchmarks/concurrency.py scripts conformance`
- `ty check tests benchmarks/concurrency.py scripts conformance` (Astral's Python type checker)
- `python3 scripts/check_complexity.py`
- `python3 -m unittest discover -s tests -p 'test_*.py'`
- Actionlint for the conformance CI workflow.
- Nixfmt and ShellCheck for the flake, verification scripts and commit hook.

Python checks cover the Docker integration harness, concurrency benchmark, and
verification helpers/tests. The current harnesses have dynamic/unannotated code;
ty catches detected type errors without claiming that every dynamic value is
statically typed. Python policy is explicit in `pyproject.toml`, so unrelated
ancestor configurations do not define project lint rules.

For an incremental check of the working tree after entering the shell:

```sh
cargo fetch --locked  # Populate the Cargo cache on first use.
bash scripts/verify
```

The standalone script uses Cargo offline mode; a new dev shell alone does not
populate Cargo's crate cache. The canonical Nix check vendors dependencies and
does not depend on the user's Cargo cache.

## What a commit verifies

The pre-commit hook archives `git write-tree` into a temporary directory, then runs
`nix flake check --no-update-lock-file` on that archive. It checks exactly the index,
including partially staged files. A good unstaged edit cannot mask a staged error.
It never stashes, resets, or rewrites the index/working tree. The temporary archive
is removed on exit. Nix reuses successful cached builds when the source is unchanged.

The package source excludes build/direnv output and saved benchmark-run artifacts;
it includes all Rust source, Python harnesses, verification configuration and hooks.
Nix checks run locally for the current system. The generated flake also exposes
Linux/macOS x86_64/aarch64 outputs, but only x86_64-linux was exercised here.

## Cyclomatic complexity

`pyproject.toml` sets `tool.rustybouncer.max-cyclomatic-complexity = 30`. Every Rust
and Python function/closure found under `src`, `tests`, `benchmarks`, `scripts`, and `conformance`
is measured using rust-code-analysis's tree-sitter parser. The gate fails when
**per-function McCabe cyclomatic complexity is greater than 30**: 30 passes, 31 fails.
Nested function totals are subtracted from the enclosing scope's aggregate, and
nested functions are checked independently. File/module aggregate complexity,
function length and argument count do not define this gate.

There are no source exclusions or per-function waivers. Prefer extracting coherent
helpers when a function exceeds the limit. Changing the tracked threshold changes
policy and should be reviewed explicitly; shell environment overrides are not used
by the commit hook. The checker fails if the analyzer/input is missing or no
functions are found. Boundary tests generate Rust and Python fixtures at CC30 and
CC31 and verify actual process exit status. Nested Python functions and Rust
closures have separate regression cases: their violations must be reported
without incorrectly charging their complexity to the enclosing function.

The initial reliable baseline found classifier CC45 and router session CC54.
Helper extraction brought them below the limit (the project peak immediately after that refactor was
CC23). Lizard was evaluated but rejected for Rust because it misidentified function
boundaries in this source; it is not a dependency or enforcement tool.

## Slower validation stays explicit

Docker and PostgreSQL client tools are in the dev shell, but Docker tests and load
measurements are not run for every commit. They need a running Docker daemon and
perform real process/container operations outside the Nix sandbox:

```sh
cargo build --locked
python3 tests/failover.py
cargo build --release --locked
python3 benchmarks/concurrency.py
```

Run the failover suite when routing, transaction state, discovery, or protocol
behavior changes. Saved benchmark results do not replace correctness checks.

The dev shell includes Psycopg for endpoint-only conformance and PgBouncer for
the optional reference adapter. Conformance harness unit tests run offline in
Nix verification; live endpoint scenarios run explicitly or in the CI workflow.
See [the conformance guide](../conformance/README.md).
