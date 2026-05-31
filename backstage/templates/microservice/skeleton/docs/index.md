# ${{ values.name }}

${{ values.description }}

This service was scaffolded from the **New Microservice** template. It exposes a
FastAPI app with Prometheus metrics and health probes, ready to be deployed via
the platform's GitOps pipeline.

## Observability

The service exposes `/metrics`. Add a `ServiceMonitor` (or the
`prometheus.io/scrape` annotations) so Prometheus picks it up, then build a
Grafana dashboard for its golden signals.
