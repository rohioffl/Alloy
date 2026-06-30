# Docker Deployment Guide - Alloy Monitoring Platform

Complete guide to deploy the entire Alloy Monitoring stack using Docker Compose.

## 📦 What's Included

The full Docker stack includes:

- **Prometheus** - Metrics storage and time-series database
- **Grafana** - Visualization, dashboards, and alerting
- **Central Monitoring API** - FastAPI control plane for node management
- **Uptime Kuma** - External website monitoring
- **Auto-provisioning** - Datasources and dashboards configured automatically

## 🚀 Quick Start (5 minutes)

### Step 1: Prerequisites

Install Docker and Docker Compose V2:

```bash
# Ubuntu/Debian
curl -fsSL https://get.docker.com | sudo bash
sudo usermod -aG docker $USER
newgrp docker

# Verify installation
docker --version
docker compose version
```

### Step 2: Clone and Configure

```bash
cd /home/ubuntu/monitoring

# Create environment file from template
cp .env.example .env

# Generate secure tokens
echo "MONITOR_INSTALL_TOKEN=$(openssl rand -hex 32)" >> .env
echo "MONITOR_API_KEY=$(openssl rand -hex 32)" >> .env

# Edit .env with your settings
nano .env
```

**Required changes in `.env`**:
- `GRAFANA_ADMIN_PASS` - Change from default password
- `MONITOR_PUBLIC_URL` - Your server's public IP or domain
- `GRAFANA_ROOT_URL` - Your Grafana public URL
- `SMTP_*` - Email settings (if using alerts)

### Step 3: Deploy

```bash
# Deploy the full stack
./scripts/deploy-docker-stack.sh start

# Or manually:
docker compose -f docker-compose.full.yml up -d
```

### Step 4: Access Services

| Service | URL | Credentials |
|---------|-----|-------------|
| **Grafana** | http://localhost:3000 | admin / (from .env) |
| **Prometheus** | http://localhost:9090 | - |
| **Monitor API** | http://localhost:9099 | - |
| **Uptime Kuma** | http://localhost:3001 | (setup on first visit) |

### Step 5: Verify Deployment

```bash
# Check service health
./scripts/deploy-docker-stack.sh status

# View logs
./scripts/deploy-docker-stack.sh logs

# Test API
curl http://localhost:9099/health
```

## 📋 Complete Deployment Options

### Option 1: Using Deployment Script (Recommended)

```bash
# Start everything
./scripts/deploy-docker-stack.sh start

# Start in foreground (see logs in real-time)
./scripts/deploy-docker-stack.sh start --foreground

# Check status
./scripts/deploy-docker-stack.sh status

# View logs
./scripts/deploy-docker-stack.sh logs

# View specific service logs
./scripts/deploy-docker-stack.sh logs grafana

# Restart everything
./scripts/deploy-docker-stack.sh restart

# Stop services
./scripts/deploy-docker-stack.sh stop

# Complete cleanup (removes volumes)
./scripts/deploy-docker-stack.sh destroy
```

### Option 2: Direct Docker Compose

```bash
# Start all services
docker compose -f docker-compose.full.yml up -d

# Check status
docker compose -f docker-compose.full.yml ps

# View logs
docker compose -f docker-compose.full.yml logs -f

# Stop services
docker compose -f docker-compose.full.yml down

# Remove everything including volumes
docker compose -f docker-compose.full.yml down -v
```

## 🔧 Configuration Files

### Environment Variables (.env)

```bash
# Copy template
cp .env.example .env

# Key variables to configure:
GRAFANA_ADMIN_PASS=your-secure-password
MONITOR_PUBLIC_URL=http://your-server-ip:9099
MONITOR_INSTALL_TOKEN=generate-with-openssl-rand
MONITOR_API_KEY=generate-with-openssl-rand
SMTP_ENABLED=true
SMTP_HOST=email-smtp.region.amazonaws.com:587
SMTP_USER=your-ses-username
SMTP_PASSWORD=your-ses-password
```

### Prometheus Configuration

Edit `docker/prometheus/prometheus.yml` to customize:
- Scrape intervals
- Alert rules
- Remote write endpoints
- Additional scrape targets

### Grafana Provisioning

Datasources and dashboards are auto-provisioned from:
- `docker/grafana/provisioning/datasources/datasources.yml`
- `docker/grafana/provisioning/dashboards/dashboards.yml`
- `dashboards/*.json` (auto-loaded)

## 📊 Post-Deployment Setup

### 1. Configure Uptime Kuma

```bash
# Access Uptime Kuma
open http://localhost:3001

# Create admin account (first visit only)
# Add your external sites to monitor
```

### 2. Verify Grafana

