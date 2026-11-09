# ${{ values.name }}

${{ values.description }}

Use the repository README for setup, delivery and troubleshooting. Health probes
are `/healthz` and `/readyz`; the ServiceMonitor scrapes `/metrics`. In Grafana
Explore select Prometheus and query
`${{ values.name | replace('-', '_') }}_requests_total` or the
`${{ values.name | replace('-', '_') }}_request_duration_seconds_bucket` histogram.
