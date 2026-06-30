#!/usr/bin/env bash
# Apply Zentra logos to a running Grafana Docker container.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$SCRIPT_DIR")"
ASSETS="${ROOT}/branding/generated"
CONTAINER="${GRAFANA_CONTAINER:-grafana}"
ORG_NAME="${1:-Zentra}"

if [ ! -f "${ASSETS}/grafana_icon.svg" ]; then
  echo "Generating branding assets..."
  bash "${SCRIPT_DIR}/generate-branding-assets.sh"
fi

if ! docker ps --format '{{.Names}}' | grep -qx "$CONTAINER"; then
  echo "Grafana container '$CONTAINER' is not running." >&2
  exit 1
fi

apply_file() {
  local src="$1" dest="$2"
  docker cp "$src" "${CONTAINER}:${dest}"
}

echo "Applying Zentra branding to container: ${CONTAINER}"

# Grafana 13 login + header use hashed assets under build/static/img/
for f in $(docker exec "$CONTAINER" sh -c 'ls /usr/share/grafana/public/build/static/img/grafana_icon.*.svg 2>/dev/null'); do
  apply_file "${ASSETS}/grafana_icon.svg" "$f"
done
for f in $(docker exec "$CONTAINER" sh -c 'ls /usr/share/grafana/public/build/static/img/grafana_text_logo_dark.*.svg 2>/dev/null'); do
  apply_file "${ASSETS}/grafana_text_logo_dark.svg" "$f"
done
for f in $(docker exec "$CONTAINER" sh -c 'ls /usr/share/grafana/public/build/static/img/grafana_text_logo_light.*.svg 2>/dev/null'); do
  apply_file "${ASSETS}/grafana_text_logo_light.svg" "$f"
done

# Legacy / provisioning paths
for target in \
  /usr/share/grafana/public/img/grafana_icon.svg \
  /usr/share/grafana/public/img/grafana_typelogo.svg \
  /usr/share/grafana/public/img/grafana_text_logo.svg \
  /usr/share/grafana/public/img/grafana_text_logo-dark.svg \
  /usr/share/grafana/public/img/grafana_text_logo_dark.svg \
  /usr/share/grafana/public/img/grafana_text_logo_light.svg \
  /usr/share/grafana/public/build/img/grafana_icon.svg \
  /usr/share/grafana/public/build/img/grafana_typelogo.svg \
  /usr/share/grafana/public/build/img/grafana_text_logo-dark.svg \
  /usr/share/grafana/public/build/img/grafana_text_logo-light.svg \
  /usr/share/grafana/public/build/img/grafana_text_logo_dark.svg \
  /usr/share/grafana/public/build/img/grafana_text_logo_light.svg; do
  case "$target" in
    *grafana_icon*) src="${ASSETS}/grafana_icon.svg" ;;
    *typelogo*) src="${ASSETS}/grafana_typelogo.svg" ;;
    *light*) src="${ASSETS}/grafana_text_logo_light.svg" ;;
    *) src="${ASSETS}/grafana_text_logo_dark.svg" ;;
  esac
  apply_file "$src" "$target" 2>/dev/null || true
done

# Favicons
apply_file "${ASSETS}/fav32.png" /usr/share/grafana/public/img/fav32.png
apply_file "${ASSETS}/fav16.png" /usr/share/grafana/public/img/fav16.png
apply_file "${ASSETS}/apple-touch-icon.png" /usr/share/grafana/public/img/apple-touch-icon.png

# Login screen theme (background + card styling)
BRANDING="${ROOT}/branding"
docker exec -u root "$CONTAINER" mkdir -p /usr/share/grafana/public/custom
apply_file "${BRANDING}/login-background-dark.svg" /usr/share/grafana/public/custom/login-background-dark.svg
apply_file "${BRANDING}/zentra-login.css" /usr/share/grafana/public/custom/zentra.css
apply_file "${BRANDING}/zentra-sidebar.css" /usr/share/grafana/public/custom/zentra-sidebar.css
for f in $(docker exec "$CONTAINER" sh -c 'ls /usr/share/grafana/public/build/static/img/g8_login_dark.*.svg 2>/dev/null'); do
  apply_file "${BRANDING}/login-background-dark.svg" "$f"
done
for f in $(docker exec "$CONTAINER" sh -c 'ls /usr/share/grafana/public/build/static/img/g8_login_light.*.svg 2>/dev/null'); do
  apply_file "${BRANDING}/login-background-dark.svg" "$f"
done
apply_file "${BRANDING}/login-background-dark.svg" /usr/share/grafana/public/img/login_background_dark.svg
apply_file "${BRANDING}/login-background-dark.svg" /usr/share/grafana/public/img/g8_login_dark.svg
docker exec -u root "$CONTAINER" sh -c \
  'grep -q zentra.css /usr/share/grafana/public/views/index.html || sed -i "s|</head>|<link rel=\"stylesheet\" href=\"/public/custom/zentra.css\" /></head>|" /usr/share/grafana/public/views/index.html'

# Organization name + app title already set via grafana-zentra.ini; update org via API
if [ -f "${ROOT}/.env" ]; then
  # shellcheck disable=SC1091
  source <(grep -E '^(GRAFANA_ADMIN_USER|GRAFANA_ADMIN_PASS|GRAFANA_ROOT_URL)=' "${ROOT}/.env" | sed 's/^/export /')
fi
GRAFANA_URL="${GRAFANA_URL:-http://localhost:3000}"
GRAFANA_ADMIN_USER="${GRAFANA_ADMIN_USER:-admin}"
GRAFANA_ADMIN_PASS="${GRAFANA_ADMIN_PASS:-admin}"

curl -sf -X PUT -u "${GRAFANA_ADMIN_USER}:${GRAFANA_ADMIN_PASS}" \
  "${GRAFANA_URL}/api/org" \
  -H "Content-Type: application/json" \
  -d "{\"name\":\"${ORG_NAME}\"}" >/dev/null && echo "Organization name set to: ${ORG_NAME}"

echo "Zentra branding applied. Hard-refresh your browser (Ctrl+Shift+R)."
