"""Render paired configs, then correlate each measured request with Envoy logs."""

import json
import re
import sys
from pathlib import Path

root = Path(__file__).resolve().parent
evidence = Path(sys.argv[2])
if sys.argv[1] == "prepare":
    repository = root.parents[1]
    image = json.loads((evidence / "echo-image.json").read_text())
    commands = [
        line.removeprefix("CMD ")
        for line in (repository / "services/echo-service/Dockerfile").read_text().splitlines()
        if line.startswith("CMD ")
    ]
    assert len(commands) == 1 and image["cmd"] == json.loads(commands[0]), (
        "Echo image command differs from the current Dockerfile; rebuild with make connection-test"
    )
    command = image["cmd"]
    assert (
        "--timeout-keep-alive" in command
        and command[command.index("--timeout-keep-alive") + 1] == "5"
    ), (
        "This controlled FIN window requires the repository's explicit 5s keepalive; update the fixture when changing it"
    )
    policy = (repository / "services/echo-service/k8s/base/backendtrafficpolicy.yaml").read_text()
    matches = re.findall(r"^\s+connectionIdleTimeout: ([0-9]+)s\s*$", policy, re.MULTILINE)
    assert len(matches) == 1, (
        "Expected one explicit whole-second BackendTrafficPolicy connectionIdleTimeout"
    )
    candidate_seconds = int(matches[0])
    assert 0 < candidate_seconds < 5, (
        "Envoy pool must retire before the 5s backend close; fix BackendTrafficPolicy"
    )
    (evidence / "fixture.json").write_text(
        json.dumps(
            {"candidate_idle_seconds": candidate_seconds, "backend_keepalive_seconds": 5}, indent=2
        )
    )
    template = (root / "envoy.yaml.tmpl").read_text()
    for target, timeout in [("baseline", "3600s"), ("candidate", f"{candidate_seconds}s")]:
        (evidence / f"{target}.yaml").write_text(template.replace("@IDLE_TIMEOUT@", timeout))
elif sys.argv[1] == "verify":
    requests = json.loads((evidence / "requests.json").read_text())
    assert len(requests) == 6, "all three cycles per case must complete"
    for target, expected in [("baseline", 503), ("candidate", 200)]:
        records = []
        for line in (evidence / f"{target}.log").read_text().splitlines():
            try:
                records.append(json.loads(line[line.index("{") :]))
            except ValueError, json.JSONDecodeError:
                continue
        for cycle in range(3):
            cycle_id = f"reuse-{target}-{cycle}"
            matching = [record for record in records if record.get("cycle") == cycle_id]
            assert len(matching) == 1, (cycle_id, matching)
            record = matching[0]
            assert record["status"] == expected, record
            if target == "baseline":
                assert record["flags"] == "UC", record
                assert (
                    record["details"]
                    == "upstream_reset_before_response_started{connection_termination}"
                ), record
            else:
                assert record["flags"] == "-" and record["details"] == "via_upstream", record
    (evidence / "result.json").write_text(
        json.dumps(
            {
                "passed": True,
                "baseline_uc_503": 3,
                "candidate_echo_200": 3,
                "backend_keepalive_seconds": 5,
                "delayed_backend_fin_seconds": 0.3,
                "reuse_delay_seconds": 5.1,
                "envoy_idle_seconds": {
                    "baseline": 3600,
                    "candidate": json.loads((evidence / "fixture.json").read_text())[
                        "candidate_idle_seconds"
                    ],
                },
                "limits": [
                    "controlled delayed-FIN fault",
                    "not proof of historical error cause",
                    "single Envoy worker, HTTP/1.1, small local sample",
                    "no Kubernetes/Gateway API policy translation exercised",
                ],
            },
            indent=2,
        )
        + "\n"
    )
    print("PASS: three correlated baseline UC/503s and three candidate echo/200s")
else:
    raise SystemExit("Expected prepare or verify")
