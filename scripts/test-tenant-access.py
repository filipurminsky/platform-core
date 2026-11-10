#!/usr/bin/env python3
"""Opt-in dev-cluster access acceptance; creates/deletes only uniquely named probes.

Requires deployed tenant-alpha/bravo, authenticated gateway, and its ready dev
mock backend. Policies must already be reconciled from the current repository.
Never prints or persists the gateway key. No cluster selection implicit default.
"""

import base64
import json
import os
import signal
import subprocess
import uuid
from pathlib import Path

context = os.environ["KUBE_CONTEXT"]
evidence = Path(os.environ.get("EVIDENCE_DIR", f"/tmp/tenant-access-{uuid.uuid4().hex[:8]}"))
evidence.mkdir(parents=True, exist_ok=True)
name = "access-" + uuid.uuid4().hex[:10]
owned = []
checks = {}


def interrupted(signum, _frame):
    raise SystemExit(128 + signum)


signal.signal(signal.SIGTERM, interrupted)


def kubectl(*args, body=None):
    result = subprocess.run(
        ["kubectl", "--context", context, "--request-timeout=20s", *args],
        input=body,
        text=True,
        capture_output=True,
        timeout=100,
    )
    if result.returncode:
        # Inputs can contain ephemeral Secret data; never include them in errors.
        raise RuntimeError(f"kubectl {args[0]} failed: {result.stderr.strip()}")
    return result.stdout


def get(kind, resource, ns=None):
    args = ["get", kind, resource, "-o", "json"]
    if ns:
        args += ["-n", ns]
    return json.loads(kubectl(*args))


def create(obj):
    result = json.loads(kubectl("create", "-f", "-", "-o", "json", body=json.dumps(obj)))
    owned.append(
        (
            result["kind"],
            result["metadata"]["namespace"],
            result["metadata"]["name"],
            result["metadata"]["uid"],
        )
    )


def resource(kind, ns, suffix="", **spec):
    return {
        "apiVersion": "v1",
        "kind": kind,
        "metadata": {"name": name + suffix, "namespace": ns},
        **spec,
    }


def pod(ns, suffix, port=8080, app=None, credential=False):
    obj = resource("Pod", ns, suffix)
    obj["metadata"]["labels"] = {"app": app or name}
    container = {
        "name": "probe",
        "image": image,
        "imagePullPolicy": "IfNotPresent",
        "command": ["python", "-m", "http.server", str(port)],
        "resources": {
            "requests": {"cpu": "10m", "memory": "32Mi"},
            "limits": {"cpu": "100m", "memory": "64Mi"},
        },
        "securityContext": {
            "allowPrivilegeEscalation": False,
            "readOnlyRootFilesystem": True,
            "capabilities": {"drop": ["ALL"]},
        },
        "readinessProbe": {"httpGet": {"path": "/", "port": port}, "periodSeconds": 2},
        "livenessProbe": {"httpGet": {"path": "/", "port": port}, "periodSeconds": 10},
    }
    obj["spec"] = {
        "serviceAccountName": name,
        "automountServiceAccountToken": False,
        "securityContext": {
            "runAsNonRoot": True,
            "runAsUser": 10001,
            "seccompProfile": {"type": "RuntimeDefault"},
        },
        "containers": [container],
        "terminationGracePeriodSeconds": 1,
    }
    if credential:
        container["volumeMounts"] = [
            {"name": "credential", "mountPath": "/credential", "readOnly": True}
        ]
        obj["spec"]["volumes"] = [{"name": "credential", "secret": {"secretName": name}}]
    create(obj)
    kubectl("-n", ns, "wait", "--for=condition=Ready", "pod/" + name + suffix, "--timeout=80s")
    return get("pod", name + suffix, ns)["status"]["podIP"]


def execute(ns, podname, code):
    return json.loads(kubectl("-n", ns, "exec", "-i", podname, "--", "python", "-", body=code))


