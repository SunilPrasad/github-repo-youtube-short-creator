"""Stage 5 — Video Composer Agent.

Takes VoiceOutput + VisualPlan + VideoScript and produces the final MP4.
Two-pass approach:
  Pass 1: Create per-clip MP4 from each image (zoompan / pan_down / static)
  Pass 2: Concat all clips + add audio + burn subtitles + final encode
"""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path

from models.schemas import (
    SECTION_IDS,
    TimelineEntry,
    VideoScript,
    VisualAsset,
    VisualPlan,
    VoiceOutput,
)
from utils.ffmpeg_helpers import (
    add_audio,
    burn_subtitles,
    concatenate_clips,
    create_pan_down_clip,
    create_static_clip,
    create_zoompan_clip,
    final_encode,
    simple_slideshow,
)

logger = logging.getLogger(__name__)

EFFECTS = ["zoom_in", "pan_down", "zoom_in", "static", "zoom_in", "pan_down"]


# ── Timeline Builder ──────────────────────────────────────────────────────────

def build_timeline(
    script: VideoScript,
    voice: VoiceOutput,
    visual_plan: VisualPlan,
) -> list[TimelineEntry]:
    """Map script sections + visuals to a flat timeline."""
    entries: list[TimelineEntry] = []
    effect_cycle = 0

    for section in script.sections:
        sid = section.id
        section_assets = visual_plan.by_section(sid)

        # Determine time range for this section
        if sid in voice.section_times:
            sec_start, sec_end = voice.section_times[sid]
        else:
            # Estimate from script position
            total_dur = voice.duration_seconds
            idx = list(SECTION_IDS).index(sid) if sid in SECTION_IDS else 0
            n = len(script.sections)
            sec_start = total_dur * idx / n
            sec_end = total_dur * (idx + 1) / n

        sec_duration = max(0.5, sec_end - sec_start)

        if not section_assets:
            # No visual — use first available as fallback
            fallback = visual_plan.assets[0] if visual_plan.assets else None
            if fallback:
                section_assets = [fallback]
            else:
                continue

        # Split section time evenly across available assets
        time_per_asset = sec_duration / len(section_assets)
        for i, asset in enumerate(section_assets):
            effect = EFFECTS[effect_cycle % len(EFFECTS)]
            effect_cycle += 1

            zoom_end = 1.10 + (0.05 if "zoom" in effect else 0.0)

            entries.append(TimelineEntry(
                start_time=round(sec_start + i * time_per_asset, 3),
                end_time=round(sec_start + (i + 1) * time_per_asset, 3),
                visual_asset_id=asset.id,
                effect=effect,
                zoom_start=1.0,
                zoom_end=zoom_end,
            ))

    return entries


# ── Clip Creation ─────────────────────────────────────────────────────────────

def _create_clip_for_entry(
    entry: TimelineEntry,
    asset: VisualAsset,
    clip_path: str,
    config,
) -> bool:
    """Create a single video clip for a timeline entry. Returns True on success."""
    duration = max(0.3, entry.end_time - entry.start_time)
    fps = getattr(config.video, "fps", 30)
    size = f"{getattr(config.video, 'width', 1080)}x{getattr(config.video, 'height', 1920)}"

    try:
        if entry.effect == "zoom_in":
            create_zoompan_clip(
                image_path=asset.path,
                output_path=clip_path,
                duration=duration,
                fps=fps,
                zoom_end=entry.zoom_end,
                size=size,
            )
        elif entry.effect == "pan_down":
            create_pan_down_clip(
                image_path=asset.path,
                output_path=clip_path,
                duration=duration,
                fps=fps,
                size=size,
            )
        else:  # static
            create_static_clip(
                image_path=asset.path,
                output_path=clip_path,
                duration=duration,
                fps=fps,
                size=size,
            )
        return True
    except Exception as exc:
        logger.warning("Clip creation failed for %s (effect=%s): %s", asset.id, entry.effect, exc)
        # Retry with static (most robust)
        if entry.effect != "static":
            try:
                create_static_clip(asset.path, clip_path, duration, fps, size)
                return True
            except Exception as exc2:
                logger.error("Static fallback also failed: %s", exc2)
        return False


