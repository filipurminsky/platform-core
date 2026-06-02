// k6 load profile for the echo-service canary demo.
//
// Two ways to use it:
//   1. Happy path — generate sustained, healthy traffic so the canary analysis
//      has data to measure and PASSES (rollout promotes 10→50→100%).
//   2. Failure injection — point FAIL_RATE > 0 (the app has no such flag, so in
//      practice you trigger failure by deploying a bad image); the canary's
//      success-rate query drops below 99% and Argo Rollouts auto-rolls-back.
//
// Run in-cluster:  kubectl apply -k tests/load
// Run locally:     TARGET=http://echo.platform-core.local k6 run tests/load/echo-load.js

import http from "k6/http";
import { check, sleep } from "k6";

const BASE = __ENV.TARGET || "http://echo.platform-core.local";

export const options = {
  scenarios: {
    ramp: {
      executor: "ramping-vus",
      startVUs: 0,
      stages: [
        { duration: "1m", target: 30 }, // ramp up
        { duration: "5m", target: 30 }, // hold — spans the full canary analysis
        { duration: "1m", target: 0 }, // ramp down
      ],
    },
  },
  thresholds: {
    http_req_failed: ["rate<0.01"], // < 1% errors (mirrors the SLO)
    http_req_duration: ["p(95)<500"], // p95 < 500ms
  },
};

export default function () {
  const res = http.get(`${BASE}/`);
  check(res, { "status is 200": (r) => r.status === 200 });
  sleep(0.2);
}
