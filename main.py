#!/usr/bin/env python3
"""GitHub → YouTube Shorts Pipeline — Orchestrator.

Usage:
    python main.py --url "https://github.com/ollama/ollama"
    python main.py --urls-file repos.txt
    python main.py --url "https://github.com/openai/whisper" --llm openai --voice-id "ErXwobaYiN019PkySvjV"
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import yaml
from dotenv import load_dotenv

# Load .env file if present (before anything reads os.environ)
load_dotenv()

# ── Logging Setup ─────────────────────────────────────────────────────────────

def setup_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    # Silence noisy third-party loggers
    for name in ("httpx", "httpcore", "playwright", "asyncio"):
        logging.getLogger(name).setLevel(logging.WARNING)


# ── Config Loading ────────────────────────────────────────────────────────────

def _dict_to_namespace(d: dict) -> SimpleNamespace:
    ns = SimpleNamespace()
    for k, v in d.items():
        setattr(ns, k, _dict_to_namespace(v) if isinstance(v, dict) else v)
    return ns


def load_config(config_path: str = "config.yaml", cli_overrides: dict | None = None) -> SimpleNamespace:
    with open(config_path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    config = _dict_to_namespace(data)

    # Apply CLI overrides
    if cli_overrides:
        if cli_overrides.get("voice_id"):
            config.voice.elevenlabs_voice_id = cli_overrides["voice_id"]
        if cli_overrides.get("llm"):
            config.llm.provider = cli_overrides["llm"]

    return config


# ── Output Directory ──────────────────────────────────────────────────────────

def make_output_dir(base_dir: str, repo_name: str) -> Path:
    safe_name = repo_name.replace("/", "_").replace("\\", "_")
    out = Path(base_dir) / safe_name
    (out / "captures").mkdir(parents=True, exist_ok=True)
    (out / "cards").mkdir(parents=True, exist_ok=True)
    return out


# ── Metadata Writer ───────────────────────────────────────────────────────────

def write_metadata(repo_data, script, output_dir: Path) -> None:
    tags = (repo_data.topics or [])[:10]
    tags += ["opensource", "github", "coding", "developer", "programming"]
    tags = list(dict.fromkeys(tags))[:20]  # dedupe, limit

    title = f"{repo_data.name} — {repo_data.description[:60]}" if repo_data.description else repo_data.name
    title = title[:100]

    description = (
        f"{script.sections[0].text}\n\n"
        f"GitHub: {repo_data.html_url}\n\n"
        f"#{' #'.join(tags[:8])}"
    )

    metadata = {
        "title": title,
        "description": description,
        "tags": tags,
        "repo_url": repo_data.html_url,
        "stars": repo_data.stars,
        "language": repo_data.language,
        "tts_provider": "See voiceover.mp3",
    }

    (output_dir / "metadata.json").write_text(
        json.dumps(metadata, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def write_script_txt(script, output_dir: Path) -> None:
    lines: list[str] = []
    for section in script.sections:
        lines.append(f"[{section.label.upper()}]")
        lines.append(section.text)
        lines.append("")
    (output_dir / "script.txt").write_text("\n".join(lines), encoding="utf-8")


# ── Single Repo Pipeline ──────────────────────────────────────────────────────

async def process_repo(url: str, config, base_output_dir: str, skip_video: bool = False) -> bool:
    """Run all 5 stages for a single repo URL. Returns True on success."""
    logger = logging.getLogger("pipeline")

    from agents.repo_analyzer import analyze_repo
    from agents.script_writer import write_script
    from agents.voice_generator import generate_voice
    from agents.visual_capture import capture_visuals
    from agents.video_composer import compose_video

    logger.info("=" * 60)
    logger.info("Processing: %s", url)
    logger.info("=" * 60)

    # Stage 1: Repo Analyzer
    logger.info("[Stage 1/5] Analyzing repository...")
    try:
        repo_data = await analyze_repo(url)
        logger.info("Repo: %s (%s stars, %s)", repo_data.full_name, f"{repo_data.stars:,}", repo_data.language)
    except Exception as exc:
        logger.error("Stage 1 failed: %s — skipping this repo.", exc)
        return False

    # Setup output dir (now we know the repo name)
    output_dir = make_output_dir(base_output_dir, repo_data.name)
    logger.info("Output dir: %s", output_dir)

    # Stage 2: Script Writer (requires ANTHROPIC_API_KEY or OPENAI_API_KEY)
    logger.info("[Stage 2/5] Writing script...")
    try:
        script = write_script(repo_data, config)
        logger.info(
            "Script: %d words, ~%.0fs. Sections: %s",
            script.total_word_count,
            script.estimated_duration_seconds,
            ", ".join(s.id for s in script.sections),
        )
        write_script_txt(script, output_dir)
    except Exception as exc:
        logger.error("Stage 2 failed: %s", exc)
        return False

    # Stage 3: Voice Generator
    logger.info("[Stage 3/5] Generating voiceover...")
    try:
        voice = await generate_voice(script, str(output_dir), config)
        logger.info(
            "Voice: %.2fs audio via %s, %d word timestamps",
            voice.duration_seconds,
            voice.tts_provider,
            len(voice.word_timestamps),
        )
    except Exception as exc:
        logger.error("Stage 3 failed (fatal for this repo): %s", exc)
        return False

    # Stage 4: Visual Capture
    logger.info("[Stage 4/5] Capturing visuals...")
    try:
        visual_plan = await capture_visuals(repo_data, script, voice, str(output_dir), config)
        logger.info("Visuals: %d assets captured.", len(visual_plan.assets))
    except Exception as exc:
        logger.warning("Stage 4 partially failed: %s — using available assets.", exc)
        from models.schemas import VisualPlan
        visual_plan = VisualPlan(assets=[])

    if not visual_plan.assets:
        logger.error("No visual assets — cannot compose video.")
        return False

    # Stage 5: Video Composer
    if skip_video:
        logger.info("[Stage 5/5] Skipped (--skip-video).")
        write_metadata(repo_data, script, output_dir)
        return True

    logger.info("[Stage 5/5] Composing video...")
    try:
        video_path = compose_video(voice, visual_plan, script, str(output_dir), config)
        logger.info("Video: %s", video_path)
    except Exception as exc:
        logger.error("Stage 5 failed: %s — saving intermediate artifacts.", exc)
        write_metadata(repo_data, script, output_dir)
        return False

    # Write metadata
    write_metadata(repo_data, script, output_dir)

    logger.info("SUCCESS: %s", output_dir)
    logger.info("  final_video.mp4 | script.txt | voiceover.mp3 | subtitles.srt | metadata.json")
    return True


# ── CLI ───────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="GitHub → YouTube Shorts: turn any GitHub repo into a ready-to-upload Short.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--url", metavar="URL", help="Single GitHub repository URL")
    group.add_argument(
        "--urls-file", metavar="FILE",
        help="Text file with one GitHub URL per line (batch mode)"
    )
    parser.add_argument(
        "--output-dir", default="./outputs", metavar="DIR",
        help="Base directory for all outputs (default: ./outputs)"
    )
    parser.add_argument(
        "--voice-id", metavar="ID",
        help="Override ElevenLabs voice ID from CLI"
    )
    parser.add_argument(
        "--llm", choices=["anthropic", "openai"],
        help="Override LLM provider (anthropic or openai)"
    )
    parser.add_argument(
        "--skip-video", action="store_true",
        help="Generate everything except final video (for debugging)"
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="Enable verbose/debug logging"
    )
    parser.add_argument(
        "--config", default="config.yaml", metavar="FILE",
        help="Path to config YAML (default: config.yaml)"
    )
    return parser.parse_args()


async def main() -> int:
    args = parse_args()
    setup_logging(args.verbose)
    logger = logging.getLogger("main")

    # Check config file
    if not Path(args.config).exists():
        logger.error("Config file not found: %s", args.config)
        return 1

    config = load_config(
        args.config,
        cli_overrides={"voice_id": args.voice_id, "llm": args.llm},
    )

    # Collect URLs
    if args.url:
        urls = [args.url]
    else:
        urls_file = Path(args.urls_file)
        if not urls_file.exists():
            logger.error("URLs file not found: %s", args.urls_file)
            return 1
        urls = [line.strip() for line in urls_file.read_text().splitlines() if line.strip() and not line.startswith("#")]

    if not urls:
        logger.error("No URLs provided.")
        return 1

    logger.info("Processing %d repo(s)...", len(urls))

    # Run pipeline for each URL
    results: list[tuple[str, bool]] = []
    for url in urls:
        success = await process_repo(
            url=url,
            config=config,
            base_output_dir=args.output_dir,
            skip_video=args.skip_video,
        )
        results.append((url, success))

    # Summary
    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    success_count = sum(1 for _, ok in results if ok)
    for url, ok in results:
        status = "OK" if ok else "FAILED"
        print(f"  [{status}] {url}")
    print(f"\n{success_count}/{len(results)} repos processed successfully.")
    print(f"Outputs in: {Path(args.output_dir).resolve()}")

    return 0 if success_count > 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
