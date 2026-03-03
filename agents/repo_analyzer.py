"""Stage 1 — Repo Analyzer Agent.

Takes a GitHub URL and returns a fully-populated RepoData object.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re

logger = logging.getLogger(__name__)
from datetime import datetime, timezone
from urllib.parse import urlparse

import httpx
import markdown
from bs4 import BeautifulSoup

from models.schemas import CodeBlock, FileEntry, ReadmeSection, RepoData


# ── URL Parsing ──────────────────────────────────────────────────────────────

def parse_github_url(url: str) -> tuple[str, str]:
    """Extract (owner, repo) from any GitHub URL format."""
    url = url.strip().rstrip("/")
    if url.endswith(".git"):
        url = url[:-4]
    parsed = urlparse(url)
    parts = [p for p in parsed.path.split("/") if p]
    if len(parts) < 2:
        raise ValueError(f"Cannot parse owner/repo from URL: {url}")
    owner, repo = parts[0], parts[1]
    return owner, repo


# ── HTTP Client ───────────────────────────────────────────────────────────────

def _build_headers(with_token: bool = True) -> dict[str, str]:
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if with_token:
        token = os.environ.get("GITHUB_TOKEN", "")
        # Only use token if it looks real (not a placeholder like "ghp_...")
        if token and not token.endswith("...") and len(token) > 10:
            headers["Authorization"] = f"Bearer {token}"
    return headers


async def _fetch_with_retry(
    client: httpx.AsyncClient, url: str, extra_headers: dict | None = None
) -> httpx.Response:
    delays = [1, 2, 4]
    last_exc: Exception | None = None
    use_token = True

    for delay in delays:
        headers = _build_headers(with_token=use_token)
        if extra_headers:
            headers.update(extra_headers)
        try:
            resp = await client.get(url, headers=headers, timeout=30)
            if resp.status_code == 404:
                raise ValueError(f"Repository not found: {url}")
            if resp.status_code == 401:
                # Invalid token — retry without it
                logger.warning("GitHub 401 Unauthorized (invalid token?). Retrying without auth.")
                use_token = False
                continue
            if resp.status_code == 403:
                raise PermissionError(
                    "GitHub API rate limit exceeded. Set a valid GITHUB_TOKEN to increase "
                    "the limit from 60 to 5000 requests/hr."
                )
            resp.raise_for_status()
            return resp
        except (ValueError, PermissionError):
            raise
        except Exception as exc:
            last_exc = exc
            await asyncio.sleep(delay)
    raise RuntimeError(f"Failed to fetch {url} after retries: {last_exc}") from last_exc


# ── README Parsing ────────────────────────────────────────────────────────────

_CATEGORY_KEYWORDS: list[tuple[str, list[str]]] = [
    ("install",      ["install", "setup", "getting started", "quickstart", "quick start"]),
    ("features",     ["feature", "highlight", "key", "why"]),
    ("architecture", ["architecture", "how it works", "design", "overview"]),
    ("examples",     ["example", "demo", "usage", "tutorial"]),
    ("api",          ["api", "endpoint", "rest", "sdk", "library"]),
    ("models",       ["model", "supported", "compatibility"]),
]


def _categorize_heading(heading: str) -> str:
    h = heading.lower()
    for category, keywords in _CATEGORY_KEYWORDS:
        if any(kw in h for kw in keywords):
            return category
    return "other"


def _extract_code_blocks(soup_element) -> list[CodeBlock]:
    blocks = []
    for pre in soup_element.find_all("pre"):
        code = pre.find("code")
        if not code:
            continue
        lang = ""
        for cls in (code.get("class") or []):
            if cls.startswith("language-"):
                lang = cls[len("language-"):]
                break
        blocks.append(CodeBlock(language=lang, content=code.get_text()))
    return blocks


def _extract_images(soup_element, base_url: str, owner: str, repo: str, branch: str) -> list[str]:
    images = []
    for img in soup_element.find_all("img"):
        src = img.get("src") or img.get("data-src") or ""
        if not src:
            continue
        images.append(_resolve_image_url(src, owner, repo, branch))
    return images


def _resolve_image_url(src: str, owner: str, repo: str, branch: str) -> str:
    if src.startswith("http"):
        return src
    src = src.lstrip("/")
    return f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{src}"


def _parse_readme(raw_md: str, owner: str, repo: str, branch: str) -> list[ReadmeSection]:
    html = markdown.markdown(raw_md, extensions=["fenced_code", "tables"])
    soup = BeautifulSoup(html, "html.parser")
    sections: list[ReadmeSection] = []

    heading_tags = {"h1", "h2", "h3"}
    elements = list(soup.children)

    current_heading: str | None = None
    current_level: int = 0
    current_nodes: list = []

    def _flush():
        if current_heading is None:
            return
        container = BeautifulSoup("", "html.parser")
        for node in current_nodes:
            container.append(node.__copy__() if hasattr(node, "__copy__") else type(node)(node))
        content_html = "".join(str(n) for n in current_nodes)
        content_text = " ".join(
            n.get_text(separator=" ", strip=True) if hasattr(n, "get_text") else str(n)
            for n in current_nodes
        ).strip()
        fake_soup = BeautifulSoup(content_html, "html.parser")
        code_blocks = _extract_code_blocks(fake_soup)
        images = _extract_images(fake_soup, "", owner, repo, branch)
        sections.append(
            ReadmeSection(
                heading=current_heading,
                level=current_level,
                content_text=content_text,
                content_html=content_html,
                code_blocks=code_blocks,
                images=images,
                category=_categorize_heading(current_heading),
            )
        )

    for elem in elements:
        tag = getattr(elem, "name", None)
        if tag in heading_tags:
            _flush()
            current_heading = elem.get_text(strip=True)
            current_level = int(tag[1])
            current_nodes = []
        else:
            current_nodes.append(elem)

    _flush()
    return sections


# ── Install / Usage Command Extraction ───────────────────────────────────────

_INSTALL_PATTERNS = [
    r"pip install\b[^\n]+",
    r"npm install\b[^\n]+",
    r"yarn add\b[^\n]+",
    r"brew install\b[^\n]+",
    r"curl\s+.*\|.*sh[^\n]*",
    r"apt(-get)? install\b[^\n]+",
    r"go install\b[^\n]+",
    r"cargo install\b[^\n]+",
]


def _find_primary_install(sections: list[ReadmeSection]) -> str | None:
    candidates: list[str] = []
    for section in sections:
        if section.category != "install":
            continue
        for block in section.code_blocks:
            for line in block.content.splitlines():
                line = line.strip()
                for pattern in _INSTALL_PATTERNS:
                    if re.search(pattern, line, re.IGNORECASE):
                        candidates.append(line)
    if not candidates:
        return None
    return min(candidates, key=len)


def _find_primary_usage(sections: list[ReadmeSection]) -> str | None:
    for section in sections:
        if section.category not in ("examples", "usage", "other"):
            continue
        for block in section.code_blocks:
            lines = [l.strip() for l in block.content.splitlines() if l.strip()]
            if lines and len(lines) <= 5:
                return lines[0]
    return None


# ── Derived Fields ────────────────────────────────────────────────────────────

def _popularity_tier(stars: int) -> str:
    if stars >= 50_000:
        return "viral"
    if stars >= 10_000:
        return "popular"
    if stars >= 1_000:
        return "growing"
    return "emerging"


def _is_recently_updated(updated_at: str) -> bool:
    try:
        dt = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
        delta = datetime.now(timezone.utc) - dt
        return delta.days <= 30
    except Exception:
        return False


# ── Main Agent ────────────────────────────────────────────────────────────────

async def analyze_repo(url: str) -> RepoData:
    """Main entry point. Returns fully populated RepoData."""
    owner, repo = parse_github_url(url)

    async with httpx.AsyncClient() as client:
        # 1. Repo metadata
        meta_resp = await _fetch_with_retry(
            client, f"https://api.github.com/repos/{owner}/{repo}"
        )
        meta = meta_resp.json()

        # 2. README (raw markdown)
        readme_raw = ""
        try:
            readme_resp = await _fetch_with_retry(
                client,
                f"https://api.github.com/repos/{owner}/{repo}/readme",
                extra_headers={"Accept": "application/vnd.github.raw+json"},
            )
            readme_raw = readme_resp.text
        except Exception:
            readme_raw = meta.get("description") or ""

        # 3. File tree
        default_branch = meta.get("default_branch", "main")
        file_tree: list[FileEntry] = []
        try:
            tree_resp = await _fetch_with_retry(
                client,
                f"https://api.github.com/repos/{owner}/{repo}/git/trees/{default_branch}",
            )
            for entry in tree_resp.json().get("tree", []):
                entry_type = "dir" if entry.get("type") == "tree" else "file"
                file_tree.append(FileEntry(name=entry["path"], type=entry_type))
        except Exception:
            pass

    # 4. Parse README
    readme_sections = _parse_readme(readme_raw, owner, repo, default_branch) if readme_raw else []

    # 5. Collect images across all sections
    all_images: list[str] = []
    for s in readme_sections:
        all_images.extend(s.images)

    # 6. License
    license_info = meta.get("license")
    license_name = license_info.get("name") if isinstance(license_info, dict) else None

    return RepoData(
        name=meta["name"],
        full_name=meta["full_name"],
        owner=owner,
        description=meta.get("description") or "",
        stars=meta.get("stargazers_count", 0),
        forks=meta.get("forks_count", 0),
        language=meta.get("language") or "",
        topics=meta.get("topics") or [],
        license=license_name,
        html_url=meta["html_url"],
        default_branch=default_branch,
        popularity_tier=_popularity_tier(meta.get("stargazers_count", 0)),
        is_recently_updated=_is_recently_updated(meta.get("updated_at", "")),
        primary_install_command=_find_primary_install(readme_sections),
        primary_usage_command=_find_primary_usage(readme_sections),
        readme_sections=readme_sections,
        readme_full_text=readme_raw,
        file_tree=file_tree,
        readme_images=all_images,
    )
