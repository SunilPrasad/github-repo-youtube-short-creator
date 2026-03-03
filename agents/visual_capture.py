"""Stage 4 — Visual Capture Agent.

Produces VisualPlan containing all screenshots + generated cards.
Part A: Playwright screenshots of GitHub repo page
Part B: Rendered HTML cards (title, feature, code, CTA)
Part C: Pillow post-processing (compositing on dark canvas)
Part D: Section-to-visual mapping
"""

from __future__ import annotations

import asyncio
import html
import logging
import re
from pathlib import Path

from models.schemas import RepoData, VideoScript, VoiceOutput, VisualAsset, VisualPlan
from utils.image_processing import add_github_url_overlay, ensure_vertical, process_screenshot

logger = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).parent.parent / "templates"


# ── Helpers ───────────────────────────────────────────────────────────────────

def _format_stars(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


def _pick_icon(repo: RepoData) -> str:
    lang = (repo.language or "").lower()
    icons = {
        "python": "🐍", "javascript": "⚡", "typescript": "⚡",
        "go": "🐹", "rust": "🦀", "java": "☕",
        "c++": "⚙️", "c": "⚙️", "ruby": "💎", "swift": "🐦",
        "kotlin": "🔵", "shell": "🖥️", "dockerfile": "🐳",
    }
    return icons.get(lang, "🚀")


def _html_escape(text: str) -> str:
    return html.escape(text, quote=True)


def _extract_features(repo: RepoData, script: VideoScript) -> list[str]:
    """Extract 3–6 bullet-point features for the feature card."""
    features: list[str] = []

    # 1. Script features section — prefer pipe-separated format produced by LLM
    for sec in script.sections:
        if sec.id == "features":
            if "|" in sec.text:
                for part in sec.text.split("|"):
                    part = part.strip(" •-*")
                    if 5 < len(part) < 120:
                        features.append(part)
            else:
                for sent in re.split(r"[.!?]", sec.text):
                    sent = sent.strip()
                    if 10 < len(sent) < 120:
                        features.append(sent)

    # 2. README feature sections (bullet points under a "Features" heading)
    if len(features) < 3:
        for section in repo.readme_sections:
            if section.category == "features":
                for line in section.content_text.splitlines():
                    line = line.strip(" -•*◆→►▸▹●○")
                    if 10 < len(line) < 120:
                        features.append(line)

    # 3. Broader fallback — mine bullet lines from any README section
    if len(features) < 3:
        for section in repo.readme_sections:
            for line in section.content_text.splitlines():
                stripped = line.strip()
                if stripped.startswith(("-", "•", "*", "◆", "►", "▸", "●", "○")):
                    clean = stripped.strip(" -•*◆→►▸▹●○")
                    if 15 < len(clean) < 100 and not clean.endswith(":"):
                        features.append(clean)
            if len(features) >= 6:
                break

    # Deduplicate and limit
    seen: set[str] = set()
    unique: list[str] = []
    for f in features:
        key = f.lower()[:40]
        if key not in seen:
            seen.add(key)
            unique.append(f)

    if unique:
        return unique[:6]

    # Last resort: build from description + topics
    fallback: list[str] = []
    if repo.description:
        fallback.append(repo.description[:80])
    for topic in repo.topics[:3]:
        fallback.append(topic.replace("-", " ").title())
    return fallback[:4] if fallback else ["Open source", "Easy to use", "Well documented", "Active community"]


FEATURE_ICONS = ["⚡", "🔒", "🚀", "🎯", "🔧", "💡", "🌐", "📦"]


def _render_feature_items(features: list[str]) -> str:
    items_html = ""
    for i, feat in enumerate(features):
        icon = FEATURE_ICONS[i % len(FEATURE_ICONS)]
        items_html += (
            f'<div class="feature-item">'
            f'<div class="feature-icon">{icon}</div>'
            f'<div class="feature-text">{_html_escape(feat)}</div>'
            f"</div>\n"
        )
    return items_html


def _render_code_content(repo: RepoData) -> str:
    lines: list[str] = []

    install = repo.primary_install_command
    usage = repo.primary_usage_command

    if install:
        lines.append(f'<span class="prompt">$ </span>{_html_escape(install)}')
    if usage:
        lines.append("")
        lines.append(f'<span class="highlight-line"><span class="prompt">$ </span>{_html_escape(usage)}</span>')

    if not lines:
        lines = [
            f'<span class="prompt">$ </span>git clone {_html_escape(repo.html_url)}',
            f'<span class="prompt">$ </span>cd {_html_escape(repo.name)}',
        ]

    return "\n".join(lines)


# ── Part B: Card Rendering ────────────────────────────────────────────────────

async def _render_card(page, template_path: Path, replacements: dict[str, str], output_path: str) -> None:
    content = template_path.read_text(encoding="utf-8")
    for key, value in replacements.items():
        content = content.replace(f"{{{{{key}}}}}", value)
    await page.set_content(content, wait_until="domcontentloaded")
    await asyncio.sleep(0.5)
    await page.screenshot(path=output_path, clip={"x": 0, "y": 0, "width": 1080, "height": 1920})


async def _generate_cards(
    repo: RepoData,
    script: VideoScript,
    cards_dir: Path,
    browser,
) -> list[VisualAsset]:
    assets: list[VisualAsset] = []
    page = await browser.new_page(viewport={"width": 1080, "height": 1920})

    try:
        # Title Card
        title_path = str(cards_dir / "title_card.png")
        try:
            await _render_card(
                page,
                TEMPLATES_DIR / "title_card.html",
                {
                    "ICON": _pick_icon(repo),
                    "REPO_NAME": _html_escape(repo.name),
                    "OWNER": _html_escape(repo.owner),
                    "DESCRIPTION": _html_escape(repo.description[:120]),
                    "STARS": _format_stars(repo.stars),
                    "FORKS": _format_stars(repo.forks),
                    "LANGUAGE": _html_escape(repo.language or "Multi"),
                },
                title_path,
            )
            assets.append(VisualAsset(
                id="card_title",
                path=title_path,
                source_type="generated_card",
                mapped_section="what_is_it",
                description="Title card with repo name and stats",
                width=1080, height=1920,
            ))
            logger.info("Generated title card.")
        except Exception as exc:
            logger.warning("Title card failed: %s", exc)

        # Feature Card
        feature_path = str(cards_dir / "feature_card.png")
        try:
            features = _extract_features(repo, script)
            await _render_card(
                page,
                TEMPLATES_DIR / "feature_card.html",
                {
                    "REPO_NAME": _html_escape(repo.name),
                    "FULL_NAME": _html_escape(repo.full_name),
                    "FEATURE_ITEMS": _render_feature_items(features),
                },
                feature_path,
            )
            assets.append(VisualAsset(
                id="card_features",
                path=feature_path,
                source_type="generated_card",
                mapped_section="features",
                description="Key features card",
                width=1080, height=1920,
            ))
            logger.info("Generated feature card.")
        except Exception as exc:
            logger.warning("Feature card failed: %s", exc)

        # Code Card
        code_path = str(cards_dir / "code_card.png")
        try:
            await _render_card(
                page,
                TEMPLATES_DIR / "code_card.html",
                {
                    "REPO_NAME": _html_escape(repo.name),
                    "FULL_NAME": _html_escape(repo.full_name),
                    "CODE_CONTENT": _render_code_content(repo),
                    "DESCRIPTION_BLOCK": "",
                },
                code_path,
            )
            assets.append(VisualAsset(
                id="card_code",
                path=code_path,
                source_type="generated_card",
                mapped_section="usage",
                description="Code / get started card",
                width=1080, height=1920,
            ))
            logger.info("Generated code card.")
        except Exception as exc:
            logger.warning("Code card failed: %s", exc)

        # CTA Card
        cta_path = str(cards_dir / "cta_card.png")
        try:
            await _render_card(
                page,
                TEMPLATES_DIR / "cta_card.html",
                {
                    "REPO_NAME": _html_escape(repo.name),
                    "FULL_NAME": _html_escape(repo.full_name),
                },
                cta_path,
            )
            assets.append(VisualAsset(
                id="card_cta",
                path=cta_path,
                source_type="generated_card",
                mapped_section="cta",
                description="Call to action card",
                width=1080, height=1920,
            ))
            logger.info("Generated CTA card.")
        except Exception as exc:
            logger.warning("CTA card failed: %s", exc)

    finally:
        await page.close()

    return assets


# ── Mobile emulation constants ────────────────────────────────────────────────

_MOBILE_VIEWPORT_W = 390
_MOBILE_VIEWPORT_H = 844
_MOBILE_SCALE = 3
_MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1"
)


