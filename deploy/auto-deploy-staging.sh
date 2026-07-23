#!/usr/bin/env bash
set -euo pipefail

SOURCE_DIR="${GROKCLI_SOURCE_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
COMPOSE_DIR="${GROKCLI_COMPOSE_DIR:-$(dirname "$SOURCE_DIR")}"
SERVICE="grokcli-2api"
CONTAINER="daye-grokcli-2api-1"
IMAGE_REPO="daye/grokcli-2api"
LOCK_FILE="${GROKCLI_DEPLOY_LOCK:-/tmp/grokcli-2api-deploy.lock}"

exec 9>"$LOCK_FILE"
flock -n 9 || exit 0

cd "$SOURCE_DIR"

GIT_PROXY_URL="${GROKCLI_GIT_PROXY_URL:-http://127.0.0.1:7890}"

if [[ -n "$(git status --porcelain)" ]]; then
  echo "ERROR: source worktree is not clean" >&2
  exit 1
fi

git -c http.proxy="$GIT_PROXY_URL" -c https.proxy="$GIT_PROXY_URL" fetch --prune origin main
LOCAL_REV="$(git rev-parse main)"
REMOTE_REV="$(git rev-parse origin/main)"

if [[ "$LOCAL_REV" != "$REMOTE_REV" ]]; then
  git merge --ff-only origin/main
fi

NEW_REV="$(git rev-parse --short HEAD)"
OLD_IMAGE="$(docker inspect "$CONTAINER" --format '{{.Config.Image}}')"
NEW_IMAGE="${IMAGE_REPO}:${NEW_REV}"

if [[ "$OLD_IMAGE" == "$NEW_IMAGE" ]]; then
  echo "Already deployed image: $NEW_IMAGE"
  exit 0
fi

BUILD_PROXY_URL="${GROKCLI_BUILD_PROXY_URL:-http://10.0.0.6:7890}"

echo "Building $NEW_IMAGE using cache from $OLD_IMAGE"
docker build \
  --add-host=host.docker.internal:host-gateway \
  --pull=false \
  --cache-from "$OLD_IMAGE" \
  --secret id=github_token,src=/etc/daye/grokcli-github.env \
  --build-arg HTTP_PROXY="${BUILD_PROXY_URL}" \
  --build-arg HTTPS_PROXY="${BUILD_PROXY_URL}" \
  --build-arg ALL_PROXY="${BUILD_PROXY_URL}" \
  --label org.opencontainers.image.revision="$(git rev-parse HEAD)" \
  --label org.opencontainers.image.source="https://github.com/djzhhil/grokcli-2api" \
  -t "$NEW_IMAGE" .

ENV_FILE="$COMPOSE_DIR/.env"
ENV_BACKUP="$ENV_FILE.pre-${NEW_REV}"
cp "$ENV_FILE" "$ENV_BACKUP"
sed -i "s#^GROK2API_IMAGE=.*#GROK2API_IMAGE=${NEW_IMAGE}#" "$ENV_FILE"

rollback() {
  echo "Deployment failed; rolling back to $OLD_IMAGE" >&2
  cp "$ENV_BACKUP" "$ENV_FILE"
  docker compose -f "$COMPOSE_DIR/compose.yaml" up -d --no-build --no-deps "$SERVICE" || true
}
trap rollback ERR

docker compose -f "$COMPOSE_DIR/compose.yaml" up -d --no-build --no-deps "$SERVICE"

for _ in $(seq 1 60); do
  status="$(docker inspect "$CONTAINER" --format '{{.State.Status}}' 2>/dev/null || true)"
  health="$(docker inspect "$CONTAINER" --format '{{if .State.Health}}{{.State.Health.Status}}{{end}}' 2>/dev/null || true)"
  if [[ "$status" == "running" && "$health" == "healthy" ]]; then
    trap - ERR
    echo "Deployed $NEW_IMAGE"
    exit 0
  fi
  sleep 2
done

echo "ERROR: health check timed out" >&2
exit 1
