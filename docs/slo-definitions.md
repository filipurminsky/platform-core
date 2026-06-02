# SLO / SLA / Error Budget Definitions

This document defines the Service Level Objectives (SLOs) for the platform-core services. These targets drive our alerting, error budget policies, and progressive delivery gates.

## Unified SLO Table

| Service Tier | SLI | Target | Window | Alert / Gate Threshold |
|--------------|-----|--------|--------|------------------------|
| **Platform (HTTP)** | Availability | **99.5%** | 30 days | Burn rate > 14× for 1h |
| (echo, gateway) | Latency (p95) | **≤ 500 ms** | 5 min | > 500 ms for 10 min |
| | Error Rate | **< 0.5%** | 5 min | > 0.5% for 5 min |
| **vLLM Inference** | Availability | **99.0%** | 30 days | Burn rate > 14× for 1h |
| | Latency (p90) | **≤ 5 s** | 5 min | > 5 s for 5 min |
| | Error Rate | **< 1%** | 5 min | > 1% for 5 min |
| **Worker (Kafka)** | Processing | **> 99%** | 5 min | < 99% success for 5 min |
| | Consumer Lag | **< 100 msg** | N/A | Sustained > 100 for 5 min |
| | DLQ Volume | **0** | Instant | Any message in DLQ |

---

## Principles

- **SLI** (Service Level Indicator) — the metric measured (e.g., request success rate).
- **SLO** (Service Level Objective) — the target for the SLI (e.g., 99.5% over 30 days).
- **Error budget** — the allowed failure budget = `1 - SLO`. At 99.5% availability, the budget is 0.5% of requests, or ~3.6 hours/month.
- **Burn rate** — how fast the budget is consumed vs. the 30-day window. A burn rate of 14× means the budget will be exhausted in `30 / 14 ≈ 2 days`.

---

## Implementation Details

### PromQL — Availability (Platform)
```promql
sum(rate(http_requests_total{status!~"5.."}[30d]))
/ sum(rate(http_requests_total[30d]))
```

### PromQL — Latency p95 (Platform)
```promql
histogram_quantile(0.95,
  sum by (le) (rate(http_request_duration_seconds_bucket[5m]))
)
```

### PromQL — vLLM Latency p90
```promql
histogram_quantile(0.90,
  sum by (le) (rate(vllm:e2e_request_latency_seconds_bucket[5m]))
)
```

---

## Progressive Delivery Gate (Canary)

For services using Argo Rollouts (e.g., `echo-service`), the canary analysis matches the **Platform (HTTP)** SLOs exactly to ensure no release degrades the overall service health.

- **Gate:** Success Rate ≥ 99% AND **p95 Latency ≤ 500 ms**.
- **Window:** Analysis runs at each step of the rollout, measuring the canary ReplicaSet specifically.

---

## Error Budget Policy

| Budget remaining | Action |
|-----------------|--------|
| > 50% | Normal deployment cadence |
| 10–50% | Extra test coverage required on PRs; platform team review |
| < 10% | Deployment freeze; platform team approval required for any change |
| 0% | Incident declared; post-mortem required before freeze lifts |
