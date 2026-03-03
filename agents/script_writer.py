"""Stage 2 — Script Writer Agent.

Takes RepoData and produces a VideoScript using Claude (Anthropic) or OpenAI.
An LLM API key is REQUIRED — set ANTHROPIC_API_KEY or OPENAI_API_KEY.
"""

from __future__ import annotations

import json
import logging
import os
import re
import textwrap
from abc import ABC, abstractmethod

from models.schemas import (
    SECTION_IDS,
    SECTION_LABELS,
    RepoData,
    ScriptSection,
    VideoScript,
)

logger = logging.getLogger(__name__)


# ── LLM Abstraction ───────────────────────────────────────────────────────────

class LLMClient(ABC):
    @abstractmethod
    def generate(self, system_prompt: str, user_prompt: str) -> str: ...


class AnthropicClient(LLMClient):
    def __init__(self, model: str = "claude-sonnet-4-20250514"):
        import anthropic
        self._client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        self._model = model

    def generate(self, system_prompt: str, user_prompt: str) -> str:
        resp = self._client.messages.create(
            model=self._model,
            max_tokens=1000,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
        )
        return resp.content[0].text


class OpenAIClient(LLMClient):
    def __init__(self, model: str = "gpt-4o"):
        import openai
        self._client = openai.OpenAI(api_key=os.environ["OPENAI_API_KEY"])
        self._model = model

    def generate(self, system_prompt: str, user_prompt: str) -> str:
        resp = self._client.chat.completions.create(
            model=self._model,
            max_tokens=1000,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        )
        return resp.choices[0].message.content


def get_llm_client(provider: str, config) -> LLMClient:
    if provider == "anthropic" and os.environ.get("ANTHROPIC_API_KEY"):
        return AnthropicClient(model=getattr(config.llm, "anthropic_model", "claude-sonnet-4-20250514"))
    if provider == "openai" and os.environ.get("OPENAI_API_KEY"):
        return OpenAIClient(model=getattr(config.llm, "openai_model", "gpt-4o"))
    raise EnvironmentError(f"No API key available for provider '{provider}'")


def _other_provider(provider: str) -> str:
    return "openai" if provider == "anthropic" else "anthropic"


# ── Prompt Building ───────────────────────────────────────────────────────────

_SYSTEM_PROMPT = textwrap.dedent("""
You are an expert YouTube Shorts scriptwriter. Your scripts stop people mid-scroll.
You write for developers. Every word earns its place.

════════════════════════════════════════
THE HOOK — THIS IS EVERYTHING
════════════════════════════════════════
The hook is the ONLY thing that matters. If it's weak, nobody watches.

Hook MUST:
- Be the very first sentence, under 20 words
- Contain the exact star count (e.g. "163,000 stars")
- Make a BOLD, surprising, or provocative claim
- Create instant FOMO or curiosity — the viewer must feel they're missing out
- Sound like a human talking, NOT a press release

STRONG hook examples (study these):
  ✓ "163,000 developers are running AI locally for free — and you're not one of them yet."
  ✓ "One command. Any AI model. No API costs. That's why Ollama has 163k stars."
  ✓ "This repo just made every AI API subscription optional. 163k stars in months."
  ✓ "What if you could run GPT-4-class AI on your laptop, right now, for free?"

WEAK hooks to NEVER write:
  ✗ "Let me tell you about an amazing open source tool."
  ✗ "This GitHub repo is really popular with 163k stars."
  ✗ "Today we're looking at ollama, a tool for running AI models."

════════════════════════════════════════
FULL SCRIPT RULES
════════════════════════════════════════
- Total: 120–150 words (45–55 seconds at natural pace)
- Sentences: under 12 words each. No exceptions.
- Language: conversational, direct, zero jargon
- Every sentence must add new information — no padding
- Use concrete numbers everywhere (stars, models, milliseconds, lines of code)
- End with a punchy CTA that tells viewers exactly what to do
- features: write EXACTLY 3–4 short phrases separated by " | " — NOT sentences, just punchy labels
  Example: "Runs 50+ models locally | Zero API costs | Full REST API | Works on Mac, Linux, Windows"

OUTPUT FORMAT:
Return ONLY a JSON object with exactly these 6 keys — no markdown, no explanation:
{
  "hook": "...",
  "what_is_it": "...",
  "how_it_works": "...",
  "features": "short phrase | short phrase | short phrase | short phrase",
  "usage": "...",
  "cta": "..."
}
""").strip()


def _build_user_prompt(repo: RepoData, word_target: str = "100–150") -> str:
    features = []
    for section in repo.readme_sections:
        if section.category == "features":
            features.append(section.content_text[:300])
    features_text = "\n".join(features[:2]) if features else "See README for details."

    readme_preview = " ".join(repo.readme_full_text.split()[:500])

    return textwrap.dedent(f"""
    Write a YouTube Shorts script for this GitHub repository.
    Target word count: {word_target} words total.

    ⚡ HOOK REMINDER: The hook MUST mention "{repo.stars:,} stars" and make a bold claim.

    REPO INFO:
    - Name: {repo.name}
    - Full name: {repo.full_name}
    - Description: {repo.description}
    - Stars: {repo.stars:,}
    - Forks: {repo.forks:,}
    - Primary language: {repo.language}
    - Popularity tier: {repo.popularity_tier}
    - Topics: {", ".join(repo.topics[:8]) if repo.topics else "none"}
    - License: {repo.license or "Not specified"}
    - Install command: {repo.primary_install_command or "See README"}
    - Usage command: {repo.primary_usage_command or "See README"}

    KEY FEATURES (from README):
    {features_text}

    README EXCERPT (first 500 words):
    {readme_preview}

    Remember: output ONLY the JSON object, nothing else.
    """).strip()


