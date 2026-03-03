"""Stage 3 — Voice Generator Agent.

Converts VideoScript text to audio with word-level timestamps using ElevenLabs.
ELEVENLABS_API_KEY environment variable is required.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
from pathlib import Path

from models.schemas import VideoScript, VoiceOutput, WordTimestamp
from utils.ffmpeg_helpers import get_audio_duration
from utils.srt_generator import estimate_word_timestamps, generate_ass, generate_srt

logger = logging.getLogger(__name__)


# ── ElevenLabs Provider ───────────────────────────────────────────────────────

def _reconstruct_words_from_chars(
    chars: list[str],
    start_times: list[float],
    end_times: list[float],
) -> list[WordTimestamp]:
    """Group character-level timing into word-level timing."""
    words: list[WordTimestamp] = []
    current_chars: list[str] = []
    current_start: float | None = None
    current_end: float = 0.0

    for char, start, end in zip(chars, start_times, end_times):
        if char in (" ", "\n", "\t"):
            if current_chars:
                words.append(
                    WordTimestamp(
                        word="".join(current_chars),
                        start=round(current_start, 3),
                        end=round(current_end, 3),
                    )
                )
                current_chars = []
                current_start = None
        else:
            if current_start is None:
                current_start = start
            current_chars.append(char)
            current_end = end

    # Flush last word
    if current_chars:
        words.append(
            WordTimestamp(
                word="".join(current_chars),
                start=round(current_start or 0.0, 3),
                end=round(current_end, 3),
            )
        )

    return words


def _generate_elevenlabs(
    script: VideoScript,
    audio_path: str,
    config,
) -> list[WordTimestamp] | None:
    """Generate audio with ElevenLabs and return word timestamps (None if timestamps unavailable)."""
    try:
        from elevenlabs import ElevenLabs, VoiceSettings
    except ImportError:
        raise RuntimeError(
            "elevenlabs package not installed. Run: pip install elevenlabs"
        )

    api_key = os.environ.get("ELEVENLABS_API_KEY")
    if not api_key:
        return None

    client = ElevenLabs(api_key=api_key)
    voice_id = getattr(config.voice, "elevenlabs_voice_id", "21m00Tcm4TlvDq8ikWAM")
    model_id = getattr(config.voice, "elevenlabs_model_id", "eleven_multilingual_v2")
    output_format = "mp3_44100_128"

    voice_settings = VoiceSettings(
        stability=getattr(config.voice, "elevenlabs_stability", 0.5),
        similarity_boost=getattr(config.voice, "elevenlabs_similarity_boost", 0.75),
        style=getattr(config.voice, "elevenlabs_style", 0.0),
        use_speaker_boost=getattr(config.voice, "elevenlabs_speaker_boost", True),
    )

    # Try convert_with_timestamps first
    try:
        logger.info("ElevenLabs: generating audio with character timestamps...")
        response = client.text_to_speech.convert_with_timestamps(
            voice_id=voice_id,
            text=script.full_text,
            model_id=model_id,
            output_format=output_format,
            voice_settings=voice_settings,
        )

        audio_chunks: list[bytes] = []
        all_chars: list[str] = []
        all_starts: list[float] = []
        all_ends: list[float] = []

        for chunk in response:
            if isinstance(chunk, dict):
                if chunk.get("audio_base64"):
                    audio_chunks.append(base64.b64decode(chunk["audio_base64"]))
                alignment = chunk.get("alignment") or {}
                chars = alignment.get("characters") or []
                starts = alignment.get("character_start_times_seconds") or []
                ends = alignment.get("character_end_times_seconds") or []
                all_chars.extend(chars)
                all_starts.extend(starts)
                all_ends.extend(ends)

        with open(audio_path, "wb") as f:
            for chunk in audio_chunks:
                f.write(chunk)

        if all_chars:
            words = _reconstruct_words_from_chars(all_chars, all_starts, all_ends)
            logger.info("ElevenLabs: got %d word timestamps via character alignment.", len(words))
            return words

    except Exception as exc:
        logger.warning("ElevenLabs convert_with_timestamps failed: %s", exc)

    # Fallback: generate audio without timestamps, use Whisper or estimate
    try:
        logger.info("ElevenLabs: falling back to standard convert (no timestamps)...")
        audio_gen = client.text_to_speech.convert(
            voice_id=voice_id,
            text=script.full_text,
            model_id=model_id,
            output_format=output_format,
            voice_settings=voice_settings,
        )
        with open(audio_path, "wb") as f:
            for chunk in audio_gen:
                f.write(chunk)
        logger.info("ElevenLabs: audio generated, attempting Whisper for timestamps...")
        return _whisper_timestamps(audio_path, script.full_text)
    except Exception as exc:
        logger.error("ElevenLabs standard convert failed: %s", exc)
        return None


def _whisper_timestamps(audio_path: str, text: str) -> list[WordTimestamp] | None:
    """Use OpenAI Whisper to get word timestamps from audio."""
    try:
        import openai

        openai_key = os.environ.get("OPENAI_API_KEY")
        if not openai_key:
            return None

        client = openai.OpenAI(api_key=openai_key)
        with open(audio_path, "rb") as f:
            transcript = client.audio.transcriptions.create(
                model="whisper-1",
                file=f,
                response_format="verbose_json",
                timestamp_granularities=["word"],
            )
        wts = getattr(transcript, "words", None) or []
        if not wts:
            return None
        result = []
        for w in wts:
            word = w.get("word") if isinstance(w, dict) else getattr(w, "word", "")
            start = w.get("start") if isinstance(w, dict) else getattr(w, "start", 0.0)
            end = w.get("end") if isinstance(w, dict) else getattr(w, "end", 0.0)
            result.append(WordTimestamp(word=word.strip(), start=start, end=end))
        logger.info("Whisper: got %d word timestamps.", len(result))
        return result
    except Exception as exc:
        logger.warning("Whisper timestamp extraction failed: %s", exc)
        return None


# ── edge-tts Provider ─────────────────────────────────────────────────────────

async def _generate_edge_tts(
    script: VideoScript,
    audio_path: str,
    config,
) -> list[WordTimestamp]:
    """Generate audio using edge-tts and return word timestamps."""
    import edge_tts

    voice = getattr(config.voice, "edge_tts_voice", "en-US-AndrewMultilingualNeural")
    rate = getattr(config.voice, "edge_tts_rate", "+0%")
    pitch = getattr(config.voice, "edge_tts_pitch", "+0Hz")

    for attempt_voice in [voice, "en-US-GuyNeural"]:
        try:
            logger.info("edge-tts: generating with voice '%s'...", attempt_voice)
            communicate = edge_tts.Communicate(
                script.full_text, attempt_voice, rate=rate, pitch=pitch
            )

            audio_bytes: list[bytes] = []
            word_events: list[dict] = []

            async for event in communicate.stream():
                if event["type"] == "audio":
                    audio_bytes.append(event["data"])
                elif event["type"] == "WordBoundary":
                    # offset is in 100-nanosecond units → divide by 10,000,000 for seconds
                    start_sec = event["offset"] / 10_000_000
                    duration_sec = event["duration"] / 10_000_000
                    word_events.append(
                        {
                            "word": event["text"],
                            "start": round(start_sec, 3),
                            "end": round(start_sec + duration_sec, 3),
                        }
                    )

            with open(audio_path, "wb") as f:
                for chunk in audio_bytes:
                    f.write(chunk)

            word_timestamps = [
                WordTimestamp(word=e["word"], start=e["start"], end=e["end"])
                for e in word_events
            ]
            logger.info("edge-tts: %d word timestamps.", len(word_timestamps))
            return word_timestamps

        except Exception as exc:
            logger.warning("edge-tts attempt with '%s' failed: %s", attempt_voice, exc)

    raise RuntimeError("edge-tts failed with all voices.")


# ── Section Times ─────────────────────────────────────────────────────────────

def _compute_section_times(
    word_timestamps: list[WordTimestamp],
    script: VideoScript,
) -> dict[str, tuple[float, float]]:
    """Map each script section to (start_seconds, end_seconds) in the audio."""
    if not word_timestamps:
        return {}

    section_times: dict[str, tuple[float, float]] = {}
    word_idx = 0

    for section in script.sections:
        section_words = section.text.split()
        n = len(section_words)
        if word_idx >= len(word_timestamps):
            break
        start = word_timestamps[word_idx].start
        end_idx = min(word_idx + n - 1, len(word_timestamps) - 1)
        end = word_timestamps[end_idx].end
        section_times[section.id] = (round(start, 3), round(end, 3))
        word_idx += n

    return section_times


# ── Validation ────────────────────────────────────────────────────────────────

def _validate_duration(duration: float) -> None:
    if duration > 65:
        raise RuntimeError(
            f"Audio is too long ({duration:.1f}s > 65s). Shorten the script and retry."
        )
    if duration > 58:
        logger.warning("Audio is %.1fs — close to 60s Shorts limit.", duration)
    if duration < 20:
        logger.warning("Audio is very short (%.1fs).", duration)


# ── Main Agent ────────────────────────────────────────────────────────────────

async def generate_voice(
    script: VideoScript,
    output_dir: str,
    config,
) -> VoiceOutput:
    """
    Generate TTS audio, word timestamps, section times, and subtitle files.
    Returns VoiceOutput.
    """
    audio_path = str(Path(output_dir) / "voiceover.mp3")
    srt_path = str(Path(output_dir) / "subtitles.srt")
    ass_path = str(Path(output_dir) / "subtitles.ass")

    # Require ElevenLabs API key
    if not os.environ.get("ELEVENLABS_API_KEY"):
        raise EnvironmentError(
            "ELEVENLABS_API_KEY is not set. ElevenLabs is required for audio generation.\n"
            "  export ELEVENLABS_API_KEY='your-key-here'\n"
            "Get your key at: https://elevenlabs.io"
        )

    words = _generate_elevenlabs(script, audio_path, config)
    provider = "elevenlabs"

    if words is None or not Path(audio_path).exists():
        raise RuntimeError(
            "ElevenLabs audio generation failed. "
            "Check your ELEVENLABS_API_KEY and voice ID in config.yaml."
        )

    # Get exact duration
    duration = get_audio_duration(audio_path)
    _validate_duration(duration)
    logger.info("Audio duration: %.2fs", duration)

    # If timestamps still missing, estimate them
    if not words:
        logger.warning("No word timestamps available — estimating uniformly.")
        words = estimate_word_timestamps(script.full_text.split(), duration)

    # Section times
    section_times = _compute_section_times(words, script)

    # Subtitle files
    words_per_chunk = getattr(config.subtitles, "words_per_chunk", 5)
    font_size = getattr(config.subtitles, "font_size", 48)
    margin_v = getattr(config.subtitles, "position_from_bottom", 200)
    bg_opacity = getattr(config.subtitles, "background_opacity", 0.5)

    generate_srt(words, srt_path, words_per_chunk=words_per_chunk)
    generate_ass(
        words,
        ass_path,
        words_per_chunk=words_per_chunk,
        font_size=font_size,
        margin_v=margin_v,
        bg_opacity=bg_opacity,
    )
    logger.info("Subtitle files written.")

    return VoiceOutput(
        audio_path=audio_path,
        duration_seconds=duration,
        word_timestamps=words,
        section_times=section_times,
        tts_provider=provider,
    )
