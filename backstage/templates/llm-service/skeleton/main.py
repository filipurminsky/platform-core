import os

import structlog
from fastapi import FastAPI, HTTPException
from openai import AsyncOpenAI

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
GATEWAY_URL = os.getenv("LLM_GATEWAY_URL", "http://llm-gateway.apps.svc/v1")
MODEL_NAME = os.getenv("LLM_MODEL", "${{ values.model }}")

log = structlog.get_logger()

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
app = FastAPI(title="${{ values.name }}", version="0.1.0")
API_KEY = os.environ["LLM_GATEWAY_API_KEY"]
if not API_KEY.strip():
    raise RuntimeError("LLM_GATEWAY_API_KEY must not be empty")
client = AsyncOpenAI(base_url=GATEWAY_URL, api_key=API_KEY)


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


@app.post("/chat")
async def chat(prompt: str):
    log.info("chat_request", model=MODEL_NAME)
    try:
        response = await client.chat.completions.create(
            model=MODEL_NAME,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=500,
        )
        return {"response": response.choices[0].message.content}
    except Exception as exc:
        log.error("chat_error", error=str(exc))
        raise HTTPException(status_code=500, detail="Inference failed") from exc
