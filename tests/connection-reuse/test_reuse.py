"""Black-box HTTP client: real echo behind Envoy and a delayed upstream FIN."""

import http.client
import json
import time
from pathlib import Path


def request(connection, cycle):
    connection.request("GET", "/", headers={"X-Test-Cycle": cycle})
    response = connection.getresponse()
    body = response.read().decode()
    return response.status, body


for target in ("baseline", "candidate"):
    deadline = time.monotonic() + 30
    while True:
        connection = http.client.HTTPConnection(target, 8080, timeout=3)
        try:
            status, body = request(connection, "readiness")
            if status == 200:
                break
        except OSError, http.client.HTTPException:
            pass
        finally:
            connection.close()
        assert time.monotonic() < deadline, f"Envoy/backend not ready: {target}"
        time.sleep(0.2)

results = []
for target, expected in [("baseline", 503), ("candidate", 200)]:
    for cycle in range(3):
        connection = http.client.HTTPConnection(target, 8080, timeout=3)
        try:
            status, body = request(connection, f"warm-{target}-{cycle}")
            assert status == 200 and json.loads(body)["service"] == "echo-service"
            started = time.monotonic()
            # Backend closes at 5s. Proxy delays its FIN by 0.3s; 5.1s lands
            # inside the controlled stale-connection window. No client retries.
            time.sleep(5.1)
            cycle_id = f"reuse-{target}-{cycle}"
            status, body = request(connection, cycle_id)
            result = {
                "cycle": cycle_id,
                "status": status,
                "elapsed_seconds": round(time.monotonic() - started, 3),
                "echo_payload": status == 200 and json.loads(body).get("service") == "echo-service",
            }
            results.append(result)
            Path("/evidence/requests.json").write_text(json.dumps(results, indent=2))
            print(json.dumps(result), flush=True)
            assert status == expected, result
            if expected == 200:
                assert result["echo_payload"], "200 from an unrelated endpoint is not success"
        finally:
            connection.close()
        time.sleep(0.4)
