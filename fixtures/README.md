# Fixture boundary

All test and smoke inputs live under `fixtures/`. Nothing in `src/` or the
release tooling reads data from outside this tree, and no fixture is fetched
over the network. Paths are always passed explicitly to the CLI; output never
embeds absolute paths, so moving the working directory cannot change results.

| Path | Role | Packaged into releases? |
| --- | --- | --- |
| `fixtures/input.json` | Canonical smoke fixture used by the unit tests and CLI default | yes |
| `fixtures/empty/input.json` | Empty-input edge case (`components: []`) | yes |
| `fixtures/revisions/<rev>/input.json` | Model **revision samples**: deterministic per-revision input payloads | yes |
| `fixtures/revisions/<rev>/revision.json` | Revision metadata; pins the input's SHA-256 so a tampered sample fails validation | yes |
| `fixtures/corrupt/*.json` | Deliberately broken inputs for failure-isolation checks | **no** |

Rules:

1. The release build allow-lists only the canonical, empty and revision
   fixtures. `fixtures/corrupt/` is excluded from every archive; it exists only
   to prove the smoke runner isolates one bad fixture instead of aborting or
   publishing partial results.
2. Each revision sample is self-describing: `revision.json` declares
   `revision_id`, `input_file` and `input_sha256`. The release verification
   recomputes the hash and fails that revision in isolation on mismatch.
3. Fixtures are frozen inputs. To change expected behavior, add a new revision
   directory; never edit a released revision in place (that would break
   reproducibility and rollback).
4. Expected numeric results for the packaged fixtures are pinned in the
   release verification (`release/verify.py`), not duplicated as mutable
   golden files.
