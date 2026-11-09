"""Pluggable STT backends.

  stub  (default) — placeholder transcript, no ML deps; for dev/CI.
  nemo            — lazy-imports NeMo Parakeet (GPU). nemo/torch/numpy are NOT
                    in pyproject.toml; the GPU image bakes them (see the
                    Dockerfile `nemo` stage). Never runs on CPU-only nodes.

Each backend returns (transcript_text, language, duration_s).

audio-api accepts wav/mp3/ogg/flac/m4a, so the nemo backend first decodes ANY
input with ffmpeg to 16 kHz mono 16-bit PCM (what Parakeet expects) and derives
the duration from the decoded sample count — not from the compressed size.
"""

from __future__ import annotations

import io
import subprocess
import threading
import wave

from platform_common.retry import PermanentError

TARGET_SAMPLE_RATE = 16_000

_nemo_model = None  # loaded once on first call, reused across messages
_nemo_lock = threading.Lock()


def wav_duration_seconds(audio_bytes: bytes) -> float | None:
    """Exact duration from a WAV header, or None if the bytes are not a readable WAV."""
    if audio_bytes[:4] != b"RIFF" or audio_bytes[8:12] != b"WAVE":
        return None
    try:
        with wave.open(io.BytesIO(audio_bytes), "rb") as wf:
            rate = wf.getframerate()
            return wf.getnframes() / rate if rate else None
    except wave.Error, EOFError:
        return None


def _transcribe_stub(audio_bytes: bytes) -> tuple[str, str, float]:
    """Stub backend — placeholder transcript.

    Duration is exact for WAV input (from the header); for compressed formats it
    is a rough estimate assuming 16 kHz/16-bit mono PCM (32 kB/s).
    """
    exact = wav_duration_seconds(audio_bytes)
    duration_s = max(0.1, exact if exact is not None else len(audio_bytes) / 32_000)
    basis = "from WAV header" if exact is not None else "estimated @ 16kHz/16-bit mono"
    transcript = f"[stub transcript — {len(audio_bytes)} bytes of audio, {duration_s:.1f}s {basis}]"
    return transcript, "en", round(duration_s, 2)


def decode_to_pcm16k(audio_bytes: bytes, ffmpeg: str = "ffmpeg", timeout: float = 120.0) -> bytes:
    """Decode any ffmpeg-supported container/codec to raw 16 kHz mono s16le PCM.

    Undecodable input is a PermanentError (→ DLQ without retries).
    """
    try:
        proc = subprocess.run(
            [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error",
             "-i", "pipe:0", "-ac", "1", "-ar", str(TARGET_SAMPLE_RATE),
             "-f", "s16le", "-acodec", "pcm_s16le", "pipe:1"],
            input=audio_bytes,
            capture_output=True,
            timeout=timeout,
            check=False,
        )  # fmt: skip
    except FileNotFoundError as exc:
        raise PermanentError(f"ffmpeg not found ({ffmpeg!r}); the nemo image must ship it") from exc
    if proc.returncode != 0 or not proc.stdout:
        err = proc.stderr.decode(errors="replace").strip()[-500:]
        raise PermanentError(f"could not decode audio: {err or 'no audio stream'}")
    return proc.stdout


def _hypothesis_text(result) -> str:
    """Normalise NeMo transcribe() output across versions.

    NeMo 2.x returns a list of Hypothesis objects (use `.text`); older RNNT
    models returned a (best_hypotheses, all_hypotheses) tuple of strings.
    """
    if isinstance(result, tuple):
        result = result[0]
    if not result:
        return ""
    first = result[0]
    if isinstance(first, list):  # n-best list
        first = first[0] if first else ""
    return str(getattr(first, "text", first) or "")


def _load_nemo(model_path: str):
    global _nemo_model
    with _nemo_lock:
        if _nemo_model is None:
            from nemo.collections.asr.models import ASRModel  # lazy: GPU image only

            _nemo_model = ASRModel.restore_from(model_path)
            _nemo_model.eval()
    return _nemo_model


def _transcribe_nemo(
    audio_bytes: bytes, *, model_path: str, ffmpeg: str = "ffmpeg"
) -> tuple[str, str, float]:
    """NeMo / Parakeet backend — GPU only (lazy-imported; see module docstring)."""
    import numpy as np  # lazy — bundled with the NeMo image

    pcm = decode_to_pcm16k(audio_bytes, ffmpeg=ffmpeg)
    samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
    duration_s = len(samples) / TARGET_SAMPLE_RATE
    if duration_s < 0.05:
        raise PermanentError("decoded audio is empty")

    model = _load_nemo(model_path)
    transcript_text = _hypothesis_text(model.transcribe([samples], batch_size=1, verbose=False))
    # Parakeet RNNT 0.6B is English-only.
    return transcript_text, "en", round(duration_s, 2)