```bash
# Access Grafana
open http://localhost:3000

# Login with admin credentials from .env
# Check that dashboards loaded:
# - Go to Dashboards → Browse
# - Should see "Monitoring" folder with all dashboards
```

### 3. Install Alloy Agents on Nodes

```bash
# On each node you want to monitor:
curl -fsSL https://raw.githubusercontent.com/rohioffl/Alloy/main/install-alloy.sh | \
  sudo bash -s -- \
    -remote-write=http://YOUR_SERVER_IP:9090/api/v1/write \
    -install-token=$(grep MONITOR_INSTALL_TOKEN .env | cut -d= -f2)
```

### 4. Configure Alert Email Recipients

```bash
# Via API
API_KEY=$(grep MONITOR_API_KEY .env | cut -d= -f2)

curl -X PUT http://localhost:9099/api/v1/clients/internal/alert-email \
  -H "X-Monitor-Key: $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"emails": ["alerts@yourcompany.com"], "enabled": true}'

# Or via Grafana UI:
# Grafana → All Servers → Clients & Accounts tab
```

## 🔍 Monitoring & Troubleshooting

### Health Checks

```bash
# Check all service health
docker ps --format "table {{.Names}}\t{{.Status}}"

# Individual service health
curl http://localhost:9099/health        # API
curl http://localhost:9090/-/healthy     # Prometheus
curl http://localhost:3000/api/health    # Grafana

# Check Prometheus targets
curl http://localhost:9090/api/v1/targets | jq '.data.activeTargets'
```

### View Logs

```bash
# All services
docker compose -f docker-compose.full.yml logs -f

# Specific service
docker compose -f docker-compose.full.yml logs -f monitor-api

# Last 100 lines
docker compose -f docker-compose.full.yml logs --tail=100 prometheus
```

### Container Shell Access

```bash
# Access API container
docker compose -f docker-compose.full.yml exec monitor-api /bin/bash

# Access Prometheus
docker compose -f docker-compose.full.yml exec prometheus /bin/sh

# Access Grafana
docker compose -f docker-compose.full.yml exec grafana /bin/bash
```

### Common Issues

#### Services Won't Start

```bash
# Check logs
docker compose -f docker-compose.full.yml logs

# Check if ports are in use
sudo netstat -tulpn | grep -E "3000|9090|9099|3001"

# Restart services
docker compose -f docker-compose.full.yml restart
```

#### Grafana Can't Connect to Prometheus

```bash
# Verify Prometheus is accessible from Grafana container
docker compose -f docker-compose.full.yml exec grafana curl http://prometheus:9090/api/v1/status/config

# Check network
docker network inspect monitoring-network
```

#### Dashboards Not Loading

```bash
# Check provisioning logs
docker compose -f docker-compose.full.yml logs grafana | grep provisioning

# Verify dashboard files are mounted
docker compose -f docker-compose.full.yml exec grafana ls -la /etc/grafana/dashboards
```

#### Permission Issues

```bash
# Fix Grafana data permissions
docker compose -f docker-compose.full.yml down
sudo chown -R 472:472 /var/lib/docker/volumes/grafana-data/_data
docker compose -f docker-compose.full.yml up -d

# Fix Prometheus data permissions
sudo chown -R 65534:65534 /var/lib/docker/volumes/prometheus-data/_data
```

## 💾 Data Management

### Backup Volumes

```bash
# Backup Prometheus data
docker run --rm \
  -v prometheus-data:/data \
  -v $(pwd):/backup \
  alpine tar czf /backup/prometheus-backup-$(date +%Y%m%d).tar.gz -C /data .

# Backup Grafana data
docker run --rm \
  -v grafana-data:/data \
  -v $(pwd):/backup \
  alpine tar czf /backup/grafana-backup-$(date +%Y%m%d).tar.gz -C /data .

# Backup Monitor API data
docker run --rm \
  -v monitor-data:/data \
  -v $(pwd):/backup \
  alpine tar czf /backup/monitor-backup-$(date +%Y%m%d).tar.gz -C /data .
```

### Restore Volumes

```bash
# Restore Prometheus data
docker volume create prometheus-data
docker run --rm \
  -v prometheus-data:/data \
  -v $(pwd):/backup \
  alpine sh -c "cd /data && tar xzf /backup/prometheus-backup-20260624.tar.gz"

# Repeat for other volumes
```

### List Volumes

```bash
# All monitoring volumes
docker volume ls | grep -E "prometheus|grafana|monitor|uptime"

# Volume details
docker volume inspect prometheus-data
```

## 🔄 Updates & Maintenance

### Update Services

