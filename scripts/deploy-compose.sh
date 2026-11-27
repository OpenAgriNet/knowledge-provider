#!/usr/bin/env bash
# One-command docker compose deploy for registry / sandbox hosts.
#
# Usage (on the server, from the repo root):
#   ./scripts/deploy-compose.sh            # build + up
#   NO_BUILD=1 ./scripts/deploy-compose.sh # skip build (use preloaded images)
#
# Host port defaults (override in .env):
#   API_HOST_PORT=8011   UI_HOST_PORT=3011
#   TEMPORAL_UI_PORT=8090
#
# Required nginx snippet locations (see deploy/):
#   /knowledge-provider-api/ → 127.0.0.1:8011
#   /knowledge-provider/     → 127.0.0.1:3011
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.yml}"
ENV_FILE="${ENV_FILE:-.env}"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "ERROR: $ENV_FILE not found — copy from .env.example and fill all secrets."
  exit 1
fi

export KP_ENV_FILE="$(cd "$(dirname "$ENV_FILE")" && pwd)/$(basename "$ENV_FILE")"

BUILD_FLAG=(--build)
if [[ "${NO_BUILD:-}" == "1" ]]; then
  BUILD_FLAG=()
fi

echo "===> compose file : $COMPOSE_FILE"
echo "===> env file     : $ENV_FILE"
echo "===> build flag   : ${BUILD_FLAG[*]:-<skipped>}"
echo ""

# ── 1. Pull images that are not built locally ─────────────────────────────────
echo "===> Pulling base images..."
docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" pull \
  postgres temporal-schema temporal temporal-namespace keycloak minio qdrant

# ── 2. Build or pull application images ───────────────────────────────────────
if [[ ${#BUILD_FLAG[@]} -gt 0 ]]; then
  echo "===> Building application images..."
  # Layer cache is kept by default — most redeploys only change pipeline/ or
  # ui/src, so requirements.txt / package.json layers (pip, npm, torch) reuse
  # cache instead of re-downloading every time. Set FORCE_NO_CACHE=1 for a
  # genuinely clean rebuild (e.g. after suspecting a stale/corrupt layer).
  NO_CACHE_FLAG=()
  if [[ "${FORCE_NO_CACHE:-}" == "1" ]]; then
    NO_CACHE_FLAG=(--no-cache)
  fi
  docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" build \
    "${NO_CACHE_FLAG[@]}" api worker ui
else
  echo "===> Pulling application images (IMAGE_TAG=${IMAGE_TAG:-latest})..."
  docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" pull \
    api worker ui
fi

# ── 3. Bring the stack up ─────────────────────────────────────────────────────
echo "===> Starting stack (detached)..."
docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" up -d

# ── 4. Status ─────────────────────────────────────────────────────────────────
echo ""
echo "===> Container status:"
docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" ps

# ── 5. Health checks ─────────────────────────────────────────────────────────
published_port() {
  local binding
  binding="$(docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" port "$1" "$2")"
  printf '%s\n' "${binding##*:}"
}

API_PORT="$(published_port api 8001)"
UI_PORT="$(published_port ui 8080)"

echo ""
echo "===> Waiting 10s for services to initialise..."
sleep 10

echo -n "  API     : "; curl -sf "http://127.0.0.1:${API_PORT}/health" && echo "OK" || echo "FAIL (may still be starting)"
echo -n "  UI      : "; curl -sf --max-time 3 "http://127.0.0.1:${UI_PORT}/" -o /dev/null && echo "OK" || echo "FAIL (may still be starting)"
echo -n "  Keycloak: "
docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" exec -T api \
  python -c 'import os; from urllib.request import urlopen; urlopen(os.environ["KEYCLOAK_ISSUER"].rstrip("/") + "/.well-known/openid-configuration", timeout=5).close()' \
  && echo "OK (private)" || echo "FAIL (check API startup and internal KEYCLOAK_ISSUER)"

# ── 6. Summary ────────────────────────────────────────────────────────────────
cat <<EOF

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 Deployed  (localhost binds — sit behind nginx)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 API       http://127.0.0.1:${API_PORT}/health
 UI        http://127.0.0.1:${UI_PORT}/
 Keycloak  private to the Compose network
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 Public URLs (after nginx — see deploy/ snippets)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 https://<your-domain>/knowledge-provider-api/health
 https://<your-domain>/knowledge-provider/
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 Logs
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 docker compose -f $COMPOSE_FILE --env-file $ENV_FILE logs -f api worker keycloak
EOF
