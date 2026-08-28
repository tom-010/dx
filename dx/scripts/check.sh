#!/usr/bin/env bash
# Everything CI would run — use it before finishing a change (see CLAUDE.md):
# lint.sh, backend tests, frontend build (tsc + vite), and the generated-client drift check.
set -euo pipefail
cd "$(dirname "$0")/.."
# shellcheck disable=SC1091
. scripts/_pnpm.sh
./scripts/lint.sh
echo "== backend: pytest";     (cd backend && uv run pytest -q)
echo "== frontend: build";     (cd frontend && pnpm build)
echo "== sync_schema: --check"; ./scripts/sync_schema.sh --check
echo "all checks passed"