# ── JSON Parsing ──────────────────────────────────────────────────────────────

def _parse_script_json(raw: str) -> dict[str, str]:
    # Strip markdown code fences if present
    text = re.sub(r"```(?:json)?\s*", "", raw).strip()
    text = text.rstrip("`").strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        # Try to extract JSON object via regex
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            data = json.loads(m.group())
        else:
            raise ValueError(f"Could not parse JSON from LLM response: {exc}") from exc
    for key in SECTION_IDS:
        if key not in data:
            raise ValueError(f"Missing key '{key}' in LLM response")
    return data


# ── Word Count Validation ─────────────────────────────────────────────────────

def _word_count(data: dict[str, str]) -> int:
    return sum(len(v.split()) for v in data.values())


# ── Fallback Template ─────────────────────────────────────────────────────────

def _template_script(repo: RepoData) -> dict[str, str]:
    features = []
    for section in repo.readme_sections:
        if section.category == "features":
            for line in section.content_text.splitlines():
                line = line.strip(" -•*")
                if len(line) > 10:
                    features.append(line)
    feature_lines = features[:4] if features else [
        "Open source & free",
        f"Built with {repo.language}" if repo.language else "Easy to use",
        "Active community",
        "Well documented",
    ]
    feature_text = " | ".join(feature_lines)

    how_text = ""
    for section in repo.readme_sections:
        if section.category in ("architecture", "other"):
            how_text = section.content_text[:200].strip()
            break
    if not how_text:
        how_text = f"It's built in {repo.language} and designed to be fast and easy to use."

    install = repo.primary_install_command or "See the README for installation."
    usage = repo.primary_usage_command or "Check the docs to get started."

    return {
        "hook": (
            f"This open-source {repo.language} tool has {repo.stars:,} stars on GitHub "
            f"— and here's why developers love it."
        ),
        "what_is_it": f"It's called {repo.name}. {repo.description}",
        "how_it_works": how_text,
        "features": feature_text,
        "usage": f"Get started with: {install} Then run: {usage}",
        "cta": "Star the repo on GitHub and try it yourself. Link in bio.",
    }


# ── Main Agent ────────────────────────────────────────────────────────────────

def write_script(repo: RepoData, config) -> VideoScript:
    """Produce a VideoScript for repo using Claude or OpenAI (required)."""
    provider = getattr(config.llm, "provider", "anthropic")

    # Verify at least one LLM key is available before attempting anything
    has_anthropic = bool(os.environ.get("ANTHROPIC_API_KEY"))
    has_openai = bool(os.environ.get("OPENAI_API_KEY"))
    if not has_anthropic and not has_openai:
        raise EnvironmentError(
            "No LLM API key found. Set ANTHROPIC_API_KEY or OPENAI_API_KEY.\n"
            "  export ANTHROPIC_API_KEY='sk-ant-...'   # for Claude\n"
            "  export OPENAI_API_KEY='sk-...'          # for GPT-4o"
        )

    data: dict[str, str] | None = None
    last_error: Exception | None = None

    # Try primary provider first, then the other one as fallback
    for attempt_provider in [provider, _other_provider(provider)]:
        try:
            client = get_llm_client(attempt_provider, config)
            logger.info("Using LLM provider: %s", attempt_provider)
        except EnvironmentError:
            logger.debug("No API key for provider '%s', skipping.", attempt_provider)
            continue

        for attempt in range(3):
            try:
                raw = client.generate(_SYSTEM_PROMPT, _build_user_prompt(repo))
                data = _parse_script_json(raw)

                wc = _word_count(data)
                if wc > 160:
                    logger.warning("Script too long (%d words), requesting shorter version.", wc)
                    raw = client.generate(_SYSTEM_PROMPT, _build_user_prompt(repo, word_target="under 130"))
                    data = _parse_script_json(raw)
                elif wc < 80:
                    logger.warning("Script too short (%d words), requesting expansion.", wc)
                    raw = client.generate(_SYSTEM_PROMPT, _build_user_prompt(repo, word_target="at least 100"))
                    data = _parse_script_json(raw)

                break  # success
            except Exception as exc:
                last_error = exc
                logger.warning("LLM attempt %d/%d failed: %s", attempt + 1, 3, exc)
                if attempt == 2:
                    data = None

        if data is not None:
            break

    if data is None:
        raise RuntimeError(
            f"Script generation failed after all LLM attempts. Last error: {last_error}\n"
            "Check your API key and network connection."
        )

    # Build VideoScript
    sections: list[ScriptSection] = []
    for sid in SECTION_IDS:
        text = data.get(sid, "").strip()
        if not text:
            continue
        sections.append(
            ScriptSection(
                id=sid,
                label=SECTION_LABELS[sid],
                text=text,
            )
        )

    full_text = " ".join(s.text for s in sections)
    total_words = len(full_text.split())

    return VideoScript(
        repo_name=repo.name,
        sections=sections,
        full_text=full_text,
        total_word_count=total_words,
        estimated_duration_seconds=round(total_words / 2.7, 2),
    )
