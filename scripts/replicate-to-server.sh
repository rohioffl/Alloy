#!/bin/bash
# Replicate Monitoring Stack to Another Server
# Usage: ./replicate-to-server.sh user@new-server new-server-ip

set -e

# Colors
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

NEW_SERVER="$1"
NEW_SERVER_IP="$2"
INCLUDE_DATA="$3"

show_usage() {
    cat <<EOF
Replicate Alloy Monitoring Stack to Another Server

Usage: $0 <user@new-server> <new-server-ip> [--with-data]

Arguments:
    user@new-server     SSH connection string (e.g., ubuntu@192.168.1.100)
    new-server-ip       IP address of new server (for .env configuration)
    --with-data         Include existing data (Prometheus metrics, Grafana users, etc.)

Examples:
    # Replicate code only (fresh installation)
    $0 ubuntu@192.168.1.100 192.168.1.100

    # Replicate with data migration
    $0 ubuntu@192.168.1.100 192.168.1.100 --with-data

What Gets Replicated:
    ✅ All dashboards (13 dashboards)
    ✅ All configurations (Prometheus, Grafana, API)
    ✅ All alert rules
    ✅ All scripts
    ✅ Docker Compose stack definition
    $([ "$INCLUDE_DATA" = "--with-data" ] && echo "✅ Existing data (metrics, users, settings)")

What Gets Generated Fresh:
    🔑 New security tokens (MONITOR_INSTALL_TOKEN, MONITOR_API_KEY)
    🔑 New Grafana admin password (you'll set this)
    ⚙️  Server-specific .env file
EOF
}

if [ -z "$NEW_SERVER" ] || [ -z "$NEW_SERVER_IP" ]; then
    show_usage
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

print_header "Replicating Alloy Monitoring Stack"

echo "Source: $(hostname)"
echo "Target: $NEW_SERVER ($NEW_SERVER_IP)"
echo "Include Data: $([ "$INCLUDE_DATA" = "--with-data" ] && echo "Yes" || echo "No (fresh install)")"
echo ""

# Confirm
read -p "Continue? (y/n): " confirm
if [ "$confirm" != "y" ]; then
    echo "Cancelled"
    exit 0
fi

# Step 1: Check SSH connectivity
print_info "Testing SSH connection..."
if ssh -o ConnectTimeout=5 -o BatchMode=yes "$NEW_SERVER" "echo 2>&1" >/dev/null; then
    print_success "SSH connection successful"
else
    print_error "Cannot connect to $NEW_SERVER"
    echo "Make sure:"
    echo "  1. Server is reachable"
    echo "  2. SSH key is configured"
    echo "  3. User has sudo privileges"
    exit 1
fi

# Step 2: Create deployment package
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
    -C "$(dirname "$PROJECT_DIR")" "$(basename "$PROJECT_DIR")" 2>/dev/null

print_success "Package created: ${PACKAGE_NAME}"

# Step 3: Backup data if requested
if [ "$INCLUDE_DATA" = "--with-data" ]; then
    print_info "Backing up data volumes..."

    # Check if volumes exist
    if docker volume ls | grep -q "prometheus-data"; then
        docker run --rm -v prometheus-data:/data -v "${TEMP_DIR}":/backup \
            alpine tar czf /backup/prometheus-data.tar.gz -C /data . 2>/dev/null
        print_success "Prometheus data backed up"
    else
        print_warning "No Prometheus data to backup"
    fi

    if docker volume ls | grep -q "grafana-data"; then
        docker run --rm -v grafana-data:/data -v "${TEMP_DIR}":/backup \
            alpine tar czf /backup/grafana-data.tar.gz -C /data . 2>/dev/null
        print_success "Grafana data backed up"
    else
        print_warning "No Grafana data to backup"
    fi

    if docker volume ls | grep -q "monitor-data"; then
        docker run --rm -v monitor-data:/data -v "${TEMP_DIR}":/backup \
            alpine tar czf /backup/monitor-data.tar.gz -C /data . 2>/dev/null
        print_success "Monitor API data backed up"
    else
        print_warning "No Monitor API data to backup"
    fi

    if docker volume ls | grep -q "uptime-kuma-data"; then
        docker run --rm -v uptime-kuma-data:/data -v "${TEMP_DIR}":/backup \
            alpine tar czf /backup/uptime-kuma-data.tar.gz -C /data . 2>/dev/null
        print_success "Uptime Kuma data backed up"
    fi
fi

# Step 4: Copy to new server
print_info "Copying files to new server..."
ssh "$NEW_SERVER" "mkdir -p /tmp/monitoring-deploy"
scp "${TEMP_DIR}"/*.tar.gz "${NEW_SERVER}:/tmp/monitoring-deploy/" >/dev/null 2>&1
print_success "Files copied"

# Step 5: Deploy on new server
print_info "Deploying on new server..."

ssh "$NEW_SERVER" "bash -s" <<ENDSSH
set -e

# Colors for remote output
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

echo -e "\${BLUE}▶ Installing on ${NEW_SERVER}...\${NC}"

# Check Docker
if ! command -v docker &> /dev/null; then
    echo -e "\${YELLOW}⚠\${NC} Docker not found, installing..."
    curl -fsSL https://get.docker.com | sudo bash
    sudo usermod -aG docker \$USER
    echo -e "\${GREEN}✓\${NC} Docker installed"
fi

# Extract
cd /home/\$USER
tar xzf /tmp/monitoring-deploy/${PACKAGE_NAME}
cd monitoring

# Create .env with new tokens
cp .env.example .env

# Generate new tokens
INSTALL_TOKEN=\$(openssl rand -hex 32)
API_KEY=\$(openssl rand -hex 32)

echo "" >> .env
echo "# Generated tokens" >> .env
echo "MONITOR_INSTALL_TOKEN=\${INSTALL_TOKEN}" >> .env
echo "MONITOR_API_KEY=\${API_KEY}" >> .env

# Update URLs
sed -i "s|http://localhost:9099|http://${NEW_SERVER_IP}:9099|g" .env
sed -i "s|http://localhost:3000|http://${NEW_SERVER_IP}:3000|g" .env

echo -e "\${GREEN}✓\${NC} Configuration updated for ${NEW_SERVER_IP}"

# Restore data if provided
if [ "$INCLUDE_DATA" = "--with-data" ]; then
    echo -e "\${BLUE}▶ Restoring data volumes...\${NC}"

    # Create volumes
    docker volume create prometheus-data >/dev/null 2>&1 || true
    docker volume create grafana-data >/dev/null 2>&1 || true
    docker volume create monitor-data >/dev/null 2>&1 || true
    docker volume create uptime-kuma-data >/dev/null 2>&1 || true

    # Restore
    [ -f /tmp/monitoring-deploy/prometheus-data.tar.gz ] && \
        docker run --rm -v prometheus-data:/data -v /tmp/monitoring-deploy:/backup \
        alpine tar xzf /backup/prometheus-data.tar.gz -C /data 2>/dev/null && \
        echo -e "\${GREEN}✓\${NC} Prometheus data restored"

    [ -f /tmp/monitoring-deploy/grafana-data.tar.gz ] && \
        docker run --rm -v grafana-data:/data -v /tmp/monitoring-deploy:/backup \
        alpine tar xzf /backup/grafana-data.tar.gz -C /data 2>/dev/null && \
        echo -e "\${GREEN}✓\${NC} Grafana data restored"

    [ -f /tmp/monitoring-deploy/monitor-data.tar.gz ] && \
        docker run --rm -v monitor-data:/data -v /tmp/monitoring-deploy:/backup \
        alpine tar xzf /backup/monitor-data.tar.gz -C /data 2>/dev/null && \
        echo -e "\${GREEN}✓\${NC} Monitor API data restored"

    [ -f /tmp/monitoring-deploy/uptime-kuma-data.tar.gz ] && \
        docker run --rm -v uptime-kuma-data:/data -v /tmp/monitoring-deploy:/backup \
        alpine tar xzf /backup/uptime-kuma-data.tar.gz -C /data 2>/dev/null && \
        echo -e "\${GREEN}✓\${NC} Uptime Kuma data restored"
fi

# Deploy
chmod +x scripts/deploy-docker-stack.sh

echo -e "\${BLUE}▶ Starting services...\${NC}"
./scripts/deploy-docker-stack.sh start >/dev/null 2>&1

# Wait for health
echo -e "\${BLUE}▶ Waiting for services to be healthy...\${NC}"
sleep 10

# Check health
API_HEALTH=\$(curl -sf http://localhost:9099/health 2>/dev/null || echo "down")
if [ "\$API_HEALTH" != "down" ]; then
    echo -e "\${GREEN}✓\${NC} Monitor API is healthy"
fi

PROM_HEALTH=\$(curl -sf http://localhost:9090/-/healthy 2>/dev/null || echo "down")
if [ "\$PROM_HEALTH" != "down" ]; then
    echo -e "\${GREEN}✓\${NC} Prometheus is healthy"
fi

GRAFANA_HEALTH=\$(curl -sf http://localhost:3000/api/health 2>/dev/null | grep -o '"database":"ok"' || echo "down")
if [ "\$GRAFANA_HEALTH" != "down" ]; then
    echo -e "\${GREEN}✓\${NC} Grafana is healthy"
fi

# Cleanup
rm -rf /tmp/monitoring-deploy

echo ""
echo -e "\${GREEN}========================================\${NC}"
echo -e "\${GREEN}  Deployment Complete!\${NC}"
echo -e "\${GREEN}========================================\${NC}"
echo ""
echo "Access services at:"
echo "  Grafana:      http://${NEW_SERVER_IP}:3000"
echo "  Prometheus:   http://${NEW_SERVER_IP}:9090"
echo "  Monitor API:  http://${NEW_SERVER_IP}:9099"
echo "  Uptime Kuma:  http://${NEW_SERVER_IP}:3001"
echo ""
echo "Important tokens (save these!):"
echo "  MONITOR_INSTALL_TOKEN: \${INSTALL_TOKEN}"
echo "  MONITOR_API_KEY:       \${API_KEY}"
echo ""
echo "Next steps:"
echo "  1. Edit .env to set GRAFANA_ADMIN_PASS"
echo "  2. Configure SMTP settings (if using alerts)"
echo "  3. Restart: ./scripts/deploy-docker-stack.sh restart"

ENDSSH

# Cleanup local temp
rm -rf "$TEMP_DIR"

print_success "Replication complete!"

echo ""
print_header "Summary"
echo "✅ Code and configurations replicated"
echo "✅ Docker stack deployed on ${NEW_SERVER}"
$([ "$INCLUDE_DATA" = "--with-data" ] && echo "✅ Data migrated from source server")
echo "✅ New security tokens generated"
echo ""
echo "Access new server:"
echo "  Grafana:      http://${NEW_SERVER_IP}:3000"
echo "  Prometheus:   http://${NEW_SERVER_IP}:9090"
echo "  Monitor API:  http://${NEW_SERVER_IP}:9099"
echo ""
echo "SSH to new server:"
echo "  ssh ${NEW_SERVER}"
echo ""
echo "View logs on new server:"
echo "  ssh ${NEW_SERVER} 'cd monitoring && ./scripts/deploy-docker-stack.sh logs'"
