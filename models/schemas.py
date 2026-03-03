"""Shared Pydantic data models — the contract between all pipeline stages."""

from __future__ import annotations

from typing import Optional, Tuple
from pydantic import BaseModel, Field


# ── README Section ──────────────────────────────────────────────────────────

class CodeBlock(BaseModel):
    language: str = ""
    content: str = ""


class ReadmeSection(BaseModel):
    heading: str
    level: int  # 1, 2, or 3
    content_text: str = ""
    content_html: str = ""
    code_blocks: list[CodeBlock] = Field(default_factory=list)
    images: list[str] = Field(default_factory=list)
    category: str = "other"  # install|features|architecture|examples|api|models|other


# ── Stage 1 Output ───────────────────────────────────────────────────────────

class FileEntry(BaseModel):
    name: str
    type: str  # "file" or "dir"


class RepoData(BaseModel):
    name: str
    full_name: str          # owner/repo
    owner: str
    description: str = ""
    stars: int = 0
    forks: int = 0
    language: str = ""
    topics: list[str] = Field(default_factory=list)
    license: Optional[str] = None
    html_url: str
    default_branch: str = "main"
    popularity_tier: str = "emerging"  # viral|popular|growing|emerging
    is_recently_updated: bool = False
    primary_install_command: Optional[str] = None
    primary_usage_command: Optional[str] = None
    readme_sections: list[ReadmeSection] = Field(default_factory=list)
    readme_full_text: str = ""
    file_tree: list[FileEntry] = Field(default_factory=list)
    readme_images: list[str] = Field(default_factory=list)


# ── Stage 2 Output ───────────────────────────────────────────────────────────

SECTION_IDS = ("hook", "what_is_it", "how_it_works", "features", "usage", "cta")

SECTION_LABELS = {
    "hook": "Hook",
    "what_is_it": "What Is It?",
    "how_it_works": "How It Works",
    "features": "Key Features",
    "usage": "How to Use",
    "cta": "Call to Action",
}


class ScriptSection(BaseModel):
    id: str  # one of SECTION_IDS
    label: str
    text: str
    estimated_duration_seconds: float = 0.0

    def model_post_init(self, __context) -> None:
        if not self.estimated_duration_seconds and self.text:
            word_count = len(self.text.split())
            self.estimated_duration_seconds = round(word_count / 2.7, 2)


class VideoScript(BaseModel):
    repo_name: str
    sections: list[ScriptSection] = Field(default_factory=list)
    full_text: str = ""
    total_word_count: int = 0
    estimated_duration_seconds: float = 0.0

    def model_post_init(self, __context) -> None:
        if self.sections and not self.full_text:
            self.full_text = " ".join(s.text for s in self.sections)
        if not self.total_word_count and self.full_text:
            self.total_word_count = len(self.full_text.split())
        if not self.estimated_duration_seconds and self.total_word_count:
            self.estimated_duration_seconds = round(self.total_word_count / 2.7, 2)


# ── Stage 3 Output ───────────────────────────────────────────────────────────

class WordTimestamp(BaseModel):
    word: str
    start: float  # seconds
    end: float    # seconds


class VoiceOutput(BaseModel):
    audio_path: str
    duration_seconds: float
    word_timestamps: list[WordTimestamp] = Field(default_factory=list)
    section_times: dict[str, Tuple[float, float]] = Field(default_factory=dict)
    tts_provider: str = "edge-tts"  # "elevenlabs" or "edge-tts"


# ── Stage 4 Output ───────────────────────────────────────────────────────────

class VisualAsset(BaseModel):
    id: str
    path: str
    source_type: str  # "screenshot" | "generated_card" | "readme_image"
    mapped_section: str  # which script section ID this is for
    description: str = ""
    width: int = 1080
    height: int = 1920


class VisualPlan(BaseModel):
    assets: list[VisualAsset] = Field(default_factory=list)

    def by_section(self, section_id: str) -> list[VisualAsset]:
        return [a for a in self.assets if a.mapped_section == section_id]


# ── Stage 5 Internal ─────────────────────────────────────────────────────────

class TimelineEntry(BaseModel):
    start_time: float
    end_time: float
    visual_asset_id: str
    effect: str = "zoom_in"          # "zoom_in" | "pan_down" | "static"
    zoom_start: float = 1.0
    zoom_end: float = 1.15
