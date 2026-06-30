#!/bin/bash
# Deploy Alloy Monitoring Platform via Docker Compose

set -e

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
COMPOSE_FILE="${PROJECT_DIR}/docker-compose.full.yml"
ENV_FILE="${PROJECT_DIR}/.env"
ENV_EXAMPLE="${PROJECT_DIR}/.env.example"

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

check_docker() {
    if ! command -v docker &> /dev/null; then
        print_error "Docker is not installed"
        echo "Install Docker: https://docs.docker.com/engine/install/"
        exit 1
    fi
    print_success "Docker found: $(docker --version)"
}

check_docker_compose() {
    if ! docker compose version &> /dev/null; then
        print_error "Docker Compose V2 is not available"
        echo "Install Docker Compose V2: https://docs.docker.com/compose/install/"
        exit 1
    fi
    print_success "Docker Compose found: $(docker compose version --short)"
}

check_env_file() {
    if [ ! -f "$ENV_FILE" ]; then
        print_warning ".env file not found"
        echo ""
        echo "Creating .env from template..."
        cp "$ENV_EXAMPLE" "$ENV_FILE"
        print_success ".env created from .env.example"
        echo ""
        print_warning "IMPORTANT: Edit .env and update these values:"
        echo "  - GRAFANA_ADMIN_PASS (change from default)"
        echo "  - MONITOR_INSTALL_TOKEN (generate with: openssl rand -hex 32)"
        echo "  - MONITOR_API_KEY (generate with: openssl rand -hex 32)"
        echo "  - MONITOR_PUBLIC_URL (your public IP/domain)"
        echo "  - SMTP settings (if using email alerts)"
        echo ""
        read -p "Press Enter after editing .env, or Ctrl+C to exit..."
    else
        print_success ".env file exists"

        # Check for default passwords
        if grep -q "GRAFANA_ADMIN_PASS=change-me" "$ENV_FILE" 2>/dev/null; then
            print_warning "GRAFANA_ADMIN_PASS is still set to default!"
        fi

        if grep -q "MONITOR_INSTALL_TOKEN=change-me" "$ENV_FILE" 2>/dev/null; then
            print_warning "MONITOR_INSTALL_TOKEN is still set to default!"
        fi
    fi
}

validate_config() {
    print_info "Validating Docker Compose configuration..."
    if docker compose -f "$COMPOSE_FILE" config > /dev/null 2>&1; then
        print_success "Configuration is valid"
    else
        print_error "Configuration validation failed"
        docker compose -f "$COMPOSE_FILE" config
        exit 1
    fi
}

pull_images() {
    print_info "Pulling Docker images..."
    docker compose -f "$COMPOSE_FILE" pull
    print_success "Images pulled"
}

build_api() {
    print_info "Building Central Monitoring API image..."
    docker compose -f "$COMPOSE_FILE" build monitor-api
    print_success "API image built"
}

start_stack() {
    local detached=$1

    print_info "Starting services..."
    if [ "$detached" = "true" ]; then
        docker compose -f "$COMPOSE_FILE" up -d
    else
        docker compose -f "$COMPOSE_FILE" up
    fi
}

show_status() {
    echo ""
    print_header "Service Status"
    docker compose -f "$COMPOSE_FILE" ps
    echo ""

    # Show health status
    print_info "Health Status:"
    docker ps --filter "name=prometheus" --filter "name=grafana" --filter "name=monitor-api" --filter "name=uptime-kuma" \
        --format "table {{.Names}}\t{{.Status}}" 2>/dev/null || true
    echo ""
}

wait_for_services() {
    print_info "Waiting for services to be healthy..."

    local max_attempts=30
    local attempt=0

    while [ $attempt -lt $max_attempts ]; do
        attempt=$((attempt + 1))

        # Check API health
        if curl -sf http://localhost:9099/health > /dev/null 2>&1; then
            print_success "Monitor API is healthy"
            break
        fi

        if [ $attempt -eq $max_attempts ]; then
            print_warning "Services may still be starting..."
            break
        fi

        echo -n "."
        sleep 2
    done
    echo ""
}

show_urls() {
    print_header "Access URLs"
    echo "Grafana:        http://localhost:3000"
    echo "Prometheus:     http://localhost:9090"
    echo "Monitor API:    http://localhost:9099"
    echo "Uptime Kuma:    http://localhost:3001"
    echo ""
    echo "Grafana login:  admin / (password from .env)"
    echo "API health:     curl http://localhost:9099/health"
    echo ""
}

