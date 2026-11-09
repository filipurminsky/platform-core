# SLOs, release gates, and error budgets

These are engineering objectives and configured thresholds, not measured service
availability or an external SLA. Local demos do not establish a 30-day SLO. The configured Prometheus retention
is six hours, so a 30-day query uses only the available shorter history.

## Implemented signals

| Scope | Signal / objective | Implemented behavior |
|---|---|---|
| echo-service | 99.5% request success over 30 days | `slo:http_request_success:ratio_rate30d` records non-5xx / all echo requests |
| echo-service | p95 ≤ 500 ms | Five-minute latency recording rule; canary uses a separate one-minute query |
| echo-service | Fast error-budget burn | `HighErrorRate`: error ratio > `14.4 * 0.005` in both 5m and 1h windows, held for 2m |
| vLLM | 99.0% success over 30 days | Error-budget rule counts `finished_reason="error"`; client aborts are excluded |
| vLLM | p90 ≤ 5 s | `VllmLatencyBreach` fires after the 5m latency estimate exceeds 5 s for 5m |
| worker-service | Consumer lag ≤ 100 | `KafkaConsumerLagHigh` fires above 100 for 5m |
| Worker/audio DLQs | No new dead letters | Alerts use increases in topic offsets, held for 1m; this is new arrival detection, not total retained depth |
| Audio pipeline | Observe end-to-end duration and failures | p50/p95 and failure-rate rules exist; there is no enforced end-to-end latency gate |

HTTP availability rules currently measure **echo-service**, not every HTTP
service or llm-gateway. Kafka Exporter is disabled in dev, so exporter-dependent
lag and DLQ alerts cannot be demonstrated there without explicitly enabling it.
Real vLLM/GPU signals are also absent from the default mock-based dev path.

Sources:
[recording rules](../observability/prometheus/slo/recording-rules.yaml),
[alerts](../observability/prometheus/rules/alerts.yaml), and
[canary analysis](../services/echo-service/k8s/base/analysistemplate.yaml).

## Progressive delivery gates

The echo canary checks its own ReplicaSet using the
`rollouts_pod_template_hash` series label, populated by the ServiceMonitor's
`podTargetLabels`. It checks:

- Canary request series and traffic must be present within the configured warm-up allowance.
- Non-5xx success rate must be **≥ 99%**.
- p95 latency must be **≤ 500 ms**.

Analysis samples run every 20 seconds, four times per metric, using one-minute
query windows. The short release success gate is deliberately documented
separately from the **99.5%** long-window availability objective. It is not a
30-day error-budget enforcement mechanism. A failed analysis aborts promotion;
verify stable routing and successful requests before declaring recovery.

## Example queries

```promql
sum(rate(echo_requests_total{status!~"5.."}[30d]))
/ sum(rate(echo_requests_total[30d]))
```

```promql
histogram_quantile(0.95,
  sum by (le) (rate(echo_request_duration_seconds_bucket[5m]))
)
```

Sparse traffic or absent series can make ratios undefined. Absence of a firing
alert is not proof of health; inspect traffic, scrape coverage, and rule loading.

## Error-budget policy

For a request-based objective, the error budget is the allowed fraction of bad
requests: `1 - objective`. It is not automatically an allowance of downtime
hours; that equivalence requires a time-based SLI or assumptions about traffic.

The following is a proposed operating policy for a team adopting the platform:

| Budget remaining | Proposed action |
|---|---|
| > 50% | Normal deployment cadence |
| 10–50% | Additional regression coverage and platform review |
| < 10% | Restrict releases to recovery work until reviewed |
| 0% | Incident review and corrective work |

These budget-dependent decisions are **not automatically wired into release
promotion**. The configured Argo CD sync window is a separate time-based
control. Adopting this policy would require defining ownership, calculating
per-service budgets, and implementing or recording the approval decision.
