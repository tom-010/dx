#!/usr/bin/env bash
# Backend tests with coverage (config in backend/pyproject.toml [tool.coverage]).
#   ./scripts/coverage.sh          # terminal report + backend/htmlcov + backend/coverage.xml
#   ./scripts/coverage.sh --open   # ...and open the HTML report in the browser
# Other args are passed to pytest.
set -euo pipefail
cd "$(dirname "$0")/../backend"
open_report=false
args=()
for arg in "$@"; do
  if [ "$arg" = "--open" ]; then open_report=true; else args+=("$arg"); fi
done
uv run pytest --cov --cov-report=term-missing:skip-covered --cov-report=html --cov-report=xml "${args[@]}"
if [ "$open_report" = true ]; then
  xdg-open htmlcov/index.html >/dev/null 2>&1 || open htmlcov/index.html
fi
