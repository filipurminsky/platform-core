"""Pluggable STT backends.

  stub  (default) — placeholder transcript, no ML deps; for dev/CI.
  nemo            — lazy-imports nemo_toolkit Parakeet (GPU). nemo/torch are
                    NOT in pyproject.toml; the prod GPU image bakes them via a
                    separate build stage. Never runs on CPU-only nodes.

Each backend returns (transcript_text, language, duration_s). The active
backend is selected by `transcribe()` in main.py from STT_BACKEND.
"""

import os


def _transcribe_stub(audio_bytes: bytes) -> tuple[str, str, float]:
    """
    Stub backend — returns a placeholder transcript.
    duration_s is estimated from byte size at 16 kHz / 16-bit mono (32 kB/s).
    Suitable for wiring demos on CPU-only clusters (dev / CI).
    """
    duration_s = max(0.1, len(audio_bytes) / 32_000)
    transcript = (
        f"[stub transcript — {len(audio_bytes)} bytes of audio, "
        f"estimated {duration_s:.1f}s @ 16kHz/16-bit mono]"
    )
    return transcript, "en", round(duration_s, 2)


def _transcribe_nemo(audio_bytes: bytes) -> tuple[str, str, float]:
    """
    NeMo / Parakeet backend — GPU only.

    IMPORTANT: nemo_toolkit and torch are NOT listed in pyproject.toml.
    They are lazy-imported here so the stub path (and tests) need no ML deps.

    The prod GPU image (Dockerfile build stage 'nemo') bakes:
      - nemo_toolkit[asr]==2.0.*
      - torch==2.2.*+cu121
      - the Parakeet RNNT-0.6B model weights at /model-cache/parakeet-rnnt-0.6b
    and sets STT_BACKEND=nemo.  This code path must NOT run on CPU-only nodes.
    """
    import tempfile

    import torch  # noqa: F401  # lazy import — fails fast if nemo not installed
    from nemo.collections.asr.models import EncDecRNNTBPEModel

    model_path = os.getenv("NEMO_MODEL_PATH", "/model-cache/parakeet-rnnt-0.6b")
    model = EncDecRNNTBPEModel.restore_from(model_path)
    model.eval()

    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        tmp.write(audio_bytes)
        tmp_path = tmp.name

    try:
        transcripts = model.transcribe([tmp_path])
        transcript_text = transcripts[0] if transcripts else ""
    finally:
        os.unlink(tmp_path)

    # NeMo Parakeet returns English transcripts; duration from audio length heuristic
    duration_s = max(0.1, len(audio_bytes) / 32_000)
    return transcript_text, "en", round(duration_s, 2)
