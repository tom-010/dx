#!/usr/bin/env bash
# Start the Vite dev server on http://localhost:5173/ (proxies /api to Django on :8000).
# --strictPort makes Vite fail instead of silently moving to 5174 when 5173 is taken.
# Output goes to stdout AND logs/frontend.log (repo root); the file starts fresh on every start.
set -euo pipefail
cd "$(dirname "$0")/.."
# pnpm is installed via nvm, which is only loaded in interactive shells.
if ! command -v pnpm >/dev/null 2>&1; then
  export NVM_DIR="${NVM_DIR:-$HOME/.nvm}"
  # shellcheck disable=SC1091
  [ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh"
fi
LOG="$(cd .. && pwd)/logs/frontend.log"
mkdir -p "$(dirname "$LOG")"
: > "$LOG"
echo "logging to $LOG" >&2
# --clearScreen false keeps the log file free of terminal clear sequences.
pnpm dev --host localhost --port 5173 --strictPort --clearScreen false "$@" 2>&1 | tee -a "$LOG"
