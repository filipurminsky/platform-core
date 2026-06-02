"""Pluggable TTS backends.

  stub   (default) — tiny valid WAV (0.1s 440 Hz tone), stdlib only; dev/CI.
  kokoro          — lazy-imports the Kokoro TTS library (CPU). kokoro/torch are
                    NOT in pyproject.toml; the prod CPU image bakes them via an
                    extra Dockerfile layer.

Each backend returns WAV bytes. The active backend is selected by
`synthesize()` in main.py from TTS_BACKEND.
"""

import io
import struct
import wave


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


def _synthesize_kokoro(text: str) -> bytes:
    """Synthesize speech using the Kokoro TTS library (CPU).

    The prod CPU image bakes Kokoro model + voice packs via an extra Dockerfile
    layer / build arg. kokoro and torch are NOT in pyproject.toml so the test
    suite stays offline and the lockfile stays light.

    Import is lazy so the stub path (dev/test) never touches this code path.
    """
    # lazy import — only when TTS_BACKEND=kokoro and the prod image is used
    try:
        import kokoro  # type: ignore[import-not-found]  # prod image bakes this
    except ImportError as exc:
        raise RuntimeError(
            "kokoro is not installed. The prod CPU image bakes Kokoro model + "
            "voice packs. Use TTS_BACKEND=stub for dev/test."
        ) from exc

    # Kokoro API: pipeline returns list of (graphemes, phonemes, audio_array)
    pipeline = kokoro.KPipeline(lang_code="en-us")
    audio_chunks = []
    for _, _, audio in pipeline(text, voice="af_heart"):
        audio_chunks.append(audio)

    import numpy as np  # type: ignore[import-not-found]  # part of kokoro's deps

    audio_np = np.concatenate(audio_chunks)
    sample_rate = 24000  # Kokoro default

    # Encode to WAV bytes
    buf = io.BytesIO()
    audio_int16 = (audio_np * 32767).clip(-32768, 32767).astype("int16")
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(audio_int16.tobytes())
    return buf.getvalue()