# ── Main Composer ─────────────────────────────────────────────────────────────

def compose_video(
    voice: VoiceOutput,
    visual_plan: VisualPlan,
    script: VideoScript,
    output_dir: str,
    config,
) -> str:
    """Compose final MP4. Returns path to final_video.mp4."""
    output_path = str(Path(output_dir) / "final_video.mp4")
    ass_path = str(Path(output_dir) / "subtitles.ass")
    srt_path = str(Path(output_dir) / "subtitles.srt")
    sub_path = ass_path if Path(ass_path).exists() else srt_path

    # Build timeline
    timeline = build_timeline(script, voice, visual_plan)
    logger.info("Timeline: %d entries.", len(timeline))

    if not timeline:
        logger.error("No timeline entries — cannot compose video.")
        raise RuntimeError("Empty timeline: no visual assets mapped to script sections.")

    # Build asset lookup
    asset_by_id = {a.id: a for a in visual_plan.assets}

    with tempfile.TemporaryDirectory() as tmpdir:
        # Pass 1: create per-clip MP4s
        clip_paths: list[str] = []
        failed_assets: list[VisualAsset] = []

        for i, entry in enumerate(timeline):
            asset = asset_by_id.get(entry.visual_asset_id)
            if not asset:
                logger.warning("Asset '%s' not found in plan.", entry.visual_asset_id)
                continue

            clip_path = str(Path(tmpdir) / f"clip_{i:04d}.mp4")
            success = _create_clip_for_entry(entry, asset, clip_path, config)
            if success and Path(clip_path).exists():
                clip_paths.append(clip_path)
                logger.debug("Clip %d created: %.2fs %s", i, entry.end_time - entry.start_time, entry.effect)
            else:
                failed_assets.append(asset)

        if not clip_paths:
            logger.warning("All clip creation failed — falling back to simple slideshow.")
            all_image_paths = [a.path for a in visual_plan.assets if Path(a.path).exists()]
            simple_slideshow(all_image_paths, voice.audio_path, output_path, fps=getattr(config.video, "fps", 30))
            return output_path

        if failed_assets:
            logger.warning("%d clips failed; continuing with %d clips.", len(failed_assets), len(clip_paths))

        # Pass 2: concat + audio + subtitles + final encode
        concat_path = str(Path(tmpdir) / "concat.mp4")
        with_audio_path = str(Path(tmpdir) / "with_audio.mp4")
        with_subs_path = str(Path(tmpdir) / "with_subs.mp4")

        try:
            logger.info("Concatenating %d clips...", len(clip_paths))
            concatenate_clips(clip_paths, concat_path)
        except Exception as exc:
            logger.error("Concatenation failed: %s — using simple slideshow fallback.", exc)
            all_image_paths = [a.path for a in visual_plan.assets if Path(a.path).exists()]
            simple_slideshow(all_image_paths, voice.audio_path, output_path, fps=getattr(config.video, "fps", 30))
            return output_path

        try:
            logger.info("Adding audio...")
            add_audio(concat_path, voice.audio_path, with_audio_path)
        except Exception as exc:
            logger.error("Add audio failed: %s", exc)
            # Copy concat as-is for debugging
            import shutil
            shutil.copy(concat_path, output_path)
            return output_path

        try:
            if Path(sub_path).exists():
                logger.info("Burning subtitles (%s)...", sub_path)
                burn_subtitles(with_audio_path, sub_path, with_subs_path)
            else:
                logger.warning("No subtitle file found — skipping subtitle burn.")
                with_subs_path = with_audio_path
        except Exception as exc:
            logger.warning("Subtitle burning failed: %s — skipping.", exc)
            with_subs_path = with_audio_path

        try:
            logger.info("Final encode → %s", output_path)
            final_encode(with_subs_path, output_path, crf=getattr(config.video, "crf", 18))
        except Exception as exc:
            logger.error("Final encode failed: %s", exc)
            import shutil
            shutil.copy(with_subs_path, output_path)

    logger.info("Video composition complete: %s", output_path)
    return output_path
