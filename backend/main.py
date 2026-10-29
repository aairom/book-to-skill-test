"""
Book-to-Skill Web Application Backend
FastAPI server that wraps the book-to-skill library's extraction pipeline
and triggers LLM-based skill generation after extraction completes.
"""

import asyncio
import json
import logging
import os
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from fastapi import FastAPI, HTTPException, BackgroundTasks, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# ─────────────────────────────── Helpers (time) ──────────────────────────────


def _now_iso() -> str:
    """Return current UTC time as an ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat()


# ─────────────────────────────── Logging ─────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("book_to_skill_app")

# ─────────────────────────────── Config ──────────────────────────────────────

BASE_DIR = Path(__file__).resolve().parent.parent
INPUT_DIR = BASE_DIR / "input"
OUTPUT_DIR = BASE_DIR / "output"
SKILLS_DIR   = Path(os.environ.get("SKILLS_DIR", Path.home() / ".agents" / "skills"))
_thread_pool = ThreadPoolExecutor(max_workers=4)
BOOK_TO_SKILL_REPO = BASE_DIR / "book_to_skill_repo"

# File-size cap for uploads — configurable via UPLOAD_MAX_MB in .env (0 = unlimited)
_upload_max_mb = int(os.environ.get("UPLOAD_MAX_MB", "100"))
MAX_UPLOAD_BYTES = _upload_max_mb * 1024 * 1024 if _upload_max_mb > 0 else 0

# Supported upload extensions (mirrors what book-to-skill accepts)
ALLOWED_UPLOAD_EXTENSIONS = {
    ".pdf", ".epub", ".docx", ".html", ".htm", ".rtf",
    ".mobi", ".azw", ".azw3", ".txt", ".md", ".rst", ".adoc",
}

# Ensure required directories exist
INPUT_DIR.mkdir(exist_ok=True)
OUTPUT_DIR.mkdir(exist_ok=True)

# In-memory job store  {job_id: JobResult}
_jobs: dict[str, dict] = {}

# ─────────────────────────────── Models ──────────────────────────────────────


class ProcessRequest(BaseModel):
    file_path: str
    mode: str = "text"                 # "text" | "technical"
    skill_name: Optional[str] = None
    output_dir: Optional[str] = None
    generate_skill: bool = True        # set False to skip LLM skill generation


class DirectoryListRequest(BaseModel):
    directory: str = str(INPUT_DIR)


class JobStatus(BaseModel):
    job_id: str
    status: str                  # "pending" | "running" | "done" | "error"
    file_path: str
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    stdout: str = ""
    stderr: str = ""
    extracted_text: Optional[str] = None
    metadata: Optional[dict] = None
    error: Optional[str] = None
    skill_status: Optional[str] = None   # "pending" | "running" | "done" | "error" | None
    skill_result: Optional[dict] = None  # SkillGenerationResult.to_dict()


# ─────────────────────────────── App ─────────────────────────────────────────

app = FastAPI(
    title="Book-to-Skill UI",
    description="Web interface for the book-to-skill extraction library",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Serve the single-page frontend
frontend_dir = BASE_DIR / "frontend"
if frontend_dir.exists():
    app.mount("/static", StaticFiles(directory=str(frontend_dir)), name="static")


# ─────────────────────────────── Helpers ─────────────────────────────────────

def _resolve_script() -> Path:
    """Return path to extract.py, searching the installed repo clone or pip entry."""
    candidates = [
        BOOK_TO_SKILL_REPO / "scripts" / "extract.py",
        BASE_DIR / "book_to_skill_repo" / "scripts" / "extract.py",
    ]
    for c in candidates:
        if c.exists():
            return c

    # Fall back to the pip-installed entry point
    import shutil
    ep = shutil.which("book-to-skill")
    if ep:
        return Path(ep)

    raise FileNotFoundError(
        "book-to-skill not found. Run 'uv pip install -e book_to_skill_repo' "
        "or ensure the repo is cloned to book_to_skill_repo/."
    )


def _safe_path(raw: str) -> Path:
    """Validate that raw resolves to an allowed location (no path traversal)."""
    p = Path(raw).resolve()
    allowed_roots = [INPUT_DIR.resolve(), OUTPUT_DIR.resolve(), Path.home().resolve()]
    for root in allowed_roots:
        try:
            p.relative_to(root)
            return p
        except ValueError:
            continue
    # Also allow absolute paths that exist and are files (user-specified dirs)
    if p.exists():
        return p
    raise HTTPException(status_code=400, detail=f"Path not allowed: {raw}")


async def _run_extraction(
    job_id: str,
    file_path: Path,
    mode: str,
    skill_name: Optional[str],
    generate_skill: bool = True,
) -> None:
    """Run book-to-skill extraction in a subprocess and store results.

    The library writes output to the directory specified by BOOK_SKILL_WORKDIR.
    We set that to output/<job_id>/ so every job gets an isolated, stable path
    that the UI can later serve without having to guess temp-dir names.
    """
    _jobs[job_id]["status"] = "running"
    _jobs[job_id]["started_at"] = _now_iso()

    try:
        script = _resolve_script()
    except FileNotFoundError as exc:
        _jobs[job_id]["status"] = "error"
        _jobs[job_id]["error"] = str(exc)
        _jobs[job_id]["finished_at"] = _now_iso()
        return

    # Per-job output directory — the library respects BOOK_SKILL_WORKDIR
    job_workdir = OUTPUT_DIR / job_id
    job_workdir.mkdir(parents=True, exist_ok=True)

    # NOTE: skill_name is intentionally NOT passed to the extractor CLI.
    # The book-to-skill CLI treats every positional argument as an input file
    # path; passing a skill name slug would cause "WARNING: Skipping <slug>:
    # File not found" and count as a failed input.  The skill name is only
    # consumed by our own skill_generator.py in phase 2.
    cmd: List[str] = [sys.executable, str(script), str(file_path), "--mode", mode]

    logger.info("Running: %s  (workdir=%s)", " ".join(cmd), job_workdir)

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={
                **os.environ,
                "PYTHONIOENCODING": "utf-8",
                "BOOK_SKILL_WORKDIR": str(job_workdir),
            },
        )
        stdout_bytes, stderr_bytes = await asyncio.wait_for(proc.communicate(), timeout=300)

        stdout = stdout_bytes.decode("utf-8", errors="replace")
        stderr = stderr_bytes.decode("utf-8", errors="replace")

        _jobs[job_id]["stdout"] = stdout
        _jobs[job_id]["stderr"] = stderr
        _jobs[job_id]["finished_at"] = _now_iso()

        # The library writes full_text.txt and metadata.json to job_workdir.
        # These paths are fixed by config.py (OUTPUT_TEXT / OUTPUT_META) when
        # BOOK_SKILL_WORKDIR is set, so we can reference them directly.
        meta_path = job_workdir / "metadata.json"
        text_path = job_workdir / "full_text.txt"

        if meta_path.exists():
            with open(meta_path, encoding="utf-8") as f:
                _jobs[job_id]["metadata"] = json.load(f)
        else:
            logger.warning("metadata.json not found in %s — stdout was: %s", job_workdir, stdout[:500])

        if text_path.exists():
            with open(text_path, encoding="utf-8") as f:
                _jobs[job_id]["extracted_text"] = f.read()
        else:
            logger.warning("full_text.txt not found in %s", job_workdir)

        if proc.returncode == 0:
            _jobs[job_id]["status"] = "done"
        else:
            _jobs[job_id]["status"] = "error"
            _jobs[job_id]["error"] = f"Process exited with code {proc.returncode}"
            return  # skip skill generation on extraction failure

        # ── Phase 2: LLM skill generation ────────────────────────────────────
        if generate_skill and _jobs[job_id]["status"] == "done":
            await _run_skill_generation(job_id, job_workdir, metadata=_jobs[job_id].get("metadata"), mode=mode, skill_name=skill_name)

    except asyncio.TimeoutError:
        _jobs[job_id]["status"] = "error"
        _jobs[job_id]["error"] = "Processing timed out after 300 seconds"
        _jobs[job_id]["finished_at"] = _now_iso()
    except Exception as exc:
        logger.exception("Extraction failed for %s", file_path)
        _jobs[job_id]["status"] = "error"
        _jobs[job_id]["error"] = str(exc)
        _jobs[job_id]["finished_at"] = _now_iso()


async def _run_skill_generation(
    job_id: str,
    job_workdir: Path,
    metadata: Optional[dict],
    mode: str,
    skill_name: Optional[str],
) -> None:
    """
    Run skill generation in a thread pool so we don't block the event loop.
    Updates _jobs[job_id] with skill_status and skill_result.
    """
    from backend.skill_generator import generate_skill as _gen_skill, _slugify

    _jobs[job_id]["skill_status"] = "running"

    # Derive skill name from provided slug or from source filename
    if not skill_name:
        fname = (metadata or {}).get("filename", "")
        if fname:
            skill_name = _slugify(Path(fname).stem)
        else:
            skill_name = job_id[:8]

    full_text_path = job_workdir / "full_text.txt"
    book_type = "technical" if mode == "technical" else "text"

    loop = asyncio.get_event_loop()
    try:
        skill_result = await loop.run_in_executor(
            _thread_pool,
            lambda: _gen_skill(
                full_text_path=full_text_path,
                metadata=metadata or {},
                skill_name=skill_name,
                output_dir=job_workdir,
                book_type=book_type,
            ),
        )
        _jobs[job_id]["skill_status"] = "done"
        _jobs[job_id]["skill_result"] = skill_result.to_dict()
        logger.info("Skill generation complete for job %s: %s", job_id, skill_name)
    except Exception as exc:
        logger.exception("Skill generation failed for job %s", job_id)
        _jobs[job_id]["skill_status"] = "error"
        _jobs[job_id]["skill_result"] = {"error": str(exc), "skill_name": skill_name}


# ─────────────────────────────── Routes ──────────────────────────────────────


@app.get("/")
async def index():
    """Serve the frontend SPA."""
    html_file = frontend_dir / "index.html"
    if html_file.exists():
        return FileResponse(str(html_file))
    return {"message": "Book-to-Skill API is running. Frontend not found."}


@app.get("/api/health")
async def health():
    """Health check endpoint."""
    return {"status": "ok", "version": "1.0.0"}


@app.get("/api/llm/status")
async def llm_status():
    """Probe the configured LLM endpoint and return availability.

    Runs the blocking probe in the thread pool so the event loop stays free.
    LLM config is read fresh from .env on every call via _cfg(), so changes
    to .env are reflected immediately without restarting the server.

    Response fields:
      available  – true | false
      model      – current LLM_MODEL value
      base_url   – current LLM_BASE_URL value (API key not included)
      checked_at – ISO-8601 UTC timestamp of this probe
    """
    from backend.skill_generator import _llm_available, _cfg

    loop = asyncio.get_event_loop()
    available = await loop.run_in_executor(_thread_pool, _llm_available)
    cfg = _cfg()   # read live values after the probe so they match what was used
    return {
        "available":  available,
        "model":      cfg.model,
        "base_url":   cfg.base_url,
        "checked_at": _now_iso(),
    }


@app.get("/api/dependencies")
async def check_dependencies():
    """Run book-to-skill --check to report installed extractors."""
    try:
        script = _resolve_script()
    except FileNotFoundError as exc:
        raise HTTPException(status_code=503, detail=str(exc))

    proc = await asyncio.create_subprocess_exec(
        sys.executable, str(script), "--check",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    out, err = await proc.communicate()
    return {
        "stdout": out.decode("utf-8", errors="replace"),
        "stderr": err.decode("utf-8", errors="replace"),
        "available": proc.returncode == 0,
    }


@app.get("/api/files")
async def list_files(directory: str = str(INPUT_DIR)):
    """List supported book files in a directory."""
    try:
        dir_path = _safe_path(directory)
    except HTTPException:
        dir_path = Path(directory).resolve()
        if not dir_path.exists():
            raise HTTPException(status_code=404, detail=f"Directory not found: {directory}")

    if not dir_path.is_dir():
        raise HTTPException(status_code=400, detail="Path is not a directory")

    supported = {".pdf", ".epub", ".docx", ".html", ".htm", ".rtf", ".mobi",
                 ".azw", ".azw3", ".txt", ".md", ".rst", ".adoc"}
    files = []
    for p in sorted(dir_path.iterdir()):
        if p.is_file() and p.suffix.lower() in supported:
            files.append({
                "name": p.name,
                "path": str(p),
                "size": p.stat().st_size,
                "extension": p.suffix.lower(),
                "modified": datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc).isoformat(),
            })
    return {"directory": str(dir_path), "files": files}


@app.get("/api/directories")
async def list_subdirectories(directory: str = str(INPUT_DIR)):
    """List subdirectories for navigation."""
    try:
        dir_path = _safe_path(directory)
    except HTTPException:
        dir_path = Path(directory).resolve()

    if not dir_path.exists():
        raise HTTPException(status_code=404, detail="Directory not found")

    dirs = []
    try:
        for p in sorted(dir_path.iterdir()):
            if p.is_dir() and not p.name.startswith("."):
                dirs.append({"name": p.name, "path": str(p)})
    except PermissionError:
        pass

    parent = str(dir_path.parent) if dir_path != dir_path.parent else None
    return {
        "current": str(dir_path),
        "parent": parent,
        "subdirectories": dirs,
    }


@app.post("/api/process")
async def process_file(req: ProcessRequest, background_tasks: BackgroundTasks):
    """Start an async extraction + skill-generation job for a book file."""
    file_path = _safe_path(req.file_path)
    if not file_path.exists():
        raise HTTPException(status_code=404, detail=f"File not found: {req.file_path}")
    if not file_path.is_file():
        raise HTTPException(status_code=400, detail="Path is not a file")

    if req.mode not in ("text", "technical"):
        raise HTTPException(status_code=400, detail="mode must be 'text' or 'technical'")

    job_id = str(uuid.uuid4())
    _jobs[job_id] = {
        "job_id":        job_id,
        "status":        "pending",
        "file_path":     str(file_path),
        "mode":          req.mode,
        "skill_name":    req.skill_name,
        "generate_skill": req.generate_skill,
        "started_at":    None,
        "finished_at":   None,
        "stdout":        "",
        "stderr":        "",
        "extracted_text": None,
        "metadata":      None,
        "error":         None,
        "skill_status":  None,
        "skill_result":  None,
    }

    background_tasks.add_task(
        _run_extraction,
        job_id, file_path, req.mode, req.skill_name, req.generate_skill,
    )
    return {"job_id": job_id, "status": "pending"}


@app.get("/api/jobs/{job_id}")
async def get_job(job_id: str):
    """Poll a processing job by ID."""
    job = _jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@app.get("/api/jobs")
async def list_jobs():
    """List all known jobs (most recent first)."""
    jobs = sorted(_jobs.values(), key=lambda j: j.get("started_at") or "", reverse=True)
    return {"jobs": jobs}


@app.delete("/api/jobs/{job_id}")
async def delete_job(job_id: str):
    """Remove a completed job from the in-memory store."""
    if job_id not in _jobs:
        raise HTTPException(status_code=404, detail="Job not found")
    del _jobs[job_id]
    return {"deleted": job_id}


@app.get("/api/output/files")
async def list_output_files():
    """List files produced in the output directory (including per-job subdirs)."""
    files = []
    if OUTPUT_DIR.exists():
        # Walk both top-level files and one level of per-job subdirectories
        for p in sorted(OUTPUT_DIR.iterdir()):
            if p.is_file():
                files.append({
                    "name": p.name,
                    "path": str(p),
                    "size": p.stat().st_size,
                    "modified": datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc).isoformat(),
                    "job_id": None,
                })
            elif p.is_dir():
                for f in sorted(p.iterdir()):
                    if f.is_file():
                        files.append({
                            "name": f.name,
                            "path": str(f),
                            "size": f.stat().st_size,
                            "modified": datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc).isoformat(),
                            "job_id": p.name,
                        })
    return {"files": files}


def _resolve_skill_dir(job_id: str, skill_result: Optional[dict]) -> Optional[Path]:
    """
    Find the skill output directory for a job.

    Priority:
    1. skill_result["skill_dir"] (set when generation completes normally)
    2. First subdirectory found under output/<job_id>/skill/ (disk scan fallback,
       needed when the server restarted mid-generation or skill_result was never saved)
    """
    if skill_result and skill_result.get("skill_dir"):
        candidate = Path(skill_result["skill_dir"])
        if candidate.exists():
            return candidate

    # Fallback: scan output/<job_id>/skill/ for any subdirectory
    fallback_root = OUTPUT_DIR / job_id / "skill"
    if fallback_root.is_dir():
        for child in sorted(fallback_root.iterdir()):
            if child.is_dir():
                return child

    return None


def _list_skill_files(skill_dir: Path) -> List[dict]:
    """Return a sorted list of .md file descriptors relative to *skill_dir*."""
    files: List[dict] = []
    for p in sorted(skill_dir.rglob("*.md")):
        try:
            rel = p.relative_to(skill_dir)
            files.append({
                "name":     p.name,
                "rel_path": str(rel),
                "size":     p.stat().st_size,
            })
        except ValueError:
            pass
    return files


@app.get("/api/jobs/{job_id}/skill")
async def get_job_skill(job_id: str):
    """Return the skill generation result and list of generated skill files."""
    job = _jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    skill_result = job.get("skill_result")
    skill_status = job.get("skill_status")

    # Locate skill directory — from skill_result or disk scan
    skill_dir = _resolve_skill_dir(job_id, skill_result)

    # Auto-heal: if status is stuck at "running" but files already exist on disk,
    # the background task must have completed (or the server was restarted).
    # Flip the status to "done" so the UI unblocks.
    if skill_status == "running" and skill_dir is not None:
        md_files = _list_skill_files(skill_dir)
        if md_files:
            skill_status = "done"
            _jobs[job_id]["skill_status"] = "done"
            logger.info(
                "Auto-healed skill_status for job %s — found %d files in %s",
                job_id, len(md_files), skill_dir,
            )

    files: List[dict] = _list_skill_files(skill_dir) if skill_dir else []

    # When skill_result is null (disk-scan fallback), synthesise a minimal result
    # so the frontend badge can show chapter count and skill name.
    if skill_result is None and files:
        chapter_count = sum(1 for f in files if f["rel_path"].startswith("chapters/"))
        skill_name = skill_dir.name if skill_dir else None
        skill_result = {
            "skill_name":    skill_name,
            "skill_dir":     str(skill_dir) if skill_dir else None,
            "files":         [f["rel_path"] for f in files],
            "chapters":      chapter_count,
            "generated_at":  None,
            "llm_available": None,   # unknown — was generated before this session
            "error":         None,
        }

    return {
        "job_id":       job_id,
        "skill_status": skill_status,
        "skill_result": skill_result,
        "files":        files,
    }


@app.get("/api/jobs/{job_id}/output")
async def get_job_output(job_id: str):
    """Return the extracted text and metadata for a completed job."""
    job = _jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    job_workdir = OUTPUT_DIR / job_id
    result: dict = {"job_id": job_id, "workdir": str(job_workdir)}

    meta_path = job_workdir / "metadata.json"
    text_path = job_workdir / "full_text.txt"

    if meta_path.exists():
        with open(meta_path, encoding="utf-8") as f:
            result["metadata"] = json.load(f)

    if text_path.exists():
        with open(text_path, encoding="utf-8") as f:
            result["extracted_text"] = f.read()

    return result


@app.get("/api/jobs/{job_id}/skill/download/{file_path:path}")
async def download_skill_file(job_id: str, file_path: str):
    """Download a generated skill file by relative path inside the skill dir."""
    job = _jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    skill_dir = _resolve_skill_dir(job_id, job.get("skill_result"))
    if skill_dir is None:
        raise HTTPException(status_code=404, detail="No skill generated for this job")

    # Resolve safely — no traversal outside skill_dir
    target = (skill_dir / file_path).resolve()
    if not str(target).startswith(str(skill_dir.resolve())):
        raise HTTPException(status_code=400, detail="Path not allowed")
    if not target.exists():
        raise HTTPException(status_code=404, detail="File not found")

    return FileResponse(str(target), filename=target.name)


@app.get("/api/output/download/{job_id}/{filename}")
async def download_job_output(job_id: str, filename: str):
    """Download a file from a per-job output subdirectory."""
    path = (OUTPUT_DIR / job_id / filename).resolve()
    output_root = OUTPUT_DIR.resolve()
    if not path.exists() or not str(path).startswith(str(output_root)):
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(str(path), filename=filename)


@app.get("/api/output/download/{filename}")
async def download_output(filename: str):
    """Download a file from the top-level output directory."""
    path = (OUTPUT_DIR / filename).resolve()
    if not path.exists() or not str(path).startswith(str(OUTPUT_DIR.resolve())):
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(str(path), filename=filename)


# ─────────────────────────────── Upload ──────────────────────────────────────


@app.post("/api/upload")
async def upload_file(file: UploadFile = File(...)):
    """Upload a book file into the input/ directory.

    Validates extension and enforces a 100 MB size cap before writing.
    Returns the saved file path so the caller can immediately select it.
    """
    if not file.filename:
        raise HTTPException(status_code=400, detail="No filename provided")

    ext = Path(file.filename).suffix.lower()
    if ext not in ALLOWED_UPLOAD_EXTENSIONS:
        raise HTTPException(
            status_code=415,
            detail=(
                f"Unsupported file type '{ext}'. "
                f"Allowed: {', '.join(sorted(ALLOWED_UPLOAD_EXTENSIONS))}"
            ),
        )

    # Stream into a temp buffer to measure size before committing to disk
    content = await file.read()
    if MAX_UPLOAD_BYTES > 0 and len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"File exceeds the {_upload_max_mb} MB upload limit (set UPLOAD_MAX_MB=0 to disable)",
        )
    if len(content) == 0:
        raise HTTPException(status_code=400, detail="Uploaded file is empty")

    # Sanitise the filename: keep only the base name, replace spaces
    safe_name = Path(file.filename).name.replace(" ", "_")
    dest = INPUT_DIR / safe_name

    # Avoid silent overwrites — append a counter suffix if the name is taken
    if dest.exists():
        stem = dest.stem
        suffix = dest.suffix
        counter = 1
        while dest.exists():
            dest = INPUT_DIR / f"{stem}_{counter}{suffix}"
            counter += 1

    dest.write_bytes(content)
    logger.info("Uploaded %s (%d bytes) → %s", file.filename, len(content), dest)

    return {
        "name": dest.name,
        "path": str(dest),
        "size": len(content),
        "directory": str(INPUT_DIR),
    }