# ── Part A: GitHub Screenshots ────────────────────────────────────────────────

async def _screenshot_github(
    repo: RepoData,
    captures_dir: Path,
    browser,
) -> list[VisualAsset]:
    assets: list[VisualAsset] = []
    page = await browser.new_page(
        viewport={"width": _MOBILE_VIEWPORT_W, "height": _MOBILE_VIEWPORT_H},
        user_agent=_MOBILE_UA,
        is_mobile=True,
        has_touch=True,
        device_scale_factor=_MOBILE_SCALE,
    )

    try:
        await page.emulate_media(color_scheme="dark")
        logger.info("Navigating to %s ...", repo.html_url)
        await page.goto(repo.html_url, wait_until="networkidle", timeout=30000)
        await asyncio.sleep(2)

        # 1. GitHub full-page hook screenshot (ALWAYS first — reliable clip, no fragile selectors)
        hook_raw = str(captures_dir / "github_hook_raw.png")
        hook_processed = str(captures_dir / "github_hook.png")
        try:
            # Capture top portion — shows repo name, description, stars, language, topics
            await page.screenshot(
                path=hook_raw,
                clip={"x": 0, "y": 0, "width": _MOBILE_VIEWPORT_W, "height": 700},
            )
            process_screenshot(hook_raw, hook_processed)
            # Add URL badge overlay on top
            add_github_url_overlay(hook_processed, hook_processed, repo.full_name)
            assets.append(VisualAsset(
                id="screenshot_github_hook",
                path=hook_processed,
                source_type="screenshot",
                mapped_section="hook",
                description=f"GitHub page: {repo.full_name}",
                width=1080, height=1920,
            ))
            logger.info("Captured GitHub hook screenshot with URL overlay.")
        except Exception as exc:
            logger.warning("GitHub hook screenshot failed: %s", exc)

        # 2. README sections
        try:
            section_count = 0
            headings = await page.query_selector_all("#readme article.markdown-body h1, #readme article.markdown-body h2, #readme article.markdown-body h3")
            for i, heading in enumerate(headings[:6]):
                if section_count >= 6:
                    break
                try:
                    heading_text = await heading.inner_text()
                    # Compute bounding box of heading + content until next heading
                    bbox = await page.evaluate(
                        """(heading) => {
                            let top = heading.getBoundingClientRect().top + window.scrollY;
                            let bottom = top + heading.offsetHeight;
                            let el = heading.nextElementSibling;
                            while (el && !['H1','H2','H3'].includes(el.tagName)) {
                                let r = el.getBoundingClientRect();
                                bottom = Math.max(bottom, r.bottom + window.scrollY);
                                el = el.nextElementSibling;
                            }
                            return {x: 0, y: top, width: window.innerWidth, height: Math.min(bottom - top, 800)};
                        }""",
                        heading,
                    )
                    if bbox["height"] < 40:
                        continue

                    cat = _categorize_heading(heading_text)
                    section_id = _section_cat_to_script_id(cat)
                    safe_name = re.sub(r"[^\w]", "_", heading_text.lower())[:30]
                    raw_path = str(captures_dir / f"readme_{i}_{safe_name}.png")
                    processed_path = str(captures_dir / f"readme_{i}_{safe_name}_proc.png")

                    await page.screenshot(path=raw_path, clip=bbox)
                    process_screenshot(raw_path, processed_path)

                    assets.append(VisualAsset(
                        id=f"screenshot_readme_{i}",
                        path=processed_path,
                        source_type="screenshot",
                        mapped_section=section_id,
                        description=f"README section: {heading_text}",
                        width=1080, height=1920,
                    ))
                    section_count += 1
                except Exception as exc:
                    logger.warning("README section %d screenshot failed: %s", i, exc)
        except Exception as exc:
            logger.warning("README sections screenshot failed: %s", exc)

        # 3. Code blocks
        try:
            code_blocks = await page.query_selector_all("#readme article.markdown-body pre")
            code_count = 0
            for i, pre in enumerate(code_blocks):
                if code_count >= 4:
                    break
                try:
                    code_text = await pre.inner_text()
                    lines = [l for l in code_text.splitlines() if l.strip()]
                    if len(lines) > 20:
                        continue  # too long
                    if not any(
                        kw in code_text.lower()
                        for kw in ["pip", "npm", "install", "run", "curl", "brew", "import", "from"]
                    ):
                        continue

                    bbox = await pre.bounding_box()
                    if not bbox or bbox["height"] < 20:
                        continue

                    raw_path = str(captures_dir / f"code_block_{i}.png")
                    processed_path = str(captures_dir / f"code_block_{i}_proc.png")
                    await pre.screenshot(path=raw_path)
                    process_screenshot(raw_path, processed_path)

                    assets.append(VisualAsset(
                        id=f"screenshot_code_{i}",
                        path=processed_path,
                        source_type="screenshot",
                        mapped_section="usage",
                        description=f"Code block {i}",
                        width=1080, height=1920,
                    ))
                    code_count += 1
                except Exception as exc:
                    logger.warning("Code block %d screenshot failed: %s", i, exc)
        except Exception as exc:
            logger.warning("Code blocks screenshot failed: %s", exc)

        # 4. File tree
        file_tree_path = str(captures_dir / "file_tree.png")
        try:
            tree_el = await page.query_selector('[aria-labelledby="folders-and-files"]')
            if tree_el:
                await tree_el.screenshot(path=file_tree_path)
            else:
                await page.screenshot(
                    path=file_tree_path,
                    clip={"x": 0, "y": 300, "width": _MOBILE_VIEWPORT_W, "height": 500},
                )
            processed = str(captures_dir / "file_tree_proc.png")
            process_screenshot(file_tree_path, processed)
            assets.append(VisualAsset(
                id="screenshot_filetree",
                path=processed,
                source_type="screenshot",
                mapped_section="how_it_works",
                description="Repository file tree",
                width=1080, height=1920,
            ))
            logger.info("Captured file tree.")
        except Exception as exc:
            logger.warning("File tree screenshot failed: %s", exc)

    finally:
        await page.close()

    return assets


