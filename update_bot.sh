#!/usr/bin/env bash
# Deploy bruggy-memory: build the image with the current git SHA as the build
# number and push it to the registry, mirroring the bruggy-forever update_bot.sh
# convention. Usage: ./update_bot.sh
set -euo pipefail

APP="clintshed/bruggy-memory"
SHA="${1:-$(git rev-parse --short HEAD)}"
VERSION="${2:-$SHA}"
REGISTRY="ghcr.io"

echo ">> Building ${APP}:${VERSION} (git ${SHA})"
docker build --build-arg APP_VERSION="${VERSION}" -t "${REGISTRY}/${APP}:${VERSION}" -t "${REGISTRY}/${APP}:latest" .

echo ">> Pushing ${REGISTRY}/${APP}:${VERSION}"
docker push "${REGISTRY}/${APP}:${VERSION}"
docker push "${REGISTRY}/${APP}:latest"

echo ">> Pulling the new image on the host and recreating the container"
echo "   ssh bruggy2  ->  docker pull ${REGISTRY}/${APP}:${VERSION} && docker compose up -d bruggy-memory"
echo ">> Done. Build ${VERSION} deployed."
