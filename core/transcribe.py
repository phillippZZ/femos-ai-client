"""
transcribe.py — Audio capture and speech-to-text.

Two entry points:
  - Recorder      — captures from the local microphone (macOS / sounddevice)
  - transcribe_bytes(raw, sample_rate, dtype) — transcribes raw audio bytes
                    passed in from an external source such as Arduino over serial

Transcription backend priority:
  1. Local openai-whisper (if installed + torch available)
  2. Groq Whisper API     (requires GROQ_API_KEY in .env)
"""

import io
import threading
import numpy as np
import sounddevice as sd

from core.config import GROQ_API_KEY

SAMPLE_RATE = 16000
CHANNELS = 1

# ── Backend detection ─────────────────────────────────────────────────────────

def _has_local_whisper() -> bool:
    try:
        import whisper  # noqa: F401
        import torch    # noqa: F401
        return True
    except ImportError:
        return False

_USE_LOCAL = _has_local_whisper()
_whisper_model = None  # lazy-loaded

def _load_local_model():
    global _whisper_model
    if _whisper_model is None:
        import whisper
        _whisper_model = whisper.load_model("base")
    return _whisper_model


# ── Transcription core ────────────────────────────────────────────────────────

def _transcribe_array(audio: np.ndarray) -> str:
    """Transcribe a float32 numpy array at SAMPLE_RATE Hz."""
    if _USE_LOCAL:
        return _transcribe_local(audio)
    elif GROQ_API_KEY:
        return _transcribe_groq(audio)
    else:
        return "Error: No transcription backend available. Install whisper or set GROQ_API_KEY."


def _transcribe_local(audio: np.ndarray) -> str:
    model = _load_local_model()
    result = model.transcribe(audio, fp16=False)
    return result["text"].strip()


def _transcribe_groq(audio: np.ndarray) -> str:
    from groq import Groq
    import scipy.io.wavfile as wav

    client = Groq(api_key=GROQ_API_KEY)

    buf = io.BytesIO()
    pcm = (np.clip(audio, -1.0, 1.0) * 32767).astype(np.int16)
    wav.write(buf, SAMPLE_RATE, pcm)
    buf.seek(0)
    buf.name = "audio.wav"

    transcription = client.audio.transcriptions.create(
        file=buf,
        model="whisper-large-v3-turbo",
        response_format="text",
    )
    return transcription.strip() if isinstance(transcription, str) else transcription.text.strip()


# ── Public: external audio bytes (e.g. from Arduino) ─────────────────────────

def transcribe_bytes(raw: bytes, sample_rate: int = SAMPLE_RATE,
                     dtype: str = "int16") -> str:
    """
    Transcribe raw audio bytes received from an external source (e.g. Arduino).

    raw         — raw PCM bytes
    sample_rate — sample rate of the audio (default 16000)
    dtype       — numpy dtype string: 'int16' (default), 'float32', etc.
    """
    audio = np.frombuffer(raw, dtype=np.dtype(dtype))
    # Normalise to float32 in [-1, 1]
    if audio.dtype != np.float32:
        audio = audio.astype(np.float32) / np.iinfo(np.dtype(dtype)).max
    # Resample to SAMPLE_RATE if needed (simple linear interpolation)
    if sample_rate != SAMPLE_RATE:
        ratio = SAMPLE_RATE / sample_rate
        new_len = int(len(audio) * ratio)
        audio = np.interp(
            np.linspace(0, len(audio) - 1, new_len),
            np.arange(len(audio)),
            audio
        ).astype(np.float32)
    return _transcribe_array(audio)


# ── Public: local microphone recorder ────────────────────────────────────────

class Recorder:
    """Records from the default mic. Call start(), then stop() -> transcribed text."""

    def __init__(self):
        self._frames: list[np.ndarray] = []
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self):
        self._frames = []
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._record, daemon=True)
        self._thread.start()

    def stop(self) -> str:
        self._stop_event.set()
        if self._thread:
            self._thread.join()
        if not self._frames:
            return ""
        audio = np.concatenate(self._frames, axis=0).flatten()
        return _transcribe_array(audio)

    def _record(self):
        with sd.InputStream(samplerate=SAMPLE_RATE, channels=CHANNELS,
                            dtype="float32") as stream:
            while not self._stop_event.is_set():
                chunk, _ = stream.read(SAMPLE_RATE // 10)  # 100 ms chunks
                self._frames.append(chunk.copy())

