"""Speech-to-text via Groq Whisper. Turn-based: one audio clip in, transcript out."""
import os

_client = None


def _client_lazy():
    global _client
    if _client is None:
        from groq import Groq
        _client = Groq(api_key=os.getenv("GROQ_API_KEY"))
    return _client


def transcribe(audio_bytes, filename="audio.webm", language=None):
    """
    audio_bytes: raw bytes of the recorded clip (webm/ogg/mp3/wav/m4a).
    language: optional ISO code ('en','zh','es') to hint Whisper; None = auto-detect.
    Returns the transcript string (may be '' for silence).
    """
    resp = _client_lazy().audio.transcriptions.create(
        file=(filename, audio_bytes),
        model=os.getenv("STT_MODEL", "whisper-large-v3"),
        language=language,
        temperature=0,
    )
    return (resp.text or "").strip()
