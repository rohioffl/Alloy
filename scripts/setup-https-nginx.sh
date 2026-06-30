#!/bin/bash
# Setup HTTPS with Nginx and Let's Encrypt for Grafana

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

DOMAIN="${1:-zentra.ankercloud.com}"
EMAIL="${2:-admin@ankercloud.com}"

show_usage() {
    cat <<EOF
Setup HTTPS with Nginx and Let's Encrypt

Usage: $0 [domain] [email]

Arguments:
    domain    Domain name (default: zentra.ankercloud.com)
    email     Email for Let's Encrypt (default: admin@ankercloud.com)

Examples:
    $0
    $0 monitoring.yourdomain.com admin@yourdomain.com

Prerequisites:
    - Domain must point to this server's IP
    - Ports 80 and 443 must be open
    - Run this ON the production server (ssh zentra-prod)
EOF
}

if [ "$1" = "-h" ] || [ "$1" = "--help" ]; then
    show_usage
    exit 0
fi

print_header "Setup HTTPS for $DOMAIN"

echo "Domain: $DOMAIN"
echo "Email:  $EMAIL"
echo ""

# Check if running as root or with sudo
if [ "$EUID" -ne 0 ]; then
    print_error "This script must be run with sudo"
    echo "Usage: sudo $0 $DOMAIN $EMAIL"
    exit 1
fi

# Verify domain resolves to this server
print_info "Checking DNS resolution..."
SERVER_IP=$(curl -s ifconfig.me)
DOMAIN_IP=$(dig +short "$DOMAIN" | tail -n1)

if [ -z "$DOMAIN_IP" ]; then
    print_error "Domain $DOMAIN does not resolve"
    echo "Please configure DNS A record pointing to $SERVER_IP"
    exit 1
fi

if [ "$DOMAIN_IP" != "$SERVER_IP" ]; then
    print_warning "Domain resolves to $DOMAIN_IP but server IP is $SERVER_IP"
    read -p "Continue anyway? (y/n): " confirm
    if [ "$confirm" != "y" ]; then
        exit 0
    fi
fi

print_success "DNS check passed"

# Install Nginx and Certbot
print_info "Installing Nginx and Certbot..."
apt-get update -qq
apt-get install -y nginx certbot python3-certbot-nginx >/dev/null 2>&1
print_success "Nginx and Certbot installed"

# Create Nginx configuration
print_info "Creating Nginx configuration..."

cat > /etc/nginx/sites-available/monitoring <<EOF
# Grafana Monitoring - HTTPS Configuration
# Generated: $(date)

# Redirect HTTP to HTTPS
server {
    listen 80;
    listen [::]:80;
    server_name $DOMAIN;

    location /.well-known/acme-challenge/ {
        root /var/www/html;
    }

    location / {
        return 301 https://\$server_name\$request_uri;
    }
}