def _categorize_heading(heading: str) -> str:
    h = heading.lower()
    checks = [
        ("install", ["install", "setup", "getting started", "quickstart"]),
        ("features", ["feature", "highlight", "key", "why"]),
        ("architecture", ["architecture", "how it works", "design", "overview"]),
        ("examples", ["example", "demo", "usage", "tutorial"]),
        ("api", ["api", "endpoint", "rest", "sdk", "library"]),
        ("models", ["model", "supported", "compatibility"]),
    ]
    for cat, keywords in checks:
        if any(kw in h for kw in keywords):
            return cat
    return "other"


def _section_cat_to_script_id(category: str) -> str:
    mapping = {
        "install": "usage",
        "features": "features",
        "architecture": "how_it_works",
        "examples": "usage",
        "api": "features",
        "models": "what_is_it",
        "other": "what_is_it",
    }
    return mapping.get(category, "what_is_it")


# ── Main Agent ────────────────────────────────────────────────────────────────

async def capture_visuals(
    repo: RepoData,
    script: VideoScript,
    voice: VoiceOutput,
    output_dir: str,
    config,
) -> VisualPlan:
    """Main entry point. Returns VisualPlan with all visual assets."""
    from playwright.async_api import async_playwright

    captures_dir = Path(output_dir) / "captures"
    cards_dir = Path(output_dir) / "cards"
    captures_dir.mkdir(parents=True, exist_ok=True)
    cards_dir.mkdir(parents=True, exist_ok=True)

    all_assets: list[VisualAsset] = []

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            # Part A: GitHub screenshots
            github_assets = await _screenshot_github(repo, captures_dir, browser)
            all_assets.extend(github_assets)

            # Part B: Generated cards
            card_assets = await _generate_cards(repo, script, cards_dir, browser)
            all_assets.extend(card_assets)
        finally:
            await browser.close()

    # Ensure we have at least a title card as fallback for every section
    _ensure_fallback_coverage(all_assets)

    logger.info("Visual capture complete: %d assets total.", len(all_assets))
    return VisualPlan(assets=all_assets)


def _ensure_fallback_coverage(assets: list[VisualAsset]) -> None:
    """Make sure every script section has at least one visual (use title card as fallback)."""
    from models.schemas import SECTION_IDS

    covered = {a.mapped_section for a in assets}
    title_card = next((a for a in assets if a.id == "card_title"), None)

    if title_card is None:
        return

    for sid in SECTION_IDS:
        if sid not in covered:
            logger.debug("No visual for section '%s' — using title card as fallback.", sid)
            assets.append(VisualAsset(
                id=f"fallback_{sid}",
                path=title_card.path,
                source_type="generated_card",
                mapped_section=sid,
                description=f"Fallback: title card for {sid}",
                width=1080, height=1920,
            ))
