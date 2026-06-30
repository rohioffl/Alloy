#!/bin/bash
# Apply Alloy Monitoring Branding to Grafana
# Replaces Grafana logo and updates organization name

set -e

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

print_header() {
    echo -e "${BLUE}========================================${NC}"
    echo -e "${BLUE}$1${NC}"
    echo -e "${BLUE}========================================${NC}"
    echo ""
}

print_success() { echo -e "${GREEN}✓${NC} $1"; }
print_error() { echo -e "${RED}✗${NC} $1"; }
print_warning() { echo -e "${YELLOW}⚠${NC} $1"; }
print_info() { echo -e "${BLUE}ℹ${NC} $1"; }

ORG_NAME="${1:-Zentra}"
GRAFANA_URL="${GRAFANA_URL:-http://localhost:3000}"
GRAFANA_USER="${GRAFANA_ADMIN_USER:-admin}"
GRAFANA_PASS="${GRAFANA_ADMIN_PASS:-admin}"

show_usage() {
    cat <<EOF
Apply Branding to Grafana

Usage: $0 [organization-name]

Arguments:
    organization-name    Organization name (default: "Zentra")

Environment Variables:
    GRAFANA_URL          Grafana URL (default: http://localhost:3000)
    GRAFANA_ADMIN_USER   Admin username (default: admin)
    GRAFANA_ADMIN_PASS   Admin password (default: admin)

Examples:
    # Use default name
    $0

    # Custom name
    $0 "Your Company Monitoring"

    # With Docker Compose
    docker compose -f docker-compose.full.yml exec grafana bash -c "
      $(cat $0)
    "

What This Does:
    1. Updates Grafana organization name
    2. Provides instructions for logo replacement
    3. Works with both bare metal and Docker installations
EOF
}

if [ "$1" = "-h" ] || [ "$1" = "--help" ]; then
    show_usage
    exit 0
fi

print_header "Applying Grafana Branding"

# Check if we're inside Grafana container
if [ -d "/usr/share/grafana/public/img" ]; then
    GRAFANA_IMG_DIR="/usr/share/grafana/public/img"
    IN_CONTAINER=true
    print_info "Running inside Grafana container"
else
    GRAFANA_IMG_DIR="/usr/share/grafana/public/img"
    IN_CONTAINER=false
    print_info "Running on host system"
fi

# Update organization name
print_info "Updating organization name to: $ORG_NAME"

response=$(curl -s -w "\n%{http_code}" -X PUT \
    -u "${GRAFANA_USER}:${GRAFANA_PASS}" \
    "${GRAFANA_URL}/api/org" \
    -H "Content-Type: application/json" \
    -d "{\"name\":\"${ORG_NAME}\"}")

http_code=$(echo "$response" | tail -n1)
body=$(echo "$response" | sed '$d')

if [ "$http_code" -eq 200 ]; then
    print_success "Organization name updated"
else
    print_error "Failed to update organization name (HTTP $http_code)"
    echo "$body"
fi

echo ""
print_header "Logo Replacement Instructions"

cat <<EOF
Your existing logo files:
  $(ls -lh /home/ubuntu/monitoring/alert/logo*.png 2>/dev/null || echo "Not found")

To replace Grafana logos, you need to:

${GREEN}For Docker Compose Setup:${NC}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Option 1: Build with Custom Logo (Recommended)
───────────────────────────────────────────────
1. Create custom Dockerfile that replaces logos during build:

   cat > Dockerfile.grafana <<'DOCKEREOF'
FROM grafana/grafana:13.0.1

USER root

# Copy your custom logos
COPY alert/logo.png /tmp/logo-source.png

# Convert and resize for different uses
RUN apk add --no-cache imagemagick && \\
    convert /tmp/logo-source.png -resize 32x32 /usr/share/grafana/public/img/grafana_icon.svg && \\
    convert /tmp/logo-source.png -resize 250x100 /usr/share/grafana/public/img/grafana_typelogo.svg && \\
    convert /tmp/logo-source.png -resize 150x40 /usr/share/grafana/public/img/grafana_text_logo.svg && \\
    rm /tmp/logo-source.png

USER grafana
DOCKEREOF

2. Update docker-compose.full.yml to use custom image:

   grafana:
     build:
       context: .
       dockerfile: Dockerfile.grafana
     # ... rest of config

3. Rebuild and restart:
   docker compose -f docker-compose.full.yml up -d --build grafana


Option 2: Volume Mount (Quick Testing)
───────────────────────────────────────
1. Convert PNG to SVG (if not already SVG):
   # Install ImageMagick if needed
   sudo apt-get install imagemagick

2. Create SVG versions:
   convert alert/logo.png -resize 32x32 custom-logos/grafana_icon.svg
   convert alert/logo.png -resize 250x100 custom-logos/grafana_typelogo.svg
   convert alert/logo.png -resize 150x40 custom-logos/grafana_text_logo.svg

3. Mount custom logos in docker-compose.full.yml:
   grafana:
     volumes:
       - ./custom-logos/grafana_icon.svg:/usr/share/grafana/public/img/grafana_icon.svg:ro
       - ./custom-logos/grafana_typelogo.svg:/usr/share/grafana/public/img/grafana_typelogo.svg:ro
       - ./custom-logos/grafana_text_logo.svg:/usr/share/grafana/public/img/grafana_text_logo-dark.svg:ro

4. Restart:
   docker compose -f docker-compose.full.yml restart grafana


${GREEN}For Bare Metal Installation:${NC}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Use the existing branding script:
  sudo ./scripts/update-grafana-branding.sh -n "$ORG_NAME" -d /path/to/logos

Or manually:
  sudo cp your-logo-icon.svg ${GRAFANA_IMG_DIR}/grafana_icon.svg
  sudo cp your-logo-full.svg ${GRAFANA_IMG_DIR}/grafana_typelogo.svg
  sudo cp your-logo-text.svg ${GRAFANA_IMG_DIR}/grafana_text_logo-dark.svg
  sudo systemctl restart grafana-server

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

${YELLOW}Note:${NC} PNG logos can be used but SVG is recommended for scalability.

Current organization: $ORG_NAME
EOF

echo ""
print_success "Branding configuration complete!"
echo ""
echo "Next steps:"
echo "  1. Follow logo replacement instructions above"
echo "  2. Clear browser cache (Ctrl+Shift+R)"
echo "  3. Reload Grafana"
