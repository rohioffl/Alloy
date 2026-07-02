#!/usr/bin/env bash
# Format and mount nvme1n1 (200G) for Zentra monitoring data.
set -euo pipefail

DISK="${MONITORING_DISK:-/dev/nvme1n1}"
MOUNT="${MONITORING_MOUNT:-/data/monitoring}"
LABEL="${MONITORING_LABEL:-zentra-monitoring}"

usage() {
  cat <<EOF
Usage: sudo $0 [--migrate]

Prepare ${DISK} for monitoring storage at ${MOUNT}.

  --migrate   Stop stack, copy existing Docker volume data, restart on new disk

Layout:
  ${MOUNT}/prometheus/
  ${MOUNT}/grafana/
  ${MOUNT}/monitor-data/
  ${MOUNT}/monitor-config/
  ${MOUNT}/uptime-kuma/
EOF
}

MIGRATE=false
while [[ $# -gt 0 ]]; do
  case "$1" in
    -h|--help) usage; exit 0 ;;
    --migrate) MIGRATE=true; shift ;;
    *) echo "Unknown option: $1" >&2; usage; exit 1 ;;
  esac
done

if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
  echo "Run as root: sudo $0 $*" >&2
  exit 1
fi

if [[ ! -b "$DISK" ]]; then
  echo "Block device not found: $DISK" >&2
  exit 1
fi

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE="${ROOT}/docker-compose.full.yml"

mount_disk() {
  if ! blkid "$DISK" >/dev/null 2>&1; then
    echo "Formatting $DISK as ext4 (label=${LABEL})..."
    mkfs.ext4 -F -L "$LABEL" "$DISK"
  else
    echo "$DISK already has a filesystem: $(blkid -s TYPE -o value "$DISK")"
  fi

  UUID=$(blkid -s UUID -o value "$DISK")
  mkdir -p "$MOUNT"
  if ! mountpoint -q "$MOUNT"; then
    mount "$DISK" "$MOUNT"
    echo "Mounted $DISK at $MOUNT"
  else
    echo "$MOUNT already mounted"
  fi

  FSTAB_LINE="UUID=${UUID}  ${MOUNT}  ext4  defaults,nofail  0  2"
  if ! grep -q "$MOUNT" /etc/fstab 2>/dev/null; then
    echo "$FSTAB_LINE" >> /etc/fstab
    echo "Added fstab entry"
  fi

  mkdir -p \
    "$MOUNT/prometheus" \
    "$MOUNT/grafana" \
    "$MOUNT/monitor-data" \
    "$MOUNT/monitor-config" \
    "$MOUNT/uptime-kuma"
  chown -R root:root "$MOUNT"
  chmod 755 "$MOUNT"
  # Prometheus container runs as root (user 0:0); Grafana as 472
  chown -R 472:472 "$MOUNT/grafana" 2>/dev/null || true
}

copy_volume() {
  local vol="$1" dest="$2"
  local src="/var/lib/docker/volumes/${vol}/_data"
  if [[ -d "$src" ]] && [[ -n "$(ls -A "$src" 2>/dev/null || true)" ]]; then
    echo "Copying ${vol} -> ${dest}..."
    rsync -a "$src/" "$dest/"
  else
    echo "Skipping empty/missing volume: ${vol}"
    mkdir -p "$dest"
  fi
}

migrate_data() {
  echo "Stopping monitoring stack..."
  docker compose -f "$COMPOSE" down || true

  copy_volume prometheus-data "$MOUNT/prometheus"
  copy_volume grafana-data "$MOUNT/grafana"
  copy_volume monitor-data "$MOUNT/monitor-data"
  copy_volume monitor-config "$MOUNT/monitor-config"
  copy_volume uptime-kuma-data "$MOUNT/uptime-kuma"

  chown -R 472:472 "$MOUNT/grafana" 2>/dev/null || true
  chown -R 1000:1000 "$MOUNT/uptime-kuma" 2>/dev/null || true

  echo "Starting stack on ${MOUNT}..."
  docker compose -f "$COMPOSE" up -d
}

mount_disk
if [[ "$MIGRATE" == true ]]; then
  migrate_data
fi

df -h "$MOUNT"
echo
echo "Monitoring data disk ready at ${MOUNT}"
du -sh "$MOUNT"/* 2>/dev/null || true
