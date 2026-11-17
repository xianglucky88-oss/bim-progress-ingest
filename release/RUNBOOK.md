# Release & operations runbook

Stdlib-only tooling (Python 3.9+). Nothing here needs pip, network access or
writable site-packages, so the same commands run in CI and on an offline
field machine.

## 1. Build a release

```bash
./scripts/run_release.sh build
# -> build/release/dist/{bim-progress-ingest-<v>.tar.gz, .zip, SHA256SUMS, release-manifest.json}
```

The build runs the unit tests first, stages into a fresh directory, normalizes
every archive entry (sorted order, fixed `SOURCE_DATE_EPOCH` mtime, fixed
uid/gid/mode, reproducible gzip header), round-trips both archives, then moves
the result into place atomically. Any failure deletes the partial output —
there is no successful-exit path that leaves a half-built release.

Verify the checksums after transferring artifacts to the field machine:

```bash
cd <dist> && sha256sum -c SHA256SUMS
```

## 2. Verify before deploying

```bash
./scripts/run_release.sh verify        # writes build/verify-report.json
```

This is the deployment gate, not the unit tests alone. It covers the runtime
matrix in clean venvs, byte-identical repeat builds/unpacks, the four failure
isolation scenarios, and publish/rollback. A non-zero exit blocks deployment.

## 3. Publish (activate) a release

```bash
python -I -B release/publish.py \
  --dist build/release/dist \
  --releases-root /opt/bim-progress/releases
```

The tarball is extracted into a temp directory, every file is checked against
the manifest's SHA-256 list, and only then is it moved to
`releases/<version>-<sha12>` and the `current` symlink switched atomically.
A corrupted or tampered bundle fails verification and is never activated.
`--dry-run` performs all checks without changing `current`.

## 4. Run the deployed software

```bash
/opt/bim-progress/releases/current/launch.py <input.json>
/opt/bim-progress/releases/current/launch.py --revision rev-2026-11
python -I -B /opt/bim-progress/releases/current/run_tests.py
```

`launch.py` locates `src/` relative to itself, so the invocation is
independent of the caller's working directory.

## 5. Roll back

```bash
python -I -B release/rollback.py --releases-root /opt/bim-progress/releases --list
python -I -B release/rollback.py --releases-root /opt/bim-progress/releases          # previous verified release
python -I -B release/rollback.py --releases-root /opt/bim-progress/releases --to 0.1.0-ab12cd34ef56
```

Rollback re-verifies the target's manifest before moving the symlink; it never
rebuilds or overwrites a release tree. Old releases are retained precisely so
rollback is a pointer change.

## Failure handling matrix

| Condition | Behavior |
| --- | --- |
| Build interrupted (staging or archive phase) | non-zero exit; output dir and staging dir removed; nothing publishable remains |
| One corrupt fixture | rejected in isolation with a structured error (`exit 2`); other smoke checks continue; `fixtures/corrupt/` is excluded from archives |
| Caches unavailable / read-only source | build still succeeds with `-B`; zero `.pyc` writes; unwritable output fails loudly |
| Stale previous-revision artifacts present | output is recreated from scratch; leftovers purged; rebuild yields the same manifest hash |
| Corrupted/tampered download at publish time | manifest hash mismatch aborts before the symlink changes |
| Tampered model revision sample | pinned `input_sha256` validation raises `RevisionError`; the revision cannot run silently |

## Reading the manifest

`release-manifest.json`:

* `reproducible` — pure function of the inputs: `source_date_epoch`,
  `input_versions` (canonical/empty fixture hashes and every model revision's
  pinned input hash), `bundle_files` (path + SHA-256 + size), and `artifacts`
  with their hashes.
* `manifest_reproducible_sha256` — hash of the canonicalized reproducible
  section; compare this single value across two builds to prove equivalence.
* `environment` — host runtime, platform, UTC build time and cwd, recorded for
  audit but deliberately outside the reproducible section.
