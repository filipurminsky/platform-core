// Exercise the gateway's pooled upstream connections around Uvicorn's 5s
// keepalive boundary. No scripted retries; any unexpected response fails the run.
// TARGET=http://localhost:18000 HOST=echo.platform-core.local k6 run tests/load/echo-idle.js
import http from "k6/http";
import { check, sleep } from "k6";

const base = __ENV.TARGET || "http://echo.platform-core.local";
const params = {
  headers: { Host: __ENV.HOST || "echo.platform-core.local" },
  timeout: "10s",
};

export const options = {
  vus: 40,
  duration: __ENV.DURATION || "5m",
  thresholds: {
    http_req_failed: ["rate==0"],
    checks: ["rate==1"],
  },
};

export default function () {
  // Stagger clients deterministically, then return near the backend's idle
  // close boundary. Vary burst size to leave a mix of pooled connection ages.
  if (__ITER === 0) sleep((__VU % 10) / 10);
  for (let i = 0; i < 1 + (__VU % 4); i++) {
    const response = http.get(`${base}/`, params);
    check(response, {
      "echo returns 200": (r) => r.status === 200,
      "response is from echo": (r) => {
        try {
          return r.json("service") === "echo-service";
        } catch (_) {
          return false;
        }
      },
    });
  }
  sleep(4.95 + ((__ITER + __VU) % 11) / 100);
}
