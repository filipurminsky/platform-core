"""${{ values.name }} — FastAPI microservice scaffolded by Backstage."""

import os

from fastapi import FastAPI, Request, Response
from prometheus_client import Counter, Histogram, generate_latest, CONTENT_TYPE_LATEST

SERVICE_NAME = "${{ values.name }}"

app = FastAPI(title=SERVICE_NAME, version="0.1.0")

REQUESTS = Counter(
    "${{ values.name | replace('-', '_') }}_requests_total",
    "Total HTTP requests",
    ["method", "path", "status"],
)
LATENCY = Histogram(
    "${{ values.name | replace('-', '_') }}_request_duration_seconds",
    "Request latency in seconds",
    ["path"],
)


@app.get("/")
async def root(request: Request):
    with LATENCY.labels(path="/").time():
        REQUESTS.labels(method=request.method, path="/", status="200").inc()
        return {"service": SERVICE_NAME, "message": "hello"}


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


@app.get("/readyz")
async def readyz():
    return {"status": "ready"}


@app.get("/metrics")
async def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
