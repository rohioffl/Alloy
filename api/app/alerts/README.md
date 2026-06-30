Custom Grafana alert rules can be added here as JSON files.

Each `.json` file must contain either one Grafana provisioning alert-rule
payload or a list of payloads. Use `__PROM_DS_UID__` for the Prometheus
datasource UID when needed; the sync endpoint replaces it before deployment.

Deploy bundled and custom alert rules with:

```bash
curl -X POST http://127.0.0.1:9099/api/v1/grafana/alerts/sync \
  -H "X-Monitor-Key: $API_KEY"
```
