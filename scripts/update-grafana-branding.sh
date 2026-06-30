#!/bin/bash
# Grafana Branding Update Script
# Updates Grafana logos and organization name

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Configuration
GRAFANA_IMG_DIR="/usr/share/grafana/public/img"
BACKUP_DIR="${GRAFANA_IMG_DIR}/backup_$(date +%Y%m%d_%H%M%S)"
GRAFANA_URL="${GRAFANA_URL:-http://localhost:3000}"
GRAFANA_ADMIN_USER="${GRAFANA_ADMIN_USER:-admin}"
GRAFANA_ADMIN_PASS="${GRAFANA_ADMIN_PASS:-admin}"

# Functions
print_header() {
    echo -e "${GREEN}========================================${NC}"
    echo -e "${GREEN}  Grafana Branding Update Script${NC}"
    echo -e "${GREEN}========================================${NC}"
    echo ""
}

print_success() {
    echo -e "${GREEN}✓${NC} $1"
}

print_error() {
    echo -e "${RED}✗${NC} $1"
}

print_warning() {
    echo -e "${YELLOW}⚠${NC} $1"
}

check_root() {
    if [ "$EUID" -ne 0 ]; then
        print_error "This script must be run as root (use sudo)"
        exit 1
    fi
}

show_usage() {
    cat <<EOF
Usage: sudo $0 [OPTIONS]

Options:
    -d <directory>      Directory containing logo files
    -n <name>          Organization name (e.g., "Alloy Monitoring")
    -u <url>           Grafana URL (default: http://localhost:3000)
    -U <username>      Grafana admin username (default: admin)
    -P <password>      Grafana admin password (default: admin)
    -l                 List current configuration
    -r                 Restore original logos from backup
    -h                 Show this help

Required logo files (in directory specified with -d):
    logo_icon.svg           → grafana_icon.svg (sidebar icon)
    logo_full.svg           → grafana_typelogo.svg (login page)
    logo_text_dark.svg      → grafana_text_logo-dark.svg (header dark)
    logo_text_light.svg     → grafana_text_logo.svg (header light)

Examples:
    # Update logos from directory
    sudo $0 -d /path/to/logos

    # Update organization name only
    sudo $0 -n "Alloy Monitoring Platform"

    # Update both logos and name
    sudo $0 -d /path/to/logos -n "My Monitoring"

    # List current configuration
    sudo $0 -l

    # Restore original logos
    sudo $0 -r
EOF
}

list_current_config() {
    print_header
    echo "Current Grafana Configuration:"
    echo ""
    echo "Logo files location: $GRAFANA_IMG_DIR"
    echo ""
    echo "Current logo files:"
    ls -lh "${GRAFANA_IMG_DIR}"/grafana_*.svg 2>/dev/null | awk '{print "  " $9 " (" $5 ")"}'
    echo ""

    # Get current org name
    ORG_NAME=$(curl -s -u "${GRAFANA_ADMIN_USER}:${GRAFANA_ADMIN_PASS}" \
        "${GRAFANA_URL}/api/org" 2>/dev/null | jq -r '.name' 2>/dev/null || echo "unknown")
    echo "Organization name: $ORG_NAME"
    echo ""

    # List backups
    if ls "${GRAFANA_IMG_DIR}"/backup_* >/dev/null 2>&1; then
        echo "Available backups:"
        ls -dt "${GRAFANA_IMG_DIR}"/backup_* | head -5 | while read backup; do
            echo "  $(basename $backup)"
        done
    else
        print_warning "No backups found"
    fi
    echo ""
}

restore_logos() {
    print_header
    echo "Available backups:"
    echo ""

    backups=($(ls -dt "${GRAFANA_IMG_DIR}"/backup_* 2>/dev/null))

    if [ ${#backups[@]} -eq 0 ]; then
        print_error "No backups found"
        exit 1
    fi

    for i in "${!backups[@]}"; do
        echo "  $((i+1))) $(basename ${backups[$i]})"
    done
    echo ""

    read -p "Select backup to restore (1-${#backups[@]}) or 0 to cancel: " choice

    if [ "$choice" -eq 0 ]; then
        echo "Cancelled"
        exit 0
    fi

    if [ "$choice" -lt 1 ] || [ "$choice" -gt ${#backups[@]} ]; then
        print_error "Invalid choice"
        exit 1
    fi

    selected_backup="${backups[$((choice-1))]}"

    echo ""
    print_warning "This will restore logos from: $(basename $selected_backup)"
    read -p "Continue? (y/n): " confirm

    if [ "$confirm" != "y" ]; then
        echo "Cancelled"
        exit 0
    fi

    # Restore logos
    cp "${selected_backup}"/grafana_*.svg "$GRAFANA_IMG_DIR/"
    chown root:root "${GRAFANA_IMG_DIR}"/grafana_*.svg
    chmod 644 "${GRAFANA_IMG_DIR}"/grafana_*.svg

    print_success "Logos restored from backup"

    systemctl restart grafana-server
    print_success "Grafana restarted"

    echo ""
    print_warning "Clear your browser cache (Ctrl+Shift+R) to see changes"
}

update_logos() {
    local logo_dir="$1"

    if [ ! -d "$logo_dir" ]; then
        print_error "Directory not found: $logo_dir"
        exit 1
    fi

    # Check if required files exist
    required_files=("logo_icon.svg" "logo_full.svg")
    optional_files=("logo_text_dark.svg" "logo_text_light.svg")

    echo "Checking logo files..."
    echo ""

    missing_required=0
    for file in "${required_files[@]}"; do
        if [ -f "${logo_dir}/${file}" ]; then
            print_success "Found: $file"
        else
            print_error "Missing required file: $file"
            missing_required=1
        fi
    done

    for file in "${optional_files[@]}"; do
        if [ -f "${logo_dir}/${file}" ]; then
            print_success "Found: $file"
        else
            print_warning "Optional file not found: $file (will use default)"
        fi
    done

    echo ""

    if [ $missing_required -eq 1 ]; then
        print_error "Missing required logo files"
        echo ""
        echo "Required files:"
        echo "  - logo_icon.svg       (sidebar icon, ~32x32px)"
        echo "  - logo_full.svg       (login page, ~250x100px)"
        echo ""
        echo "Optional files:"
        echo "  - logo_text_dark.svg  (header dark theme, ~150x40px)"
        echo "  - logo_text_light.svg (header light theme, ~150x40px)"
        exit 1
    fi

    # Backup originals
    echo "Creating backup..."
    mkdir -p "$BACKUP_DIR"
    cp "${GRAFANA_IMG_DIR}"/grafana_*.svg "$BACKUP_DIR/" 2>/dev/null || true
    print_success "Backup created: $(basename $BACKUP_DIR)"
    echo ""

    # Replace logos
    echo "Replacing logos..."

    [ -f "${logo_dir}/logo_icon.svg" ] && {
        cp "${logo_dir}/logo_icon.svg" "${GRAFANA_IMG_DIR}/grafana_icon.svg"
        print_success "Updated: grafana_icon.svg"
    }

    [ -f "${logo_dir}/logo_full.svg" ] && {
        cp "${logo_dir}/logo_full.svg" "${GRAFANA_IMG_DIR}/grafana_typelogo.svg"
        print_success "Updated: grafana_typelogo.svg"
    }

    [ -f "${logo_dir}/logo_text_dark.svg" ] && {
        cp "${logo_dir}/logo_text_dark.svg" "${GRAFANA_IMG_DIR}/grafana_text_logo-dark.svg"
        print_success "Updated: grafana_text_logo-dark.svg"
    }

    [ -f "${logo_dir}/logo_text_light.svg" ] && {
        cp "${logo_dir}/logo_text_light.svg" "${GRAFANA_IMG_DIR}/grafana_text_logo.svg"
        print_success "Updated: grafana_text_logo.svg"
    }

    # Set permissions
    chown root:root "${GRAFANA_IMG_DIR}"/grafana_*.svg
    chmod 644 "${GRAFANA_IMG_DIR}"/grafana_*.svg

    echo ""
    print_success "Logo files updated successfully"
}

update_org_name() {
    local org_name="$1"

    echo "Updating organization name..."

    response=$(curl -s -w "\n%{http_code}" -X PUT \
        -u "${GRAFANA_ADMIN_USER}:${GRAFANA_ADMIN_PASS}" \
        "${GRAFANA_URL}/api/org" \
        -H "Content-Type: application/json" \
        -d "{\"name\":\"${org_name}\"}")

    http_code=$(echo "$response" | tail -n1)
    body=$(echo "$response" | sed '$d')

    if [ "$http_code" -eq 200 ]; then
        print_success "Organization name updated to: $org_name"
        return 0
    else
        print_error "Failed to update organization name (HTTP $http_code)"
        echo "$body"
        return 1
    fi
}

# Main script
main() {
    local logo_dir=""
    local org_name=""
    local list_config=0
    local restore_backup=0

    # Parse arguments
    while getopts "d:n:u:U:P:lrh" opt; do
        case $opt in
            d) logo_dir="$OPTARG" ;;
            n) org_name="$OPTARG" ;;
            u) GRAFANA_URL="$OPTARG" ;;
            U) GRAFANA_ADMIN_USER="$OPTARG" ;;
            P) GRAFANA_ADMIN_PASS="$OPTARG" ;;
            l) list_config=1 ;;
            r) restore_backup=1 ;;
            h) show_usage; exit 0 ;;
            \?) print_error "Invalid option: -$OPTARG"; show_usage; exit 1 ;;
        esac
    done

    # List configuration
    if [ $list_config -eq 1 ]; then
        list_current_config
        exit 0
    fi

    # Restore backup
    if [ $restore_backup -eq 1 ]; then
        check_root
        restore_logos
        exit 0
    fi

    # Check if at least one action is specified
    if [ -z "$logo_dir" ] && [ -z "$org_name" ]; then
        print_error "No action specified"
        echo ""
        show_usage
        exit 1
    fi

    check_root
    print_header

    # Update logos
    if [ -n "$logo_dir" ]; then
        update_logos "$logo_dir"
    fi

    # Update organization name
    if [ -n "$org_name" ]; then
        echo ""
        update_org_name "$org_name"
    fi

    # Restart Grafana
    echo ""
    echo "Restarting Grafana..."
    systemctl restart grafana-server
    sleep 2

    if systemctl is-active --quiet grafana-server; then
        print_success "Grafana restarted successfully"
    else
        print_error "Grafana failed to restart"
        systemctl status grafana-server
        exit 1
    fi

    # Final message
    echo ""
    echo -e "${GREEN}========================================${NC}"
    print_success "Branding update complete!"
    echo -e "${GREEN}========================================${NC}"
    echo ""
    print_warning "Clear your browser cache to see changes:"
    echo "  • Hard refresh: Ctrl + Shift + R (Linux/Windows)"
    echo "  • Hard refresh: Cmd + Shift + R (Mac)"
    echo "  • Or use incognito/private browsing mode"
    echo ""
}

# Run main function
main "$@"
