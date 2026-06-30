# Grafana Branding (Zentra)

How to apply the Zentra logo, login theme, and org name when running Grafana in Docker.

## Source assets

| Path | Purpose |
|------|---------|
| `og-logo.png` | Master logo (transparent PNG) |
| `alert/logo.png` | Full logo served by the Monitor API |
| `alert/logo-email.png` | Compact logo for alert emails |
| `branding/` | Generated SVG/PNG assets and login CSS |

## Quick apply (running stack)

```bash
# Regenerate assets from og-logo.png, then patch the running Grafana container
./scripts/generate-branding-assets.sh
./scripts/apply-zentra-branding-docker.sh "Zentra"
```

`apply-zentra-branding-docker.sh` updates logos, favicons, login background, and login CSS inside the `grafana` container, then restarts Grafana.

## Bake branding into the image (recommended for new deploys)

```bash
./scripts/generate-branding-assets.sh
docker compose -f docker-compose.full.yml build grafana
docker compose -f docker-compose.full.yml up -d grafana
```

`Dockerfile.grafana` copies generated assets and `branding/zentra-login.css` / `branding/login-background-dark.svg` into the Grafana 13 image.

## One-command deploy with branding

```bash
./scripts/deploy-with-branding.sh "Zentra"
```

## Organization name

After deploy, set the org display name via API or UI:

```bash
curl -X PUT -u admin:$GRAFANA_ADMIN_PASS http://localhost:3000/api/org \
  -H "Content-Type: application/json" \
  -d '{"name":"Zentra"}'
```

Or use **Administration → General → Organization** in Grafana.

## Configuration files

| File | Role |
|------|------|
| `docker/grafana/grafana-zentra.ini` | App title, SMTP, `disable_sanitize_html` for Command Center iframe |
| `docker-compose.full.yml` | `GF_PANELS_DISABLE_SANITIZE_HTML`, SMTP env vars, volume mounts |
| `branding/zentra-login.css` | Glass login card, inputs, button, hidden footer |
| `branding/zentra-sidebar.css` | Hide selected mega-menu items |

## Sidebar menu

Grafana OSS does not support per-role sidebar hiding without Enterprise RBAC. Zentra uses two approaches:

1. **Config** (`docker/grafana/grafana-zentra.ini`) — disables Explore, Help, and News feed.
2. **CSS** (`branding/zentra-sidebar.css`) — hides Drilldown, Connections, Bookmarks, and **Migrate to Grafana Cloud**.

To hide more items, add selectors in `zentra-sidebar.css` using the nav path (e.g. `a[href="/alerting"]` to hide Alerting). Then run:

```bash
./scripts/apply-zentra-branding-docker.sh "Zentra"
```

Or rebuild the Grafana image. Hidden items are still reachable by direct URL; this only cleans up the sidebar.

## Production HTTPS

For a public domain (e.g. `zentra.ankercloud.com`):

1. Set `GRAFANA_ROOT_URL` and `MONITOR_PUBLIC_URL` in `.env`
2. Run `./scripts/setup-https-nginx.sh your.domain.com admin@example.com`
3. Proxy `/monitor-api/` to the Monitor API (port 9099)

See `DOCKER_DEPLOYMENT.md` for the full Docker stack guide.

## Legacy bare-metal Grafana

If Grafana is installed via `apt`/`systemd` (not Docker), use `scripts/update-grafana-branding.sh` or `scripts/apply-branding.sh` to replace files under `/usr/share/grafana/public/`. Docker is the supported path for this repo.
