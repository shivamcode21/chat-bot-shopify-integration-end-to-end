# Dramatiq webhook pipeline — Grafana alerts

Alert rules for the queue pipeline, routed to the existing Slack contact point
**`webhook-processing-errors`** (→ `#webhook-processing-errors`).

Source of truth: [`dramatiq_webhook_alerts.yaml`](./dramatiq_webhook_alerts.yaml)
(Grafana file-provisioning format, `apiVersion: 1`).

## The rules

| UID | Fires when | Severity |
|---|---|---|
| `dramatiq-queue-backlog` | `sum(dramatiq_queue_depth_messages) > 500` for 10m | warning |
| `dramatiq-task-failures` | `rate(dramatiq_task_failure_tasks_total) > 0` for 5m | critical |
| `dramatiq-enqueue-failing` | `rate(webhook_enqueue_failed_jobs_total) > 0` for 5m (broker unreachable → inline fallback) | critical |
| `dramatiq-broker-memory` | `render-redis` memory > 70% of limit for 10m (noeviction → enqueues fail) | warning |
| `dramatiq-worker-stalled` | enqueue rate > 0 but success rate ≈ 0 for 10m (worker down/stuck) | critical |

All use `noDataState: OK` — a metric that doesn't exist yet (e.g. no failures) is
healthy, not alerting.

## Apply

### Grafana Cloud (this stack) — provisioning API
File provisioning can't be dropped on Cloud disk, so use the companion script:

```bash
export GRAFANA_URL="https://<your-stack>.grafana.net"
export GRAFANA_TOKEN="<service-account token with Alerting write / Editor>"
pip install pyyaml
python apply_dramatiq_alerts.py --dry-run   # preview payloads
python apply_dramatiq_alerts.py             # create/update the rules
```
Create the token in Grafana → Administration → Service accounts. Re-running is
idempotent (upsert by rule UID). Rules land in folder **Dramatiq Webhook
Pipeline** and stay editable in the UI.

### Self-hosted Grafana — file provisioning
Copy `dramatiq_webhook_alerts.yaml` into Grafana's `provisioning/alerting/`
directory and restart (or `kill -HUP`). Confirm the contact point name matches.

## Notes
- Datasource UID is `grafanacloud-prom`; metric names are the live OTLP→Prom
  series (unit suffix included), verified in prod.
- To change the channel, edit each rule's `notification_settings.receiver`.
- Tune thresholds (`500`, `0.7`, …) in the YAML and re-apply.
