#!/usr/bin/env bash
# Auto-format and auto-fix both halves of the repo (ruff for Python, Biome for TypeScript).
set -euo pipefail
cd "$(dirname "$0")/.."
# shellcheck disable=SC1091
. scripts/_pnpm.sh
echo "== backend (ruff)"
(cd backend && uv run ruff check --fix . && uv run ruff format .)
echo "== frontend (biome)"
(cd frontend && pnpm biome check --write .)
