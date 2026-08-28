#!/usr/bin/env bash
# Start the Vite dev server (delegates to frontend/scripts/serve.sh). Logs: stdout + logs/frontend.log.
exec "$(dirname "$0")/../frontend/scripts/serve.sh" "$@"