```bash
# Pull latest images
docker compose -f docker-compose.full.yml pull

# Rebuild API (if code changed)
docker compose -f docker-compose.full.yml build monitor-api

# Restart with new images
docker compose -f docker-compose.full.yml up -d
```

### Update Single Service

```bash
# Update Grafana only
docker compose -f docker-compose.full.yml pull grafana
docker compose -f docker-compose.full.yml up -d grafana

# Rebuild and restart API
docker compose -f docker-compose.full.yml build monitor-api
docker compose -f docker-compose.full.yml up -d monitor-api
```

## 🌐 Production Deployment

### 1. Use Reverse Proxy (Nginx/Traefik/Caddy)

**Example Nginx configuration:**

```nginx
# /etc/nginx/sites-available/monitoring

upstream grafana {
    server localhost:3000;
}

upstream prometheus {
    server localhost:9090;
}

upstream monitor-api {
    server localhost:9099;
}

server {
    listen 80;
    server_name monitoring.yourdomain.com;

    location / {
        return 301 https://$host$request_uri;
    }
}

server {
    listen 443 ssl http2;
    server_name monitoring.yourdomain.com;

    ssl_certificate /etc/letsencrypt/live/monitoring.yourdomain.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/monitoring.yourdomain.com/privkey.pem;

    location / {
        proxy_pass http://grafana;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }

    location /prometheus/ {
        proxy_pass http://prometheus/;
    }

    location /api/ {
        proxy_pass http://monitor-api/api/;
    }
}
```

### 2. Enable HTTPS with Let's Encrypt

```bash
# Install certbot
sudo apt-get install certbot python3-certbot-nginx

# Get certificate
sudo certbot --nginx -d monitoring.yourdomain.com

# Auto-renewal is configured automatically
```

### 3. Resource Limits

Edit `docker-compose.full.yml` and add:

```yaml
services:
  prometheus:
    deploy:
      resources:
        limits:
          cpus: '2'
          memory: 4G
        reservations:
          cpus: '1'
          memory: 2G

  grafana:
    deploy:
      resources:
        limits:
          cpus: '1'
          memory: 2G
        reservations:
          memory: 512M
```

### 4. Security Hardening

```bash
# Change default passwords
nano .env  # Update GRAFANA_ADMIN_PASS

# Generate strong tokens
openssl rand -hex 32  # Update MONITOR_INSTALL_TOKEN
openssl rand -hex 32  # Update MONITOR_API_KEY

# Restrict port access (firewall)
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw deny 9090/tcp  # Prometheus (access via reverse proxy only)
sudo ufw deny 9099/tcp  # API (VPC only)
```

### 5. Enable External Database (PostgreSQL)

Uncomment the `postgres` service in `docker-compose.full.yml` and update `.env`:

```bash
GF_DATABASE_TYPE=postgres
GF_DATABASE_HOST=postgres:5432
GF_DATABASE_NAME=grafana
GF_DATABASE_USER=grafana
GF_DATABASE_PASSWORD=your-secure-password
POSTGRES_PASSWORD=your-secure-password
```

## 📈 Scaling

### Horizontal Scaling

For large deployments:

1. **External Prometheus** - Use Cortex/Mimir/Thanos
2. **External Storage** - Mount volumes to NFS/EBS
3. **Load Balancer** - Multiple Grafana replicas
4. **Separate Alert Manager** - Dedicated alerting service

### Vertical Scaling

Increase resources in docker-compose.yml:

```yaml
services:
  prometheus:
    deploy:
      resources:
        limits:
          cpus: '4'
          memory: 8G
```

## Additional resources

- [README.md](README.md) — architecture, node install, alerting, API overview
- [api/README.md](api/README.md) — Central Monitoring API layout and env vars
- [docs/GRAFANA_BRANDING_GUIDE.md](docs/GRAFANA_BRANDING_GUIDE.md) — Zentra logo and login branding

## 🆘 Support

### Check Configuration

```bash
# Validate compose file
docker compose -f docker-compose.full.yml config

# Check environment variables
docker compose -f docker-compose.full.yml config | grep -A 5 environment
```

### Debug Mode

```bash
# Enable debug logging for Grafana
echo "GF_LOG_LEVEL=debug" >> .env
docker compose -f docker-compose.full.yml restart grafana

# View debug logs
docker compose -f docker-compose.full.yml logs -f grafana
```

### Complete Reset

```bash
# Stop everything
docker compose -f docker-compose.full.yml down -v

# Remove all monitoring volumes
docker volume rm prometheus-data grafana-data monitor-data monitor-config uptime-kuma-data

# Start fresh
./scripts/deploy-docker-stack.sh start
```

---

**Ready to deploy?**

```bash
./scripts/deploy-docker-stack.sh start
```
