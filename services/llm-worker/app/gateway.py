"""LLM gateway client + defensive response parsing.

Always talks to the EXISTING llm-gateway (OpenAI-compatible
POST /v1/chat/completions) — NEVER vLLM directly (contract §6).
The assistant reply is parsed into (executive summary, action_items[]); if
parsing fails we fall back to the full text as summary with no action items,
so a low-quality model reply never crashes the worker.
"""

import time

import httpx

from app.config import LLM_MODEL, LLM_TIMEOUT
from app.metrics import LLM_REQUEST_DURATION

SYSTEM_PROMPT = (
    "You are an executive assistant. Given a meeting transcript, produce:\n"
    "1. SUMMARY: A concise executive summary (2-5 sentences).\n"
    "2. ACTION_ITEMS: A bullet list of concrete action items.\n\n"
    "Format your response exactly as:\n"
    "SUMMARY:\n<your summary here>\n\n"
    "ACTION_ITEMS:\n- <item 1>\n- <item 2>\n..."
)


def call_llm_gateway(
    http_client: httpx.Client,
    transcript: str,
) -> tuple[str, list[str], int]:
    """
    POST to LLM gateway and parse the response.

    Returns (summary, action_items, completion_tokens).
    Defensive: if parsing fails, returns (full_text, [], tokens).
    Raises httpx.HTTPStatusError / httpx.RequestError on gateway errors.
    """
    payload = {
        "model": LLM_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"Transcript:\n{transcript}"},
        ],
        "temperature": 0.3,
        "max_tokens": 512,
    }

    start = time.perf_counter()
    resp = http_client.post(
        "/v1/chat/completions",
        json=payload,
        timeout=LLM_TIMEOUT,
    )
    elapsed = time.perf_counter() - start
    LLM_REQUEST_DURATION.observe(elapsed)

    resp.raise_for_status()
    data = resp.json()

    # Extract completion_tokens for metric
    completion_tokens = 0
    try:
        completion_tokens = data.get("usage", {}).get("completion_tokens", 0) or 0
    except Exception:
        pass

    # Extract assistant message text
    try:
        text = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        text = str(data)

    # Defensive parse: extract SUMMARY and ACTION_ITEMS sections
    summary, action_items = _parse_llm_response(text)
    return summary, action_items, completion_tokens


def _parse_llm_response(text: str) -> tuple[str, list[str]]:
    """
    Parse the structured LLM response.

    Expects:
      SUMMARY:
      <summary text>

      ACTION_ITEMS:
      - <item>
      - <item>

    Falls back to (full_text, []) on any parse failure.
    """
    try:
        summary = ""
        action_items: list[str] = []

        lines = text.strip().splitlines()
        section = None
        summary_lines: list[str] = []
        item_lines: list[str] = []

        for line in lines:
            stripped = line.strip()
            if stripped.upper().startswith("SUMMARY:"):
                section = "summary"
                # inline content after the header
                rest = stripped[len("SUMMARY:") :].strip()
                if rest:
                    summary_lines.append(rest)
            elif stripped.upper().startswith("ACTION_ITEMS:"):
                section = "action_items"
                rest = stripped[len("ACTION_ITEMS:") :].strip()
                if rest.startswith("-"):
                    item_lines.append(rest.lstrip("- ").strip())
            else:
                if section == "summary":
                    summary_lines.append(stripped)
                elif section == "action_items":
                    if stripped.startswith("-"):
                        item_lines.append(stripped.lstrip("- ").strip())
                    elif stripped:
                        item_lines.append(stripped)

        summary = " ".join(s for s in summary_lines if s).strip()
        action_items = [i for i in item_lines if i]

        if not summary:
            # Nothing parseable — fall back
            return text.strip(), []

        return summary, action_items

    except Exception:
        # Defensive: never crash on parse failure
        return text.strip(), []
