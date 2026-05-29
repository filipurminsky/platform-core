# SLO / SLA / Error Budget Definitions

## Principles

- **SLI** (Service Level Indicator) — the metric measured (e.g., request success rate)
- **SLO** (Service Level Objective) — the target for the SLI (e.g., 99.5% over 30 days)
- **Error budget** — the allowed failure budget = `1 - SLO`. At 99.5% availability, the budget is 0.5% of requests, or ~3.6 hours/month.
- **Burn rate** — how fast the budget is consumed vs. the 30-day window. A burn rate of 14× means the budget will be exhausted in `30 / 14 ≈ 2 days`.

---

## HTTP Services (echo-service, llm-gateway)

| SLI | Target | Window | Alert threshold |
|-----|--------|--------|-----------------|
| Availability (non-5xx / total) | 99.5% | 30 days | burn rate > 14× for 1 h |
| Latency p95 | ≤ 500 ms | 5 min rolling | p95 > 500 ms for 10 min |
| Error rate | < 0.5% | 5 min rolling | > 0.5% for 5 min |

**PromQL — availability:**
```promql
sum(rate(http_requests_total{status!~"5.."}[30d]))
/ sum(rate(http_requests_total[30d]))
```

**PromQL — latency p95:**
```promql
histogram_quantile(0.95,
  sum by (le) (rate(http_request_duration_seconds_bucket[5m]))
)
```

---

## vLLM Inference Service

| SLI | Target | Window | Alert threshold |
|-----|--------|--------|-----------------|
| Availability | 99.0% | 30 days | burn rate > 14× for 1 h |
| Latency p90 | ≤ 5 s | 5 min rolling | p90 > 5 s for 5 min |
| Error rate | < 1% | 5 min rolling | > 1% for 5 min |

Error budget (99.0% target): **7.2 hours / month** of acceptable downtime.

---

## Kafka / worker-service

| SLI | Target | Window | Alert threshold |
|-----|--------|--------|-----------------|
| Consumer lag | < 100 messages | sustained | > 100 for 5 min |
| DLQ messages | 0 | instantaneous | any message in DLQ |
| Processing success rate | > 99% | 5 min rolling | < 99% for 5 min |

---

## Error Budget Policy

| Budget remaining | Action |
|-----------------|--------|
| > 50% | Normal deployment cadence |
| 10–50% | Extra test coverage required on PRs; platform team review |
| < 10% | Deployment freeze; platform team approval required for any change |
| 0% | Incident declared; post-mortem required before freeze lifts |