# HTTPS Configuration
server {
    listen 443 ssl http2;
    listen [::]:443 ssl http2;
    server_name $DOMAIN;

    # SSL certificates (will be added by certbot)
    # ssl_certificate /etc/letsencrypt/live/$DOMAIN/fullchain.pem;
    # ssl_certificate_key /etc/letsencrypt/live/$DOMAIN/privkey.pem;

    # SSL configuration
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_ciphers HIGH:!aNULL:!MD5;
    ssl_prefer_server_ciphers on;
    ssl_session_cache shared:SSL:10m;
    ssl_session_timeout 10m;

    # Security headers
    add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;
    add_header X-Frame-Options "SAMEORIGIN" always;
    add_header X-Content-Type-Options "nosniff" always;
    add_header X-XSS-Protection "1; mode=block" always;

    # Grafana proxy
    location / {
        proxy_pass http://localhost:3000;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;

        # WebSocket support (for live updates)
        proxy_http_version 1.1;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection "upgrade";

        # Increase timeouts for long queries
        proxy_read_timeout 300s;
        proxy_connect_timeout 75s;
    }

    # Prometheus (optional - access via /prometheus/)
    location /prometheus/ {
        proxy_pass http://localhost:9090/;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }

    # Monitor API (optional - access via /api/)
    location /monitor-api/ {
        proxy_pass http://localhost:9099/;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }

    # Health check endpoint
    location /health {
        access_log off;
        return 200 "OK";
        add_header Content-Type text/plain;
    }
}
EOF

print_success "Nginx configuration created"

# Enable site
print_info "Enabling Nginx site..."
ln -sf /etc/nginx/sites-available/monitoring /etc/nginx/sites-enabled/
rm -f /etc/nginx/sites-enabled/default

# Test Nginx configuration
if nginx -t 2>&1 | grep -q "successful"; then
    print_success "Nginx configuration is valid"
else
    print_error "Nginx configuration has errors"
    nginx -t
    exit 1
fi

# Reload Nginx
systemctl reload nginx
print_success "Nginx reloaded"

# Get SSL certificate
print_info "Obtaining SSL certificate from Let's Encrypt..."
print_warning "This will communicate with Let's Encrypt servers"

certbot --nginx -d "$DOMAIN" \
    --non-interactive \
    --agree-tos \
    --email "$EMAIL" \
    --redirect

if [ $? -eq 0 ]; then
    print_success "SSL certificate obtained successfully"
else
    print_error "Failed to obtain SSL certificate"
    print_info "Manual certificate request:"
    echo "  sudo certbot --nginx -d $DOMAIN"
    exit 1
fi

# Test SSL renewal
print_info "Testing SSL certificate renewal..."
certbot renew --dry-run >/dev/null 2>&1
if [ $? -eq 0 ]; then
    print_success "SSL auto-renewal is configured"
else
    print_warning "SSL auto-renewal test failed (but certificate is installed)"
fi

# Update firewall
print_info "Configuring firewall..."
if command -v ufw >/dev/null 2>&1; then
    ufw allow 80/tcp >/dev/null 2>&1
    ufw allow 443/tcp >/dev/null 2>&1
    print_success "Firewall configured for HTTP/HTTPS"
fi

print_header "HTTPS Setup Complete!"

cat <<EOF
Your monitoring platform is now secured with HTTPS!

Access URLs:
  ✅ https://$DOMAIN                (Grafana - Primary)
  ✅ https://$DOMAIN/prometheus/    (Prometheus - Optional)
  ✅ https://$DOMAIN/monitor-api/   (Monitor API - Optional)

SSL Certificate:
  Domain:     $DOMAIN
  Provider:   Let's Encrypt
  Expires:    90 days (auto-renews)
  Email:      $EMAIL

Next Steps:
  1. Update Grafana root URL:
     cd /opt/monitoring  # (or your install path)
     nano .env
     # Set: GRAFANA_ROOT_URL=https://$DOMAIN
     docker compose -f docker-compose.full.yml restart grafana

  2. Test HTTPS access:
     curl https://$DOMAIN/health

  3. Login to Grafana:
     https://$DOMAIN
     (admin / password from .env)

  4. Update node install commands to use domain:
     curl -fsSL https://raw.githubusercontent.com/.../install-alloy.sh | \\
       sudo bash -s -- \\
         -remote-write=https://$DOMAIN/prometheus/api/v1/write \\
         -install-token=YOUR_TOKEN

SSL Auto-Renewal:
  Certbot will automatically renew certificates before expiry.
  Renewal attempts happen twice daily.

Check Certificate Status:
  sudo certbot certificates

Force Renewal:
  sudo certbot renew --force-renewal

Nginx Commands:
  sudo systemctl status nginx
  sudo systemctl reload nginx
  sudo nginx -t  # Test configuration

Firewall Status:
  sudo ufw status

EOF

print_success "HTTPS configuration complete!"
