#!/usr/bin/env bash
set -euo pipefail
export PYTHONDONTWRITEBYTECODE=1

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
target="${1:-$PWD}"

python3 "$REPO_ROOT/core/agy_local_install.py" "$target" "$REPO_ROOT"