show_next_steps() {
    print_header "Next Steps"
    echo "1. Access Grafana at http://localhost:3000"
    echo "   - Login with credentials from .env"
    echo "   - Dashboards will auto-load from /dashboards directory"
    echo ""
    echo "2. Setup Uptime Kuma at http://localhost:3001"
    echo "   - Create admin account on first visit"
    echo "   - Add your external sites to monitor"
    echo ""
    echo "3. Install Alloy agents on your nodes:"
    echo "   curl -fsSL https://raw.githubusercontent.com/rohioffl/Alloy/main/install-alloy.sh | \\"
    echo "     sudo bash -s -- \\"
    echo "       -remote-write=http://YOUR_SERVER_IP:9090/api/v1/write \\"
    echo "       -install-token=\$(grep MONITOR_INSTALL_TOKEN .env | cut -d= -f2)"
    echo ""
    echo "4. View logs:"
    echo "   docker compose -f docker-compose.full.yml logs -f"
    echo ""
    echo "5. Stop services:"
    echo "   docker compose -f docker-compose.full.yml down"
    echo ""
}

stop_stack() {
    print_info "Stopping services..."
    docker compose -f "$COMPOSE_FILE" down
    print_success "Services stopped"
}

destroy_stack() {
    print_warning "This will remove all containers, networks, and volumes!"
    read -p "Are you sure? (type 'yes' to confirm): " confirm

    if [ "$confirm" != "yes" ]; then
        echo "Cancelled"
        exit 0
    fi

    print_info "Destroying stack..."
    docker compose -f "$COMPOSE_FILE" down -v
    print_success "Stack destroyed"

    print_warning "All data has been removed!"
}

show_logs() {
    local service=$1
    if [ -z "$service" ]; then
        docker compose -f "$COMPOSE_FILE" logs -f
    else
        docker compose -f "$COMPOSE_FILE" logs -f "$service"
    fi
}

show_help() {
    cat <<EOF
Usage: $0 [COMMAND] [OPTIONS]

Commands:
    start           Start all services (default)
    stop            Stop all services
    restart         Restart all services
    status          Show service status
    logs [service]  Show logs (all or specific service)
    destroy         Remove everything (including volumes)
    validate        Validate configuration only
    help            Show this help

Options:
    -f, --foreground    Run in foreground (don't detach)

Examples:
    # Start stack
    $0 start

    # Start in foreground
    $0 start --foreground

    # View all logs
    $0 logs

    # View API logs
    $0 logs monitor-api

    # Check status
    $0 status

    # Stop everything
    $0 stop

    # Complete cleanup
    $0 destroy

Services:
    - prometheus      Metrics storage
    - grafana         Visualization & dashboards
    - monitor-api     Central control plane
    - uptime-kuma     External site monitoring
EOF
}

main() {
    local command="${1:-start}"
    local detached="true"

    # Parse flags
    shift || true
    while [ $# -gt 0 ]; do
        case "$1" in
            -f|--foreground)
                detached="false"
                ;;
            *)
                print_error "Unknown option: $1"
                show_help
                exit 1
                ;;
        esac
        shift
    done

    case "$command" in
        start)
            print_header "Deploying Alloy Monitoring Stack"
            check_docker
            check_docker_compose
            check_env_file
            validate_config
            echo ""
            pull_images
            echo ""
            build_api
            echo ""
            start_stack "$detached"

            if [ "$detached" = "true" ]; then
                wait_for_services
                show_status
                show_urls
                show_next_steps
            fi
            ;;
        stop)
            print_header "Stopping Services"
            stop_stack
            ;;
        restart)
            print_header "Restarting Services"
            stop_stack
            echo ""
            start_stack "true"
            wait_for_services
            show_status
            ;;
        status)
            show_status
            ;;
        logs)
            shift || true
            show_logs "$1"
            ;;
        destroy)
            print_header "Destroying Stack"
            destroy_stack
            ;;
        validate)
            print_header "Validating Configuration"
            check_docker
            check_docker_compose
            check_env_file
            validate_config
            print_success "All checks passed!"
            ;;
        help|--help|-h)
            show_help
            ;;
        *)
            print_error "Unknown command: $command"
            echo ""
            show_help
            exit 1
            ;;
    esac
}

main "$@"
