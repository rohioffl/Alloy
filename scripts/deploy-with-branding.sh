#!/bin/bash
# Deploy Alloy Monitoring with Custom Branding

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

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
ORG_NAME="${1:-Zentra}"

cd "$PROJECT_DIR"

print_header "Deploy Alloy Monitoring with Branding"

echo "Organization Name: $ORG_NAME"
echo ""

# Check if .env exists
if [ ! -f ".env" ]; then
    print_warning ".env file not found"
    print_info "Creating from template..."
    cp .env.example .env

    # Generate tokens
    INSTALL_TOKEN=$(openssl rand -hex 32)
    API_KEY=$(openssl rand -hex 32)

    echo "" >> .env
    echo "MONITOR_INSTALL_TOKEN=$INSTALL_TOKEN" >> .env
    echo "MONITOR_API_KEY=$API_KEY" >> .env

    print_success ".env created"
    print_warning "Please edit .env and update:"
    echo "  - GRAFANA_ADMIN_PASS"
    echo "  - MONITOR_PUBLIC_URL"
    echo "  - SMTP settings (if using email alerts)"
    echo ""
    read -p "Press Enter after editing .env..."
fi

# Build custom Grafana with logos
print_info "Building custom Grafana image with your logos..."
docker compose -f docker-compose.full.yml build grafana

print_success "Grafana image built with custom branding"

# Start the stack
print_info "Starting services..."
docker compose -f docker-compose.full.yml up -d

# Wait for Grafana to be ready
print_info "Waiting for Grafana to start..."
sleep 10

# Check if services are healthy
print_info "Checking service health..."

API_HEALTH=$(curl -sf http://localhost:9099/health 2>/dev/null || echo "down")
GRAFANA_HEALTH=$(curl -sf http://localhost:3000/api/health 2>/dev/null | grep -o '"database":"ok"' || echo "down")

if [ "$API_HEALTH" != "down" ]; then
    print_success "Monitor API is healthy"
fi

if [ "$GRAFANA_HEALTH" != "down" ]; then
    print_success "Grafana is healthy"
fi

# Update organization name
print_info "Updating Grafana organization name..."

GRAFANA_USER=$(grep GRAFANA_ADMIN_USER .env | cut -d= -f2 | tr -d '"' || echo "admin")
GRAFANA_PASS=$(grep GRAFANA_ADMIN_PASS .env | cut -d= -f2 | tr -d '"' || echo "admin")

sleep 5  # Wait a bit more for Grafana to fully initialize

response=$(curl -s -w "\n%{http_code}" -X PUT \
    -u "${GRAFANA_USER}:${GRAFANA_PASS}" \
    "http://localhost:3000/api/org" \
    -H "Content-Type: application/json" \
    -d "{\"name\":\"${ORG_NAME}\"}" 2>/dev/null)

http_code=$(echo "$response" | tail -n1)

if [ "$http_code" -eq 200 ]; then
    print_success "Organization name updated to: $ORG_NAME"
else
    print_warning "Could not update organization name (Grafana may still be starting)"
    print_info "Run this to update later:"
    echo "  curl -X PUT -u admin:password http://localhost:3000/api/org \\"
    echo "    -H 'Content-Type: application/json' \\"
    echo "    -d '{\"name\":\"$ORG_NAME\"}'"
fi

echo ""
print_header "Deployment Complete!"

echo "Access your monitoring platform:"
echo ""
echo "  Grafana:          http://localhost:3000"
echo "  Prometheus:       http://localhost:9090"
echo "  Monitor API:      http://localhost:9099"
echo "  Uptime Kuma:      http://localhost:3001"
echo ""
echo "Branding applied:"
echo "  ✅ Custom logo (from alert/logo.png)"
echo "  ✅ Organization name: $ORG_NAME"
echo ""
echo "Next steps:"
echo "  1. Login to Grafana (admin / password from .env)"
echo "  2. Check that your logo appears in:"
echo "     - Login page"
echo "     - Top-left header"
echo "     - Browser favicon"
echo "  3. Clear browser cache if old logo still shows (Ctrl+Shift+R)"
echo ""
print_info "View logs:"
echo "  docker compose -f docker-compose.full.yml logs -f"
echo ""
print_info "Stop services:"
echo "  docker compose -f docker-compose.full.yml down"
