"""Pluggable TTS backends.

  stub   (default) — tiny valid WAV (0.1s 440 Hz tone), stdlib only; dev/CI.
  kokoro          — lazy-imports the Kokoro TTS library (CPU). kokoro/torch are
                    NOT in pyproject.toml; the prod CPU image bakes them via an
                    extra Dockerfile layer.

Each backend returns WAV bytes. The active backend is selected by
`synthesize()` in main.py from TTS_BACKEND (validated as a Literal at startup).
"""

import io
import struct
import threading
import wave

from platform_common.retry import PermanentError


def _synthesize_stub(text: str) -> bytes:
    """Generate a tiny valid WAV (0.1s of 440 Hz tone) using stdlib only.

    This is the wiring demo backend (TTS_BACKEND=stub). Used in dev and tests.
    No heavy deps required.
    """
    sample_rate = 16000
    duration_s = 0.1
    num_samples = int(sample_rate * duration_s)
    frequency = 440.0  # Hz

    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)  # 16-bit PCM
        wf.setframerate(sample_rate)
        # Build PCM samples: simple square wave at 440 Hz
        # struct.pack returns bytes per sample; join them into one bytes object.
        samples = b"".join(
            struct.pack(
                "<h",
                int(32767 * 0.3 * (1 if (i * frequency // sample_rate) % 2 == 0 else -1)),
            )
            for i in range(num_samples)
        )
        wf.writeframes(samples)
    return buf.getvalue()


_kokoro_pipelines: dict = {}  # lang_code -> KPipeline, built once per process
_kokoro_lock = threading.Lock()


def _get_kokoro_pipeline(lang_code: str):
    """Build (once) and cache the Kokoro pipeline — model load is the expensive part."""
    with _kokoro_lock:
        pipeline = _kokoro_pipelines.get(lang_code)
        if pipeline is None:
            try:
                import kokoro  # type: ignore[import-not-found]  # baked into the kokoro image
            except ImportError as exc:
                raise PermanentError(
                    "kokoro is not installed. The kokoro image bakes the model + voice "
                    "packs. Use TTS_BACKEND=stub for dev/test."
                ) from exc
            pipeline = kokoro.KPipeline(lang_code=lang_code)
            _kokoro_pipelines[lang_code] = pipeline
    return pipeline


def _synthesize_kokoro(text: str, *, lang_code: str = "a", voice: str = "af_heart") -> bytes:
    """Synthesize speech using the Kokoro TTS library (CPU).

    kokoro and torch are NOT in pyproject.toml so the test suite stays offline
    and the lockfile light; the import is lazy and the pipeline is cached at
    module level (like the nemo model in stt-worker).
    """
    pipeline = _get_kokoro_pipeline(lang_code)
    # Kokoro yields (graphemes, phonemes, audio) per sentence-ish segment.
    audio_chunks = [audio for _, _, audio in pipeline(text, voice=voice)]
    if not audio_chunks:
        raise PermanentError("kokoro produced no audio for the given text")

    import numpy as np  # type: ignore[import-not-found]  # part of kokoro's deps

    audio_np = np.concatenate([np.asarray(chunk) for chunk in audio_chunks])
    sample_rate = 24000  # Kokoro default

    buf = io.BytesIO()
    audio_int16 = (audio_np * 32767).clip(-32768, 32767).astype("int16")
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(audio_int16.tobytes())
    return buf.getvalue()
