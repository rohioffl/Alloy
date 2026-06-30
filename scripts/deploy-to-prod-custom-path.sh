#!/bin/bash
# Deploy Alloy Monitoring to Production Server with Custom Path

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

SSH_HOST="zentra-prod"
PUBLIC_IP="3.66.48.205"
PRIVATE_IP="10.100.0.195"
DOMAIN="zentra.ankercloud.com"
TARGET_DIR="${1:-/opt/monitoring}"
ORG_NAME="${2:-Zentra Monitoring}"

show_usage() {
    cat <<EOF
Deploy Alloy Monitoring to Production with Custom Path

Usage: $0 [target-directory] [organization-name]

Arguments:
    target-directory     Directory on production server (default: /opt/monitoring)
    organization-name    Grafana organization name (default: Zentra Monitoring)

Examples:
    # Deploy to /opt/monitoring
    $0

    # Deploy to custom path
    $0 /home/ubuntu/alloy-monitoring "My Company"

    # Deploy to /srv/monitoring
    $0 /srv/monitoring "Production Monitoring"
EOF
}

if [ "$1" = "-h" ] || [ "$1" = "--help" ]; then
    show_usage
    exit 0
fi

print_header "Deploy to Production: $TARGET_DIR"

echo "SSH Host:      $SSH_HOST"
echo "Public IP:     $PUBLIC_IP"
echo "Target Path:   $TARGET_DIR"
echo "Organization:  $ORG_NAME"
echo ""

# Confirm
read -p "Deploy to PRODUCTION at $TARGET_DIR? (type 'yes' to confirm): " confirm
if [ "$confirm" != "yes" ]; then
    echo "Cancelled"
    exit 0
fi

# Test SSH connection
print_info "Testing SSH connection..."
if ! ssh $SSH_HOST "echo 2>&1" >/dev/null 2>&1; then
    print_error "Cannot connect to $SSH_HOST"
    echo "Make sure SSH config is correct: ~/.ssh/config"
    exit 1
fi
print_success "SSH connection OK"

# Create target directory on production
print_info "Creating target directory on production..."
ssh $SSH_HOST "sudo mkdir -p $TARGET_DIR && sudo chown \$USER:\$USER $TARGET_DIR"
print_success "Directory created: $TARGET_DIR"

# Package monitoring stack
print_info "Creating deployment package..."
TEMP_DIR=$(mktemp -d)
PACKAGE_NAME="monitoring-deploy-$(date +%Y%m%d-%H%M%S).tar.gz"

tar czf "${TEMP_DIR}/${PACKAGE_NAME}" \
    --exclude='.git' \
    --exclude='.env' \
    --exclude='.env.local' \
    --exclude='api/.venv' \
    --exclude='**/__pycache__' \
    --exclude='*.pyc' \
    --exclude='docker-compose.override.yml' \
    -C $(dirname $(pwd)) $(basename $(pwd)) 2>/dev/null

print_success "Package created"

# Copy to production
print_info "Copying to production server..."
ssh $SSH_HOST "mkdir -p /tmp/monitoring-deploy"
scp "${TEMP_DIR}/${PACKAGE_NAME}" "${SSH_HOST}:/tmp/monitoring-deploy/" >/dev/null 2>&1
print_success "Files copied"

# Deploy on production
print_info "Deploying on production server..."

ssh $SSH_HOST "bash -s" <<ENDSSH
set -e

# Colors for remote output
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

echo -e "\${BLUE}▶ Installing on production server...\${NC}"

# Check Docker
if ! command -v docker &> /dev/null; then
    echo -e "\${YELLOW}⚠\${NC} Docker not found, installing..."
    curl -fsSL https://get.docker.com | sudo bash
    sudo usermod -aG docker \$USER
    echo -e "\${GREEN}✓\${NC} Docker installed"
fi

# Extract to target directory
cd $(dirname ${TARGET_DIR})
tar xzf /tmp/monitoring-deploy/${PACKAGE_NAME}
cd ${TARGET_DIR}

# Create .env with new tokens
cp .env.example .env

# Generate new tokens
INSTALL_TOKEN=\$(openssl rand -hex 32)
API_KEY=\$(openssl rand -hex 32)

echo "" >> .env
echo "# Generated tokens" >> .env
echo "MONITOR_INSTALL_TOKEN=\${INSTALL_TOKEN}" >> .env
echo "MONITOR_API_KEY=\${API_KEY}" >> .env

