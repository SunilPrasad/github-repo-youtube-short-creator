# GitHub → YouTube Shorts Pipeline

Fully autonomous CLI tool: give it a GitHub repo URL, get back a ready-to-upload YouTube Short.

```
python main.py --url "https://github.com/ollama/ollama"
```

**Output (one folder per repo):**
```
outputs/ollama/
├── final_video.mp4     ← 9:16 vertical, 1080×1920, 30–60s
├── script.txt          ← narration script
├── voiceover.mp3       ← TTS audio
├── subtitles.srt       ← subtitle file
├── metadata.json       ← YouTube title, description, tags
├── captures/           ← all GitHub screenshots
└── cards/              ← generated visual cards
```

---

## How It Works (5-Stage Pipeline)

| Stage | Agent | What It Does |
|-------|-------|--------------|
| 1 | Repo Analyzer | Fetches repo metadata, README, file tree via GitHub API |
| 2 | Script Writer | Generates 100–150 word narration script (Claude or GPT-4o) |
| 3 | Voice Generator | Converts script to audio with word timestamps (ElevenLabs or edge-tts) |
| 4 | Visual Capture | Takes Playwright screenshots of GitHub + renders HTML cards |
| 5 | Video Composer | Assembles clips with Ken Burns effects, subtitles, and audio via FFmpeg |

---

## Setup

### 1. Install Python dependencies

```bash
pip install -r requirements.txt
playwright install chromium
```

### 2. Install FFmpeg

FFmpeg and ffprobe must be installed and on your PATH.

- **Windows:** Download from [ffmpeg.org](https://ffmpeg.org/download.html) or `winget install ffmpeg`
- **macOS:** `brew install ffmpeg`
- **Linux:** `sudo apt install ffmpeg`

Verify: `ffmpeg -version` and `ffprobe -version`

### 3. Set API keys (environment variables)

```bash
# Required — script generation (pick one or both; Claude is used by default)
export ANTHROPIC_API_KEY="sk-ant-..."
export OPENAI_API_KEY="sk-..."

# Required — audio generation
export ELEVENLABS_API_KEY="..."

# Optional but recommended (raises GitHub rate limit from 60 → 5000 req/hr)
export GITHUB_TOKEN="ghp_..."
```

On Windows (PowerShell):
```powershell
$env:ANTHROPIC_API_KEY = "sk-ant-..."
$env:ELEVENLABS_API_KEY = "..."
```

### 4. Configure voice and LLM (optional)

Edit `config.yaml`:

```yaml
llm:
  provider: "anthropic"   # or "openai"

voice:
  elevenlabs_voice_id: "21m00Tcm4TlvDq8ikWAM"
  # Find your Voice ID: ElevenLabs Dashboard → Voices → click voice → "Voice ID"
```

---

## Usage

### Single repo
```bash
python main.py --url "https://github.com/ollama/ollama"
```

### Batch mode (multiple repos)
```bash
# repos.txt — one URL per line
python main.py --urls-file repos.txt
```

### Override LLM and voice from CLI
```bash
python main.py --url "https://github.com/openai/whisper" \
               --llm openai \
               --voice-id "ErXwobaYiN019PkySvjV"
```

### Debug mode (skip video assembly)
```bash
python main.py --url "https://github.com/ggerganov/llama.cpp" --skip-video --verbose
```

### All options
```
--url URL            Single GitHub repo URL
--urls-file FILE     File with one URL per line
--output-dir DIR     Output directory (default: ./outputs)
--voice-id ID        Override ElevenLabs voice ID
--llm {anthropic,openai}  Override LLM provider
--skip-video         Skip final video (for debugging)
--verbose / -v       Debug logging
--config FILE        Config file path (default: config.yaml)
```

---

## Fallback Chain

| Component | Primary | Fallback |
|-----------|---------|----------|
| LLM | Provider in config (Claude/GPT-4o) | Other provider (if key available) |
| Word timestamps | ElevenLabs character alignment | Whisper → uniform estimate |
| Screenshots | DOM selectors | Clip-based screenshot |
| Video | zoompan + pan clips | Static slideshow |

**Both `ANTHROPIC_API_KEY` (or `OPENAI_API_KEY`) and `ELEVENLABS_API_KEY` are required.**

---

## Configuration Reference (`config.yaml`)

| Section | Key | Default | Description |
|---------|-----|---------|-------------|
| `llm` | `provider` | `anthropic` | `"anthropic"` or `"openai"` |
| `llm` | `anthropic_model` | `claude-sonnet-4-20250514` | Claude model ID |
| `llm` | `openai_model` | `gpt-4o` | OpenAI model ID |
| `voice` | `elevenlabs_voice_id` | Rachel | ElevenLabs voice ID string |
| `voice` | `elevenlabs_model_id` | `eleven_multilingual_v2` | ElevenLabs model |
| `voice` | `edge_tts_voice` | Andrew | edge-tts voice name |
| `video` | `fps` | `30` | Frames per second |
| `video` | `crf` | `18` | H.264 quality (lower=better) |
| `subtitles` | `words_per_chunk` | `5` | Words per subtitle line |
| `subtitles` | `position_from_bottom` | `200` | Pixels from bottom |

---

## Finding Your ElevenLabs Voice ID

> The voice ID is **NOT** the voice name. It's a unique string like `"21m00Tcm4TlvDq8ikWAM"`.

1. Go to [elevenlabs.io](https://elevenlabs.io) → Log in
2. Click **Voices** in the sidebar
3. Click any voice
4. Copy the **Voice ID** shown on the page

Popular built-in voices:
- `21m00Tcm4TlvDq8ikWAM` → Rachel (warm, professional)
- `ErXwobaYiN019PkySvjV` → Antoni (well-rounded)
- `EXAVITQu4vr4xnSDxMaL` → Bella (soft)
- `AZnzlk1XvdvUeBnXmlld` → Domi (strong)

Custom/cloned voices work too — same process.

---

## Tested Repos

- `https://github.com/ollama/ollama` — command-heavy, 162k+ stars
- `https://github.com/openai/whisper` — prose-heavy, clear features
- `https://github.com/ggerganov/llama.cpp` — technical, complex build

---

## Project Structure

```
├── main.py                  # CLI entry point + orchestrator
├── config.yaml              # All settings
├── requirements.txt
├── agents/
│   ├── repo_analyzer.py     # Stage 1: GitHub API → RepoData
│   ├── script_writer.py     # Stage 2: LLM → VideoScript
│   ├── voice_generator.py   # Stage 3: TTS → VoiceOutput
│   ├── visual_capture.py    # Stage 4: Screenshots + cards → VisualPlan
│   └── video_composer.py    # Stage 5: FFmpeg → MP4
├── models/
│   └── schemas.py           # All Pydantic data models
├── templates/               # HTML card templates
│   ├── title_card.html
│   ├── feature_card.html
│   ├── code_card.html
│   └── cta_card.html
└── utils/
    ├── ffmpeg_helpers.py    # FFmpeg/ffprobe wrappers
    ├── srt_generator.py     # SRT/ASS subtitle generation
    └── image_processing.py  # Pillow post-processing
```
