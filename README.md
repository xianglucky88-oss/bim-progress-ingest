# BIM Progress Ingest

Link BIM components to schedule activities and calculate revision-aware progress.

## Boundaries

| Concern | Location | Notes |
| --- | --- | --- |
| Source code | `src/bim_progress_ingest/` | domain logic only; no filesystem/cwd dependence beyond explicit inputs |
| Unit tests | `tests/` | runnable with stdlib `unittest`, no third-party deps |
| Fixtures | `fixtures/` | frozen inputs; see `fixtures/README.md`. `fixtures/corrupt/` is never packaged |
| Model revision samples | `fixtures/revisions/<rev>/` | each revision pins its input SHA-256 in `revision.json` |
| CLI | `src/bim_progress_ingest/cli.py` | deterministic JSON, structured errors, `--revision` flag |
| Release tooling | `release/` | stdlib-only build/smoke/publish/rollback/verify |
| Entry point (offline) | `release/launcher.py`, packaged as `launch.py` | self-locating; works under `python -I -B` from any cwd |
| Generated artifacts | `build/`, `dist/` (git-ignored) | never hand-edited |

## Run locally

```bash
# Deterministic CLI (default fixture resolves from the package, not the cwd)
PYTHONPATH=src python -B -m bim_progress_ingest.cli
PYTHONPATH=src python -B -m bim_progress_ingest.cli fixtures/input.json
PYTHONPATH=src python -B -m bim_progress_ingest.cli --revision rev-2026-11

# Unit tests
PYTHONPATH=src python -B -m unittest discover -s tests -v
```

The fixture is local and deterministic; no network, credentials or pip packages
are required.

## Release entry point

`scripts/run_release.sh` is the single repeatable entry point for CI and for
offline field machines. It pins `TZ=UTC`, `PYTHONHASHSEED=0`,
`SOURCE_DATE_EPOCH` and runs every interpreter with `-I -B` (isolated mode plus
no bytecode writes — note `-I` ignores `PYTHON*` env vars, so `-B` is required
to keep trees free of `__pycache__`).

```bash
./scripts/run_release.sh build      # deterministic tar.gz + zip + manifest
./scripts/run_release.sh smoke      # smoke checks against the source tree
./scripts/run_release.sh verify     # full operational verification (see below)
./scripts/run_release.sh all        # verify, then build into build/release
./scripts/run_release.sh publish --dist build/release/dist --releases-root /opt/bim/releases
./scripts/run_release.sh rollback --releases-root /opt/bim/releases [--to NAME]
```

### Determinism guarantees

* staging is rebuilt from scratch and only moved into place on success, so
  stale caches or old-revision artifacts can never merge into a release;
* archive members are sorted with fixed mtimes (`SOURCE_DATE_EPOCH`),
  uid/gid/mode and a reproducible gzip header — identical inputs yield
  byte-identical `tar.gz`/`zip` regardless of machine, timezone or cwd;
* `release-manifest.json` separates a **reproducible** section (input
  revisions, every bundled file's SHA-256/size, artifact hashes) from an
  **environment** section (runtime version, platform, build time, cwd). The
  reproducible section carries its own SHA-256 so repeat builds can be compared
  with one value.

### Smoke checks (`release/smoke.py`)

Run through `python -I -B` from a neutral cwd, against both the source tree and
an unpacked bundle: repeated-run byte identity, duplicated update rows,
missing updates (including no-updates-at-all), empty input, working-directory
switch invariance, cwd-independent default input, corrupt-fixture isolation,
missing-file error envelope, every model revision sample, tampered-revision
rejection, timezone/hash-seed invariance and a no-bytecode-cache assertion.

### Operational verification (`release/verify.py`)

1. **runtime matrix / clean environments** — each discoverable CPython gets a
   fresh `venv --without-pip`; source and bundle smoke plus bundle unit tests
   run with `-I -B`, and the venv is asserted to expose no third-party package.
   On a host with one interpreter the single-runtime checks still run fully;
   CI extends the matrix to 3.9–3.13 (`.github/workflows/release.yml`).
2. **repeatable build & unpack** — two builds in different TZ/cwd produce
   identical artifacts, manifests and SHA256SUMS; tar.gz and zip unpack to
   byte-identical trees.
3. **failure isolation** — interrupted builds (two injection points) leave no
   output; a tampered revision fails only its own checks; a read-only source
   tree builds without writing caches; an unwritable output fails loudly;
   stale previous-revision artifacts are purged and the rebuild converges to
   the same manifest hash.
4. **publish & rollback** — releases install into
   `<root>/releases/<version>-<sha>` with an atomic `current` symlink;
   installation is hash-verified against the manifest first; rollback is a
   re-verified symlink switch (no rebuild), exercised forward, backward and
   via `--dry-run`.

"Works on my machine" is not a release: a build is only published after the
verification report is green and the manifest hashes match.
