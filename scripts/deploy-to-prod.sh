#!/bin/bash
# Deploy Alloy Monitoring to Production Server (zentra-prod)

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
ORG_NAME="${1:-Zentra Monitoring}"

print_header "Deploy to Production: zentra-prod"

echo "Target Server: $SSH_HOST ($PUBLIC_IP)"
echo "Organization:  $ORG_NAME"
echo ""

# Confirm
read -p "Deploy to PRODUCTION? (type 'yes' to confirm): " confirm
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

# Use replication script
print_info "Deploying monitoring stack to production..."

./scripts/replicate-to-server.sh $SSH_HOST $PUBLIC_IP

print_success "Base deployment complete"

# Update organization name on production
print_info "Updating organization name on production..."

ssh $SSH_HOST "cd monitoring && bash -c '
curl -X PUT -u admin:admin http://localhost:3000/api/org \
  -H \"Content-Type: application/json\" \
  -d \"{\\\"name\\\":\\\"$ORG_NAME\\\"}\" 2>/dev/null || true
'"

# Final instructions
print_header "Production Deployment Complete!"

cat <<EOF
Access your production monitoring:
  External URL:  http://${PUBLIC_IP}:3000
  Internal URL:  http://${PRIVATE_IP}:3000
  SSH:           ssh ${SSH_HOST}

Next steps on production:
  1. Configure firewall rules:
     # Allow Grafana from your IP only
     sudo ufw allow from YOUR_IP to any port 3000

     # Allow API from VPC only
     sudo ufw allow from 10.100.0.0/24 to any port 9099

     # Allow Prometheus remote_write from nodes
     sudo ufw allow from 10.0.0.0/8 to any port 9090

  2. Setup HTTPS (recommended):
     # Install Nginx reverse proxy
     sudo apt-get install nginx certbot python3-certbot-nginx

     # Get SSL certificate
     sudo certbot --nginx -d monitoring.yourdomain.com

  3. Update .env on production:
     ssh ${SSH_HOST}
     cd monitoring
     nano .env
     # Update: GRAFANA_ADMIN_PASS, SMTP settings

  4. Install Alloy agents on your nodes:
     curl -fsSL https://raw.githubusercontent.com/rohioffl/Alloy/main/install-alloy.sh | \\
       sudo bash -s -- \\
         -remote-write=http://${PUBLIC_IP}:9090/api/v1/write \\
         -install-token=\$(ssh ${SSH_HOST} 'grep MONITOR_INSTALL_TOKEN monitoring/.env | cut -d= -f2')

View production logs:
  ssh ${SSH_HOST} 'cd monitoring && ./scripts/deploy-docker-stack.sh logs'

Manage production stack:
  ssh ${SSH_HOST} 'cd monitoring && ./scripts/deploy-docker-stack.sh status'
  ssh ${SSH_HOST} 'cd monitoring && ./scripts/deploy-docker-stack.sh restart'

EOF

print_success "Production deployment successful!"
