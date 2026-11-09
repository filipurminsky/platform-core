// k6 load profile for the echo-service canary demo.
//
// Two ways to use it:
//   1. Happy path — generate sustained, healthy traffic so the canary analysis
//      has data to measure and PASSES (rollout promotes 10→50→100%).
//   2. Failure injection — point FAIL_RATE > 0 (the app has no such flag, so in
//      practice you trigger failure by deploying a bad image); the canary's
//      success-rate query drops below 99% and Argo Rollouts auto-rolls-back.
//
// Run in-cluster:  kubectl apply -k tests/load   (targets the Envoy Gateway
//                  Service with Host: echo.platform-core.local)
// Run locally:     TARGET=http://localhost HOST=echo.platform-core.local k6 run tests/load/echo-load.js
//                  (kind maps host :80 to the Envoy Gateway proxy)

import http from "k6/http";
import { check, sleep } from "k6";

const BASE = __ENV.TARGET || "http://echo.platform-core.local";
// Optional Host override: the Gateway routes by hostname, so when TARGET is the
// gateway address (not the route's DNS name) send the route hostname explicitly.
const PARAMS = __ENV.HOST ? { headers: { Host: __ENV.HOST } } : {};

export const options = {
  scenarios: {
    ramp: {
      executor: "ramping-vus",
      startVUs: 0,
      stages: [
        { duration: "1m", target: 30 }, // ramp up
        { duration: __ENV.HOLD_DURATION || "5m", target: 30 }, // demo also covers restoration analysis
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
  const res = http.get(`${BASE}/`, PARAMS);
  check(res, { "status is 200": (r) => r.status === 200 });
  sleep(0.2);
}
