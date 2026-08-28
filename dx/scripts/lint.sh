#!/usr/bin/env bash
# Static checks only, no changes: ruff (lint + format check), mypy, Biome, tsc via vite build
# is left to check.sh. Exit code != 0 on any finding.
set -euo pipefail
cd "$(dirname "$0")/.."
# shellcheck disable=SC1091
. scripts/_pnpm.sh
status=0
echo "== backend: ruff check";        (cd backend && uv run ruff check .)          || status=1
echo "== backend: ruff format";       (cd backend && uv run ruff format --check .) || status=1
echo "== backend: mypy";              (cd backend && uv run mypy .)                || status=1
echo "== frontend: biome";            (cd frontend && pnpm lint)                   || status=1
exit $status
