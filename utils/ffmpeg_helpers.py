"""FFmpeg / ffprobe command builders.

All functions use subprocess.run(check=True) and capture stderr.
FFmpeg and ffprobe must be installed and on PATH.
"""

from __future__ import annotations

import logging
import subprocess
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)


def _run(cmd: list[str], description: str = "") -> subprocess.CompletedProcess:
    logger.debug("FFmpeg: %s", " ".join(cmd))
    try:
        result = subprocess.run(
            cmd,
            check=True,
            capture_output=True,
            text=True,
        )
        return result
    except subprocess.CalledProcessError as exc:
        logger.error("FFmpeg error (%s):\n%s", description, exc.stderr)
        raise


def get_audio_duration(audio_path: str) -> float:
    """Return audio duration in seconds via ffprobe."""
    result = _run(
        [
            "ffprobe",
            "-v", "quiet",
            "-show_entries", "format=duration",
            "-of", "csv=p=0",
            audio_path,
        ],
        "get_audio_duration",
    )
    return float(result.stdout.strip())


def scale_image_for_zoompan(image_path: str, output_path: str, zoom_end: float = 1.15) -> None:
    """Scale image to 1.15× output size (1242×2208) required by zoompan filter."""
    scale_w = int(1080 * zoom_end + 2)  # slight extra to avoid edge artefacts
    scale_h = int(1920 * zoom_end + 2)
    # Make even numbers (required by libx264)
    scale_w += scale_w % 2
    scale_h += scale_h % 2
    _run(
        [
            "ffmpeg", "-y",
            "-i", image_path,
            "-vf", f"scale={scale_w}:{scale_h}:force_original_aspect_ratio=increase,"
                   f"crop={scale_w}:{scale_h}",
            output_path,
        ],
        "scale_image_for_zoompan",
    )


def create_zoompan_clip(
    image_path: str,
    output_path: str,
    duration: float,
    fps: int = 30,
    zoom_end: float = 1.15,
    size: str = "1080x1920",
) -> None:
    """Create a video clip from a static image with slow zoom (Ken Burns)."""
    frames = max(1, int(duration * fps))
    zoom_step = (zoom_end - 1.0) / frames if frames > 1 else 0.0

    # Scale image first to avoid zoompan out-of-bounds
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
        scaled = tmp.name
    scale_image_for_zoompan(image_path, scaled, zoom_end)

    _run(
        [
            "ffmpeg", "-y",
            "-loop", "1",
            "-i", scaled,
            "-vf",
            (
                f"zoompan=z='min(zoom+{zoom_step:.6f},{zoom_end})'"
                f":d={frames}:s={size}:fps={fps},"
                "format=yuv420p"
            ),
            "-t", str(duration),
            "-an",
            "-c:v", "libx264",
            "-preset", "fast",
            "-crf", "18",
            output_path,
        ],
        "create_zoompan_clip",
    )
    Path(scaled).unlink(missing_ok=True)


def create_pan_down_clip(
    image_path: str,
    output_path: str,
    duration: float,
    fps: int = 30,
    size: str = "1080x1920",
) -> None:
    """Create a video clip with a downward pan effect."""
    frames = max(1, int(duration * fps))
    w, h = 1080, 1920
    # Pan from top to 15% down
    y_end = int(h * 0.15)
    y_step = y_end / frames if frames > 1 else 0

    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
        scaled = tmp.name
    # Scale to 1.15× height for pan room
    scale_w = w
    scale_h = int(h * 1.15 + 2)
    scale_h += scale_h % 2
    _run(
        ["ffmpeg", "-y", "-i", image_path, "-vf", f"scale={scale_w}:{scale_h}", scaled],
        "scale_for_pan",
    )

    _run(
        [
            "ffmpeg", "-y",
            "-loop", "1",
            "-i", scaled,
            "-vf",
            (
                f"crop={w}:{h}:0:'min(n*{y_step:.4f},{y_end})',"
                "format=yuv420p"
            ),
            "-t", str(duration),
            "-an",
            "-c:v", "libx264",
            "-preset", "fast",
            "-crf", "18",
            output_path,
        ],
        "create_pan_down_clip",
    )
    Path(scaled).unlink(missing_ok=True)


