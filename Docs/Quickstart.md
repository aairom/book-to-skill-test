# Quickstart Guide

## Prerequisites

| Tool | Minimum version | Install |
|------|----------------|---------|
| Python | 3.9+ | [python.org](https://python.org) |
| `uv` | any recent | `pip install uv` or [docs.astral.sh/uv](https://docs.astral.sh/uv) |
| `git` | any | system package manager |
| `poppler-utils` *(optional, PDF)* | any | `brew install poppler` / `apt install poppler-utils` |

For **LLM-powered skill generation** (optional) you need one of:

| Option | Setup |
|--------|-------|
| [Ollama](https://ollama.ai) (default) | `ollama pull llama3.2` then `ollama list` to confirm the exact tag |
| [llama.cpp](https://github.com/ggerganov/llama.cpp) | start server on port `9931` |
| Any OpenAI-compatible API | set `LLM_BASE_URL` + `LLM_API_KEY` in `.env` |

> **No LLM?** No problem — the app still extracts text and produces stub skill files.

---

## Installation

### 1. Clone the repository

```bash
git clone <this-repo-url> book-to-skill-test
cd book-to-skill-test
```

### 2. Configure environment

```bash
cp .env.example .env
# Edit .env as needed — see the LLM section below
```

Key variables in `.env`:

```bash
APP_HOST=0.0.0.0                         # bind address (127.0.0.1 for local-only)
APP_PORT=8080                            # server port (default fine)
UPLOAD_MAX_MB=100                        # max upload size in MB (0 = unlimited)
LLM_BASE_URL=http://localhost:11434/v1   # Ollama default
LLM_API_KEY=ollama                       # any string for Ollama
LLM_MODEL=llama3.2:latest               # exact tag — run `ollama list` to check
LLM_TIMEOUT=120                          # seconds per LLM request (increase for slow models)
```

### 3. Add sample files

A ready-to-use sample is already included:

```
input/sample/introduction-to-python.txt
```

Open the UI, navigate to the `sample/` subdirectory in the file browser, and select that file.

To use your own book, place any supported format in `input/`:

```bash
cp ~/Downloads/mybook.pdf input/
# or EPUB, DOCX, TXT, Markdown, etc.
```

---

## Starting the Application

```bash
./scripts/start.sh
```

This will:
1. Create a Python virtual environment in `.venv/`
2. Install all Python dependencies via `uv pip install`
3. Clone the `book-to-skill` repository to `book_to_skill_repo/` (first run only)
4. Install the library into the virtual environment
5. Launch the FastAPI server in the background
6. Print the URL to the console

Expected output:

```
⚙  Creating virtual environment…
📦 Installing / syncing dependencies…
📚 Cloning book-to-skill repository…
🚀 Starting Book-to-Skill UI…

✅ Server started successfully!

   🌐  UI:  http://localhost:8080
   📋  API: http://localhost:8080/docs
   📝  Log: /path/to/project/server.log
   🆔  PID: 12345

   To stop: ./scripts/stop.sh
```

Open **http://localhost:8080** in your browser.

---

## Using the UI

### Step 1 — Browse and select a file

The **File Browser** (left sidebar) opens in `input/` by default. You can:
- Navigate subdirectories by clicking folder entries
- Type any absolute path in the text box and press Enter
- Click any file entry to select it — its details appear in the *Process* tab

### Step 2 — Configure processing

In the **Process** tab:

| Control | Options | Notes |
|---------|---------|-------|
| **Extraction mode** | `text` (default) / `technical` | Use `technical` for books with code, tables, formulas — requires `docling` |
| **Skill name** | free text | Optional slug for the output folder (e.g. `clean-code`). Defaults to the file stem |
| **Generate skill (LLM)** | Yes (default) / No | Whether to run phase-2 skill generation after extraction |

### Step 3 — Run

Click **▶ Process**. A job is created and the UI polls for status every 1.5 s.

#### Phase 1 — Extraction
The status pill shows **⏳ running**. When complete it turns **✅ done** and the *Results* tab
populates with:
- Extracted text preview (first 2 000 chars)
- Document metadata (pages, words, chapters detected, etc.)
- Download links for `full_text.txt` and `metadata.json`

#### Phase 2 — Skill generation
If "Generate skill" is enabled, the **Generated Skill Files** card appears with a spinner.
The UI keeps polling until `skill_status` settles. When done:
- Top-level files: `SKILL.md`, `glossary.md`, `patterns.md`, `cheatsheet.md`
- Per-chapter files: `chapters/ch01-*.md`, `chapters/ch02-*.md`, …
- Each file has a **⬇ Download** link

> If no LLM is available the card shows **stub (no LLM)** and the files still download —
> they contain placeholder content you can fill in manually.

### Step 4 — History

The **History** tab lists every job from this session. Click **View** on any row to reload
that job's results (including skill files) as the active job.

---

## LLM Configuration

### Local Ollama (recommended for privacy)

```bash
# Install Ollama from https://ollama.ai, then:
ollama pull llama3.2     # ~2 GB, runs on CPU or GPU
ollama serve             # starts on :11434 (default)
```

`.env`:
```
LLM_BASE_URL=http://localhost:11434/v1
LLM_API_KEY=ollama
LLM_MODEL=llama3.2:latest
```

> **Note:** `llama3` (without a version suffix) is a legacy tag removed from newer Ollama releases.
> Run `ollama list` to see exactly which models are installed, then set `LLM_MODEL` to match.

### Local llama.cpp

Start the llama.cpp server on the configured port:

```bash
./llama-server -m /path/to/model.gguf --port 9931
```

`.env`:
```
LLM_BASE_URL=http://localhost:9931/v1
LLM_API_KEY=none
LLM_MODEL=your-model-name
```

### Remote OpenAI-compatible API

```
LLM_BASE_URL=https://api.openai.com/v1
LLM_API_KEY=sk-...
LLM_MODEL=gpt-4o-mini
```

### Increase timeout for slow models

Large models on CPU may need more than the default 120 s per request:

```
LLM_TIMEOUT=300
```

---

## Stopping the Application

```bash
./scripts/stop.sh
```

---

## Cleanup

Remove build artefacts (virtualenv, `__pycache__`, logs):

```bash
./scripts/cleanup.sh
```

To also remove the cloned library:

```bash
./scripts/cleanup.sh --full
```

---

## Running Tests

```bash
# Activate the virtual environment
source .venv/bin/activate

# Run all 51 tests
pytest tests/ -v
```

The test suite covers:
- All REST API endpoints (file browser, upload, process, jobs, output, dependencies)
- `/api/llm/status` — LLM availability probe (both `available=true` and `available=false`)
- Skill generation API endpoints (`/api/jobs/{id}/skill`, download)
- Skill generator unit tests (stub output, LLM generation, slugify, path traversal)
- `_resolve_skill_dir` disk-scan fallback and auto-heal behaviour

---

## Troubleshooting

| Problem | Solution |
|---------|----------|
| Port 8080 already in use | Edit `APP_PORT` in `.env` |
| `book-to-skill not found` | Run `./scripts/start.sh` again; it auto-clones the repo |
| PDF extraction empty | Install `poppler-utils` or `pypdf` |
| Scanned PDF returns no text | Run `ocrmypdf input.pdf output.pdf` first |
| Server log shows import error | Run `uv pip install -r requirements.txt` in `.venv` |
| LLM badge shows "LLM offline" (red) | LLM endpoint unreachable — check `LLM_BASE_URL` in `.env`; hover the badge for details |
| LLM badge shows correct URL but still red | Model name mismatch — run `ollama list` and set `LLM_MODEL` to an **exact** tag (e.g. `llama3.2:latest`) |
| LLM badge stuck on "LLM…" (orange pulse) | Backend is still probing; wait a moment or click the badge to recheck |
| Skill generation stays "running" | LLM may be unreachable; check the LLM badge in the header or disable skill gen via dropdown |
| Skill files show "stub (LLM offline)" | LLM probe failed — run `ollama list` and set `LLM_MODEL` in `.env` to an **exact** installed tag (e.g. `llama3.2:latest`, not `llama3`) |
| Skill generation very slow | Increase `LLM_TIMEOUT` in `.env`; or switch to a smaller/faster model |
| Skill files missing after server restart | Hit **Refresh skill** — the backend auto-heals `skill_status` from "running" to "done" and re-discovers files from disk |
| Skill badge shows "🔍 recovered from disk" | Normal: files were generated in a previous server session; all downloads still work |
