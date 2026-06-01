# ${{ values.name }}

${{ values.description }}

Scaffolded from the **New Microservice** template in the platform-core Backstage portal.

## Endpoints

| Path       | Purpose                       |
|------------|-------------------------------|
| `/`        | Main handler                  |
| `/healthz` | Liveness probe                |
| `/readyz`  | Readiness probe               |
| `/metrics` | Prometheus scrape endpoint    |

## Local development

```bash
uv sync                              # creates .venv + uv.lock from pyproject.toml
uv run uvicorn app.main:app --reload
```

## Container

```bash
docker build -t ${{ values.name }}:dev .
docker run -p 8000:8000 ${{ values.name }}:dev
```
