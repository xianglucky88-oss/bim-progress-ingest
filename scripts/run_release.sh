#!/usr/bin/env bash
# Reproducible release entry point for CI and offline field machines.
#
#   ./scripts/run_release.sh build      deterministic build only
#   ./scripts/run_release.sh smoke      smoke checks against the source tree
#   ./scripts/run_release.sh verify     full operational verification
#   ./scripts/run_release.sh publish --dist DIR --releases-root ROOT
#   ./scripts/run_release.sh rollback --releases-root ROOT [--to NAME]
#   ./scripts/run_release.sh all        verify, then build into build/release
#
# The environment is pinned so residual caches, host timezones or hash seeds
# cannot influence output. No network access is required.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${PYTHON:-python3}"

export TZ=UTC
export PYTHONDONTWRITEBYTECODE=1
export PYTHONHASHSEED=0
export SOURCE_DATE_EPOCH="${SOURCE_DATE_EPOCH:-1793491200}"
unset PYTHONPATH || true

command="${1:-all}"
shift || true

case "$command" in
  build)
    exec "$PYTHON" -I -B "$REPO_ROOT/release/build.py" --out "$REPO_ROOT/build/release" "$@"
    ;;
  smoke)
    exec "$PYTHON" -I -B "$REPO_ROOT/release/smoke.py" "$REPO_ROOT" --python "$PYTHON" "$@"
    ;;
  verify)
    exec "$PYTHON" -I -B "$REPO_ROOT/release/verify.py" "$@"
    ;;
  publish)
    exec "$PYTHON" -I -B "$REPO_ROOT/release/publish.py" "$@"
    ;;
  rollback)
    exec "$PYTHON" -I -B "$REPO_ROOT/release/rollback.py" "$@"
    ;;
  all)
    "$PYTHON" -I -B "$REPO_ROOT/release/verify.py" "$@"
    "$PYTHON" -I -B "$REPO_ROOT/release/build.py" --out "$REPO_ROOT/build/release"
    echo "release artifacts: $REPO_ROOT/build/release/dist"
    ;;
  *)
    echo "unknown command: $command" >&2
    exit 64
    ;;
esac