try:
    assert (
        get("secret", "in-cluster", "argocd")["metadata"]["labels"].get("environment") == "dev"
    ), "dev cluster required"
    for ns in ("tenant-alpha", "tenant-bravo"):
        assert get("namespace", ns)["metadata"]["labels"].get("platform.io/tenant"), (
            f"{ns} is not platform-enrolled"
        )
    deployment = get("deployment", "llm-gateway", "apps")
    assert deployment.get("status", {}).get("readyReplicas", 0) > 0
    image = deployment["spec"]["template"]["spec"]["containers"][0]["image"]
    key = get("secret", "llm-gateway-api-keys", "apps")["data"]["llm-worker"]
    assert base64.b64decode(key), "gateway key must be nonempty"
    for ns in ("tenant-alpha", "tenant-bravo", "apps"):
        create(resource("ServiceAccount", ns, automountServiceAccountToken=False))
    create(resource("Secret", "tenant-alpha", type="Opaque", data={"token": key}))
    del key
    # Same app label in another namespace must NOT pass vLLM's AND selector.
    pod("tenant-alpha", "-client", app="llm-gateway", credential=True)
    same = pod("tenant-alpha", "-peer")
    other = pod("tenant-bravo", "-peer")
    checks["reverse_cross_tenant_denied"] = execute(
        "tenant-bravo",
        name + "-peer",
        f"import socket,json\ntry:\n s=socket.create_connection(({same!r},8080),3);s.close();denied=False\nexcept TimeoutError:\n denied=True\nprint(json.dumps(denied))",
    )
    inference = pod("apps", "-inference", port=8000, app="vllm-inference")
    gateway_pods = json.loads(
        kubectl("-n", "apps", "get", "pods", "-l", "app=llm-gateway", "-o", "json")
    )["items"]
    gateway_pod = next(
        p["metadata"]["name"] for p in gateway_pods if p["status"].get("phase") == "Running"
    )
    # A positive control proves the protected inference endpoint is alive.
    checks["gateway_to_inference"] = execute(
        "apps",
        gateway_pod,
        f"import socket,json\ns=socket.create_connection(({inference!r},8000),3);s.close();print(json.dumps(True))",
    )
    code = """import json,socket,urllib.request,urllib.error
from pathlib import Path
checks={}
def reachable(ip,port):
 try:
  s=socket.create_connection((ip,port),3);s.close();return True
 except TimeoutError:return False
 except OSError as e:raise AssertionError(f"unexpected connectivity error: {e}")
def request(token):
 req=urllib.request.Request('http://llm-gateway.apps.svc.cluster.local/v1/models',headers={'Authorization':'Bearer '+token} if token else {})
 try:
  with urllib.request.urlopen(req,timeout=15) as r:return r.status
 except urllib.error.HTTPError as e:return e.code
checks['same_tenant_allowed']=reachable(SAME,8080)
checks['cross_tenant_denied']=not reachable(OTHER,8080)
checks['direct_inference_denied']=not reachable(INFERENCE,8000)
checks['missing_key_401']=request('')==401
checks['wrong_key_401']=request('invalid-acceptance-key')==401
checks['authenticated_gateway_200']=request(Path('/credential/token').read_text())==200
print(json.dumps(checks))
"""
    code = f"SAME={same!r}\nOTHER={other!r}\nINFERENCE={inference!r}\n" + code
    checks.update(execute("tenant-alpha", name + "-client", code))
    assert all(checks.values()), checks
finally:
    cleanup_errors = []
    for kind, ns, resource_name, uid in reversed(owned):
        try:
            current = get(kind, resource_name, ns)
            assert current["metadata"]["uid"] == uid, "resource ownership changed"
            kubectl("-n", ns, "delete", kind, resource_name, "--wait=true", "--timeout=30s")
        except Exception as exc:
            cleanup_errors.append(str(exc))
    result = {
        "context": context,
        "checks": checks,
        "passed": bool(checks) and len(checks) == 8 and all(checks.values()) and not cleanup_errors,
        "cleanup_errors": cleanup_errors,
    }
    (evidence / "tenant-access.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))
    if cleanup_errors:
        raise RuntimeError("probe cleanup failed; inspect evidence")
