"""SRT and ASS subtitle file generators.

Takes word timestamps and groups them into timed subtitle chunks.
"""

from __future__ import annotations

from pathlib import Path

from models.schemas import WordTimestamp


def _format_srt_time(seconds: float) -> str:
    """Convert seconds to SRT timestamp: HH:MM:SS,mmm"""
    ms = int(seconds * 1000)
    h = ms // 3_600_000
    ms %= 3_600_000
    m = ms // 60_000
    ms %= 60_000
    s = ms // 1000
    ms %= 1000
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _format_ass_time(seconds: float) -> str:
    """Convert seconds to ASS timestamp: H:MM:SS.cc"""
    cs = int(seconds * 100)
    h = cs // 360_000
    cs %= 360_000
    m = cs // 6_000
    cs %= 6_000
    s = cs // 100
    cs %= 100
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def generate_srt(
    words: list[WordTimestamp],
    output_path: str,
    words_per_chunk: int = 5,
) -> None:
    """Write an SRT file grouping words into chunks."""
    chunks = _chunk_words(words, words_per_chunk)
    lines: list[str] = []
    for i, (text, start, end) in enumerate(chunks, start=1):
        lines.append(str(i))
        lines.append(f"{_format_srt_time(start)} --> {_format_srt_time(end)}")
        lines.append(text)
        lines.append("")
    Path(output_path).write_text("\n".join(lines), encoding="utf-8")


def generate_ass(
    words: list[WordTimestamp],
    output_path: str,
    words_per_chunk: int = 5,
    font_size: int = 48,
    margin_v: int = 200,
    font_color: str = "white",
    bg_opacity: float = 0.5,
) -> None:
    """Write an ASS subtitle file with styled subtitles."""
    # Convert color
    color_map = {"white": "&H00FFFFFF", "yellow": "&H0000FFFF"}
    primary_color = color_map.get(font_color, "&H00FFFFFF")
    # Background box: semi-transparent black
    back_alpha = int((1.0 - bg_opacity) * 255)
    back_color = f"&H{back_alpha:02X}000000"

    header = f"""\
[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Arial,{font_size},{primary_color},&H000000FF,&H00000000,{back_color},-1,0,0,0,100,100,0,0,3,2,0,2,30,30,{margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

    chunks = _chunk_words(words, words_per_chunk)
    events: list[str] = []
    for text, start, end in chunks:
        events.append(
            f"Dialogue: 0,{_format_ass_time(start)},{_format_ass_time(end)},Default,,0,0,0,,{text}"
        )

    Path(output_path).write_text(header + "\n".join(events) + "\n", encoding="utf-8")


def _chunk_words(
    words: list[WordTimestamp], words_per_chunk: int
) -> list[tuple[str, float, float]]:
    """Group words into (text, start_time, end_time) chunks."""
    if not words:
        return []
    chunks: list[tuple[str, float, float]] = []
    i = 0
    while i < len(words):
        group = words[i : i + words_per_chunk]
        text = " ".join(w.word for w in group)
        start = group[0].start
        end = group[-1].end
        chunks.append((text, start, end))
        i += words_per_chunk
    return chunks


def estimate_word_timestamps(
    words: list[str], total_duration: float
) -> list[WordTimestamp]:
    """Uniformly distribute word timestamps across total duration."""
    if not words:
        return []
    duration_per_word = total_duration / len(words)
    result: list[WordTimestamp] = []
    for i, word in enumerate(words):
        start = i * duration_per_word
        end = start + duration_per_word
        result.append(WordTimestamp(word=word, start=round(start, 3), end=round(end, 3)))
    return result