def create_static_clip(
    image_path: str,
    output_path: str,
    duration: float,
    fps: int = 30,
    size: str = "1080x1920",
) -> None:
    """Create a static (no motion) video clip from an image."""
    w, h = size.split("x")
    _run(
        [
            "ffmpeg", "-y",
            "-loop", "1",
            "-i", image_path,
            "-vf", f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
                   f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,format=yuv420p",
            "-t", str(duration),
            "-an",
            "-c:v", "libx264",
            "-preset", "fast",
            "-crf", "18",
            output_path,
        ],
        "create_static_clip",
    )


def concatenate_clips(clip_paths: list[str], output_path: str) -> None:
    """Concatenate multiple video clips using the concat demuxer."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
        list_file = f.name
        for p in clip_paths:
            f.write(f"file '{p}'\n")

    _run(
        [
            "ffmpeg", "-y",
            "-f", "concat",
            "-safe", "0",
            "-i", list_file,
            "-c", "copy",
            output_path,
        ],
        "concatenate_clips",
    )
    Path(list_file).unlink(missing_ok=True)


def add_audio(video_path: str, audio_path: str, output_path: str) -> None:
    """Mix video with audio track, trimming to shortest stream."""
    _run(
        [
            "ffmpeg", "-y",
            "-i", video_path,
            "-i", audio_path,
            "-c:v", "copy",
            "-c:a", "aac",
            "-b:a", "128k",
            "-shortest",
            output_path,
        ],
        "add_audio",
    )


def burn_subtitles(video_path: str, subtitle_path: str, output_path: str) -> None:
    """Burn ASS/SRT subtitle file into video."""
    # Escape backslashes and colons for FFmpeg filter on Windows
    safe_sub = subtitle_path.replace("\\", "/").replace(":", "\\:")
    _run(
        [
            "ffmpeg", "-y",
            "-i", video_path,
            "-vf", f"subtitles='{safe_sub}'",
            "-c:v", "libx264",
            "-preset", "medium",
            "-crf", "18",
            "-c:a", "copy",
            output_path,
        ],
        "burn_subtitles",
    )


def final_encode(video_path: str, output_path: str, crf: int = 18) -> None:
    """Final H.264 encode to ensure correct settings."""
    _run(
        [
            "ffmpeg", "-y",
            "-i", video_path,
            "-c:v", "libx264",
            "-preset", "medium",
            "-crf", str(crf),
            "-pix_fmt", "yuv420p",
            "-c:a", "aac",
            "-b:a", "128k",
            "-movflags", "+faststart",
            output_path,
        ],
        "final_encode",
    )


def simple_slideshow(
    image_paths: list[str],
    audio_path: str,
    output_path: str,
    fps: int = 30,
    size: str = "1080x1920",
) -> None:
    """Ultra-simple fallback: one image per second concat + audio."""
    if not image_paths:
        raise ValueError("No images provided for slideshow")

    audio_duration = get_audio_duration(audio_path)
    secs_per_image = max(1.0, audio_duration / len(image_paths))
    w, h = size.split("x")

    clip_paths: list[str] = []
    with tempfile.TemporaryDirectory() as tmpdir:
        for i, img in enumerate(image_paths):
            clip = str(Path(tmpdir) / f"clip_{i:04d}.mp4")
            create_static_clip(img, clip, secs_per_image, fps, size)
            clip_paths.append(clip)

        concat_video = str(Path(tmpdir) / "concat.mp4")
        concatenate_clips(clip_paths, concat_video)

        with_audio = str(Path(tmpdir) / "with_audio.mp4")
        add_audio(concat_video, audio_path, with_audio)
        final_encode(with_audio, output_path)