# Update URLs (use domain for Grafana, IP for API)
sed -i "s|http://localhost:9099|http://${PUBLIC_IP}:9099|g" .env
sed -i "s|http://localhost:3000|https://${DOMAIN}|g" .env
sed -i "s|GRAFANA_ROOT_URL=.*|GRAFANA_ROOT_URL=https://${DOMAIN}|g" .env

echo -e "\${GREEN}✓\${NC} Configuration created at ${TARGET_DIR}/.env"

# Make scripts executable
chmod +x scripts/*.sh

# Build custom Grafana with branding
echo -e "\${BLUE}▶ Building custom Grafana with your logos...\${NC}"
docker compose -f docker-compose.full.yml build grafana >/dev/null 2>&1
echo -e "\${GREEN}✓\${NC} Custom Grafana image built"

# Deploy
echo -e "\${BLUE}▶ Starting services...\${NC}"
docker compose -f docker-compose.full.yml up -d >/dev/null 2>&1

# Wait for services to be ready
echo -e "\${BLUE}▶ Waiting for services to start...\${NC}"
sleep 15

# Check health
API_HEALTH=\$(curl -sf http://localhost:9099/health 2>/dev/null || echo "down")
if [ "\$API_HEALTH" != "down" ]; then
    echo -e "\${GREEN}✓\${NC} Monitor API is healthy"
fi

GRAFANA_HEALTH=\$(curl -sf http://localhost:3000/api/health 2>/dev/null | grep -o '"database":"ok"' || echo "down")
if [ "\$GRAFANA_HEALTH" != "down" ]; then
    echo -e "\${GREEN}✓\${NC} Grafana is healthy"
fi

# Update organization name
sleep 5
curl -s -X PUT -u admin:admin http://localhost:3000/api/org \
    -H "Content-Type: application/json" \
    -d '{"name":"${ORG_NAME}"}' >/dev/null 2>&1 || true

# Cleanup
rm -rf /tmp/monitoring-deploy

echo ""
echo -e "\${GREEN}========================================\${NC}"
echo -e "\${GREEN}  Deployment Complete!\${NC}"
echo -e "\${GREEN}========================================\${NC}"
echo ""
echo "Installation Path: ${TARGET_DIR}"
echo ""
echo "Access services at:"
echo "  Grafana:      https://${DOMAIN} (setup HTTPS with setup-https-nginx.sh)"
echo "  Or via IP:    http://${PUBLIC_IP}:3000"
echo "  Prometheus:   http://${PUBLIC_IP}:9090"
echo "  Monitor API:  http://${PUBLIC_IP}:9099"
echo "  Uptime Kuma:  http://${PUBLIC_IP}:3001"
echo ""
echo "Grafana login:  admin / admin (change in .env)"
echo ""
echo "Important tokens (save these!):"
echo "  MONITOR_INSTALL_TOKEN: \${INSTALL_TOKEN}"
echo "  MONITOR_API_KEY:       \${API_KEY}"
echo ""
echo "Manage stack:"
echo "  cd ${TARGET_DIR}"
echo "  ./scripts/deploy-docker-stack.sh status"
echo "  ./scripts/deploy-docker-stack.sh logs"
echo "  ./scripts/deploy-docker-stack.sh restart"

ENDSSH

# Cleanup local temp
rm -rf "$TEMP_DIR"

print_success "Deployment complete!"

echo ""
print_header "Production Deployment Summary"

cat <<EOF
Installation completed at: ${TARGET_DIR}

Access your monitoring:
  External URL:  http://${PUBLIC_IP}:3000
  SSH:           ssh ${SSH_HOST}

Next Steps:
  1. SSH to production:
     ssh ${SSH_HOST}

  2. Update production passwords:
     cd ${TARGET_DIR}
     nano .env
     # Change: GRAFANA_ADMIN_PASS, SMTP settings
     ./scripts/deploy-docker-stack.sh restart

  3. Configure firewall:
     sudo ufw allow from YOUR_IP to any port 3000
     sudo ufw allow from 10.100.0.0/24 to any port 9099

  4. View logs:
     cd ${TARGET_DIR}
     ./scripts/deploy-docker-stack.sh logs

  5. Get install token for nodes:
     ssh ${SSH_HOST} "grep MONITOR_INSTALL_TOKEN ${TARGET_DIR}/.env | cut -d= -f2"

Manage production:
  ssh ${SSH_HOST}
  cd ${TARGET_DIR}
  ./scripts/deploy-docker-stack.sh [status|logs|restart|stop]

EOF

print_success "Production deployment successful!"
