#!/usr/bin/env bash
# Build the bundled image: Vite frontend + Django backend, served by one container.
#   ./scripts/build.sh          # docker build -> dx-app:latest
#   ./scripts/build.sh --run    # build, then start db + app via compose -> http://localhost:8080
set -euo pipefail
cd "$(dirname "$0")/.."
TAG="${IMAGE_TAG:-dx-app:latest}"
# Baked into the image as APP_VERSION (logs, Sentry release).
VERSION="${APP_VERSION:-$(git rev-parse --short HEAD 2>/dev/null || echo dev)}"
docker build -f docker/Dockerfile -t "$TAG" --build-arg "APP_VERSION=$VERSION" .
if [ "${1:-}" = "--run" ]; then
  exec docker compose -f docker/docker-compose.yml --profile app up -d --wait
fi
echo "Built $TAG. Run it with: ./scripts/build.sh --run  (or docker compose -f docker/docker-compose.yml --profile app up)"
