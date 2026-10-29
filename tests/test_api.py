"""
Unit + integration tests for the Book-to-Skill Web App backend.
Run with: pytest tests/ -v
"""

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

# Ensure the project root is on sys.path so `backend.main` imports cleanly.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from backend.main import app, _jobs, OUTPUT_DIR, _resolve_script, _safe_path

# ─────────────────────────────── Fixtures ────────────────────────────────────


@pytest.fixture
def client():
    """Synchronous test client (does not need a running server)."""
    return TestClient(app)


@pytest.fixture
def tmp_text_file(tmp_path):
    """Create a minimal plain-text 'book' file for testing."""
    book = tmp_path / "sample.txt"
    book.write_text(
        "Chapter 1\n\nThis is the first chapter.\n\nChapter 2\n\nSecond chapter content.\n",
        encoding="utf-8",
    )
    return book


@pytest.fixture(autouse=True)
def clear_jobs():
    """Reset the in-memory job store before every test."""
    _jobs.clear()
    yield
    _jobs.clear()


@pytest.fixture(autouse=True)
def mock_run_extraction(request):
    """Prevent real subprocess + LLM calls in HTTP-layer tests.

    Tests that explicitly test _run_extraction behaviour opt out via the
    ``real_extraction`` mark, or by patching _resolve_script themselves.
    """
    if request.node.get_closest_marker("real_extraction"):
        yield
        return

    # For tests that exercise the HTTP layer (not the extraction internals),
    # replace _run_extraction with a no-op coroutine so TestClient returns
    # immediately without spawning a subprocess or calling Ollama.
    async def _noop(*args, **kwargs):
        pass

    with patch("backend.main._run_extraction", side_effect=_noop):
        yield


# ─────────────────────────────── Health ──────────────────────────────────────


def test_health(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "ok"
    assert "version" in data


# ─────────────────────────────── File listing ────────────────────────────────


def test_list_files_default(client):
    """Default /api/files returns a directory and a files list."""
    r = client.get("/api/files")
    assert r.status_code == 200
    data = r.json()
    assert "directory" in data
    assert "files" in data
    assert isinstance(data["files"], list)


def test_list_files_custom_dir(client, tmp_path):
    """Files from a temp directory are correctly enumerated."""
    (tmp_path / "a.pdf").write_bytes(b"%PDF-1.4")
    (tmp_path / "b.epub").write_bytes(b"PK\x03\x04")
    (tmp_path / "ignore.xyz").write_bytes(b"data")

    r = client.get(f"/api/files?directory={tmp_path}")
    assert r.status_code == 200
    files = r.json()["files"]
    names = {f["name"] for f in files}
    assert "a.pdf" in names
    assert "b.epub" in names
    assert "ignore.xyz" not in names, "Unsupported extension should not appear"


def test_list_files_nonexistent_dir(client):
    r = client.get("/api/files?directory=/nonexistent_dir_xyz_123")
    assert r.status_code == 404


# ─────────────────────────────── Directory navigation ────────────────────────


def test_list_directories(client, tmp_path):
    """Subdirectories are listed correctly."""
    sub1 = tmp_path / "books"
    sub2 = tmp_path / "papers"
    sub1.mkdir()
    sub2.mkdir()

    r = client.get(f"/api/directories?directory={tmp_path}")
    assert r.status_code == 200
    data = r.json()
    assert data["current"] == str(tmp_path)
    names = {d["name"] for d in data["subdirectories"]}
    assert "books" in names
    assert "papers" in names


# ─────────────────────────────── Processing (mocked) ─────────────────────────


def test_process_missing_file(client):
    """A path that doesn't exist returns 400 (path not allowed) or 404."""
    r = client.post(
        "/api/process",
        json={"file_path": "/nonexistent/path/book.pdf", "mode": "text"},
    )
    assert r.status_code in (400, 404)


def test_process_invalid_mode(client, tmp_text_file):
    r = client.post(
        "/api/process",
        json={"file_path": str(tmp_text_file), "mode": "INVALID"},
    )
    assert r.status_code == 400


def test_process_creates_job(client, tmp_text_file):
    """A valid request creates a job and returns a job_id."""
    r = client.post(
        "/api/process",
        json={"file_path": str(tmp_text_file), "mode": "text"},
    )
    assert r.status_code == 200
    data = r.json()
    assert "job_id" in data
    assert data["status"] == "pending"


def test_get_job_not_found(client):
    r = client.get("/api/jobs/nonexistent-job-id")
    assert r.status_code == 404


def test_get_existing_job(client, tmp_text_file):
    """After posting a process request the job is retrievable."""
    post_r = client.post(
        "/api/process",
        json={"file_path": str(tmp_text_file), "mode": "text"},
    )
    job_id = post_r.json()["job_id"]
    r = client.get(f"/api/jobs/{job_id}")
    assert r.status_code == 200
    data = r.json()
    assert data["job_id"] == job_id
    assert data["file_path"] == str(tmp_text_file)


def test_list_jobs_empty(client):
    r = client.get("/api/jobs")
    assert r.status_code == 200
    assert r.json()["jobs"] == []


def test_list_jobs_populated(client, tmp_text_file):
    client.post("/api/process", json={"file_path": str(tmp_text_file), "mode": "text"})
    r = client.get("/api/jobs")
    assert r.status_code == 200
    assert len(r.json()["jobs"]) == 1


def test_delete_job(client, tmp_text_file):
    post_r = client.post(
        "/api/process",
        json={"file_path": str(tmp_text_file), "mode": "text"},
    )
    job_id = post_r.json()["job_id"]
    del_r = client.delete(f"/api/jobs/{job_id}")
    assert del_r.status_code == 200
    assert del_r.json()["deleted"] == job_id
    # Confirm it's gone
    assert client.get(f"/api/jobs/{job_id}").status_code == 404


# ─────────────────────────────── Output files ────────────────────────────────


def test_output_files_empty(client):
    r = client.get("/api/output/files")
    assert r.status_code == 200
    assert isinstance(r.json()["files"], list)


def test_output_download_missing(client):
    r = client.get("/api/output/download/nonexistent.txt")
    assert r.status_code == 404


# ─────────────────────────────── _safe_path ──────────────────────────────────


def test_safe_path_allows_input_dir():
    from backend.main import INPUT_DIR
    p = _safe_path(str(INPUT_DIR))
    assert p == INPUT_DIR.resolve()


def test_safe_path_allows_tmp(tmp_path):
    """Existing absolute paths outside input/output are allowed."""
    p = _safe_path(str(tmp_path))
    assert p.exists()


def test_safe_path_blocks_traversal():
    """Path traversal to /etc/passwd should raise."""
    with pytest.raises(Exception):
        _safe_path("/etc/passwd/../../../etc/shadow")


# ─────────────────────────────── Extraction integration ──────────────────────


def _make_realistic_script(workdir_env_var: str, text: str, metadata: dict) -> str:
    """Return Python source for a fake extract.py that writes real output files."""
    import json as _json
    meta_str = _json.dumps(metadata)
    text_escaped = text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return (
        "import os, json, pathlib, sys\n"
        f'workdir = pathlib.Path(os.environ.get("{workdir_env_var}", "/tmp/test_work"))\n'
        "workdir.mkdir(parents=True, exist_ok=True)\n"
        f'(workdir / "full_text.txt").write_text("{text_escaped}", encoding="utf-8")\n'
        f"(workdir / \"metadata.json\").write_text(json.dumps({metadata}), encoding=\"utf-8\")\n"
        'print("Extraction complete")\n'
        "sys.exit(0)\n"
    )


@pytest.mark.asyncio
@pytest.mark.real_extraction
async def test_run_extraction_success(tmp_text_file):
    """_run_extraction completes without error when the script succeeds."""
    import uuid
    from backend.main import _run_extraction

    job_id = str(uuid.uuid4())
    _jobs[job_id] = {
        "job_id": job_id, "status": "pending", "file_path": str(tmp_text_file),
        "mode": "text", "skill_name": None, "started_at": None,
        "finished_at": None, "stdout": "", "stderr": "", "extracted_text": None,
        "metadata": None, "error": None,
    }

    # Script that writes real output files into BOOK_SKILL_WORKDIR
    fake_meta = {"filename": "sample.txt", "chars": 10, "words": 2}
    script_src = (
        "import os, json, pathlib, sys\n"
        "workdir = pathlib.Path(os.environ['BOOK_SKILL_WORKDIR'])\n"
        "workdir.mkdir(parents=True, exist_ok=True)\n"
        "(workdir / 'full_text.txt').write_text('extracted content', encoding='utf-8')\n"
        "(workdir / 'metadata.json').write_text(json.dumps("
        + str(fake_meta) +
        "), encoding='utf-8')\n"
        "print('Extraction complete')\n"
        "sys.exit(0)\n"
    )
    with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
        f.write(script_src)
        echo_script = f.name

    try:
        with patch("backend.main._resolve_script", return_value=Path(echo_script)):
            await _run_extraction(job_id, tmp_text_file, "text", None)
    finally:
        os.unlink(echo_script)

    job = _jobs[job_id]
    assert job["status"] == "done"
    assert "Extraction complete" in job["stdout"]
    assert job["extracted_text"] == "extracted content"
    assert job["metadata"] is not None
    assert job["metadata"]["filename"] == "sample.txt"


@pytest.mark.asyncio
@pytest.mark.real_extraction
async def test_run_extraction_failure(tmp_text_file):
    """_run_extraction records error when the script exits non-zero."""
    import uuid
    from backend.main import _run_extraction

    job_id = str(uuid.uuid4())
    _jobs[job_id] = {
        "job_id": job_id, "status": "pending", "file_path": str(tmp_text_file),
        "mode": "text", "skill_name": None, "started_at": None,
        "finished_at": None, "stdout": "", "stderr": "", "extracted_text": None,
        "metadata": None, "error": None,
    }

    with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
        f.write("import sys\nprint('fail', file=sys.stderr)\nsys.exit(1)\n")
        fail_script = f.name

    try:
        with patch("backend.main._resolve_script", return_value=Path(fail_script)):
            await _run_extraction(job_id, tmp_text_file, "text", None)
    finally:
        os.unlink(fail_script)

    job = _jobs[job_id]
    assert job["status"] == "error"
    assert "fail" in job["stderr"]


@pytest.mark.asyncio
@pytest.mark.real_extraction
async def test_run_extraction_script_not_found():
    """_run_extraction handles missing script gracefully."""
    import uuid
    from backend.main import _run_extraction

    job_id = str(uuid.uuid4())
    _jobs[job_id] = {
        "job_id": job_id, "status": "pending", "file_path": "/some/file.txt",
        "mode": "text", "skill_name": None, "started_at": None,
        "finished_at": None, "stdout": "", "stderr": "", "extracted_text": None,
        "metadata": None, "error": None,
    }

    with patch("backend.main._resolve_script", side_effect=FileNotFoundError("not found")):
        await _run_extraction(job_id, Path("/some/file.txt"), "text", None)

    job = _jobs[job_id]
    assert job["status"] == "error"
    assert "not found" in job["error"]


# ─────────────────────────────── Job output endpoint ─────────────────────────


def test_job_output_not_found(client):
    """Non-existent job_id returns 404."""
    r = client.get("/api/jobs/nonexistent-id/output")
    assert r.status_code == 404


def test_job_output_returns_files(client, tmp_text_file, tmp_path, monkeypatch):
    """GET /api/jobs/{job_id}/output returns text + metadata when files exist."""
    import uuid

    monkeypatch.setattr("backend.main.OUTPUT_DIR", tmp_path)

    job_id = str(uuid.uuid4())
    job_workdir = tmp_path / job_id
    job_workdir.mkdir()
    (job_workdir / "full_text.txt").write_text("hello world", encoding="utf-8")
    (job_workdir / "metadata.json").write_text(
        '{"filename": "test.txt", "chars": 11}', encoding="utf-8"
    )

    _jobs[job_id] = {
        "job_id": job_id, "status": "done", "file_path": str(tmp_text_file),
        "mode": "text", "skill_name": None, "started_at": None,
        "finished_at": None, "stdout": "", "stderr": "",
        "extracted_text": "hello world", "metadata": {"filename": "test.txt"},
        "error": None,
    }

    r = client.get(f"/api/jobs/{job_id}/output")
    assert r.status_code == 200
    data = r.json()
    assert data["extracted_text"] == "hello world"
    assert data["metadata"]["filename"] == "test.txt"


# ─────────────────────────────── Output file listing ─────────────────────────


def test_output_files_includes_job_subdirs(client, tmp_path, monkeypatch):
    """list_output_files walks into per-job subdirectories."""
    monkeypatch.setattr("backend.main.OUTPUT_DIR", tmp_path)

    job_dir = tmp_path / "some-job-id"
    job_dir.mkdir()
    (job_dir / "full_text.txt").write_text("text", encoding="utf-8")
    (job_dir / "metadata.json").write_text("{}", encoding="utf-8")

    r = client.get("/api/output/files")
    assert r.status_code == 200
    files = r.json()["files"]
    names = {f["name"] for f in files}
    assert "full_text.txt" in names
    assert "metadata.json" in names
    # job_id should be reported
    job_files = [f for f in files if f["job_id"] == "some-job-id"]
    assert len(job_files) == 2


# ─────────────────────────────── Upload ──────────────────────────────────────


def test_upload_success(client, tmp_path, monkeypatch):
    """Uploading a valid .txt file saves it to INPUT_DIR and returns metadata."""
    from backend.main import INPUT_DIR

    # Redirect INPUT_DIR to tmp_path so we don't pollute the real input folder
    monkeypatch.setattr("backend.main.INPUT_DIR", tmp_path)

    data = {"file": ("book.txt", b"Chapter 1\nSome content.\n", "text/plain")}
    r = client.post("/api/upload", files=data)

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["name"] == "book.txt"
    assert "path" in body
    assert body["size"] == len(b"Chapter 1\nSome content.\n")
    assert (tmp_path / "book.txt").exists()


def test_upload_no_filename(client):
    """Request with an empty filename is rejected (400 from handler or 422 from validation)."""
    from io import BytesIO
    r = client.post(
        "/api/upload",
        files={"file": ("", BytesIO(b"data"), "text/plain")},
    )
    # FastAPI may raise 422 (schema validation) before reaching our handler,
    # or 400 when our guard fires — both are valid rejections.
    assert r.status_code in (400, 422)


def test_upload_unsupported_extension(client):
    """Files with disallowed extensions are rejected with 415."""
    data = {"file": ("malware.exe", b"\x4d\x5a", "application/octet-stream")}
    r = client.post("/api/upload", files=data)
    assert r.status_code == 415
    assert "Unsupported file type" in r.json()["detail"]


def test_upload_empty_file(client, tmp_path, monkeypatch):
    """Zero-byte uploads are rejected with 400."""
    monkeypatch.setattr("backend.main.INPUT_DIR", tmp_path)
    data = {"file": ("empty.pdf", b"", "application/pdf")}
    r = client.post("/api/upload", files=data)
    assert r.status_code == 400
    assert "empty" in r.json()["detail"].lower()


def test_upload_too_large(client, tmp_path, monkeypatch):
    """Files larger than MAX_UPLOAD_BYTES are rejected with 413."""
    monkeypatch.setattr("backend.main.INPUT_DIR", tmp_path)
    # Override the cap to 10 bytes for speed
    monkeypatch.setattr("backend.main.MAX_UPLOAD_BYTES", 10)
    data = {"file": ("big.txt", b"x" * 11, "text/plain")}
    r = client.post("/api/upload", files=data)
    assert r.status_code == 413


def test_upload_collision_avoidance(client, tmp_path, monkeypatch):
    """Uploading the same filename twice produces distinct saved names."""
    monkeypatch.setattr("backend.main.INPUT_DIR", tmp_path)
    payload = b"Some book content.\n"
    files = {"file": ("book.txt", payload, "text/plain")}

    r1 = client.post("/api/upload", files=files)
    assert r1.status_code == 200
    name1 = r1.json()["name"]

    files = {"file": ("book.txt", payload, "text/plain")}
    r2 = client.post("/api/upload", files=files)
    assert r2.status_code == 200
    name2 = r2.json()["name"]

    assert name1 != name2, "Second upload should not overwrite the first"
    assert (tmp_path / name1).exists()
    assert (tmp_path / name2).exists()


def test_upload_space_in_filename(client, tmp_path, monkeypatch):
    """Spaces in filenames are replaced with underscores."""
    monkeypatch.setattr("backend.main.INPUT_DIR", tmp_path)
    data = {"file": ("my book.txt", b"content", "text/plain")}
    r = client.post("/api/upload", files=data)
    assert r.status_code == 200
    assert " " not in r.json()["name"]
    assert "_" in r.json()["name"]


# ─────────────────────────────── Skill generation ────────────────────────────


def test_get_job_skill_not_found(client):
    """GET /api/jobs/{id}/skill returns 404 for unknown job."""
    r = client.get("/api/jobs/nonexistent-id/skill")
    assert r.status_code == 404


def test_get_job_skill_no_result_yet(client, tmp_text_file):
    """GET /api/jobs/{id}/skill returns skill_status=None when no generation ran."""
    post_r = client.post(
        "/api/process",
        json={"file_path": str(tmp_text_file), "mode": "text", "generate_skill": False},
    )
    job_id = post_r.json()["job_id"]
    r = client.get(f"/api/jobs/{job_id}/skill")
    assert r.status_code == 200
    data = r.json()
    assert data["job_id"] == job_id
    assert data["skill_result"] is None
    assert data["files"] == []


def test_get_job_skill_with_result(client, tmp_text_file, tmp_path, monkeypatch):
    """GET /api/jobs/{id}/skill lists generated skill files when skill_result is set."""
    import uuid
    from backend.main import _jobs

    monkeypatch.setattr("backend.main.OUTPUT_DIR", tmp_path)

    job_id = str(uuid.uuid4())
    # Create a fake skill directory with files
    skill_dir = tmp_path / job_id / "skill" / "test-skill"
    chapters_dir = skill_dir / "chapters"
    chapters_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("# Test Skill\n", encoding="utf-8")
    (skill_dir / "glossary.md").write_text("# Glossary\n", encoding="utf-8")
    (chapters_dir / "ch01-intro.md").write_text("# Chapter 1\n", encoding="utf-8")

    _jobs[job_id] = {
        "job_id": job_id, "status": "done", "file_path": str(tmp_text_file),
        "mode": "text", "skill_name": "test-skill", "generate_skill": True,
        "started_at": None, "finished_at": None, "stdout": "", "stderr": "",
        "extracted_text": None, "metadata": None, "error": None,
        "skill_status": "done",
        "skill_result": {
            "skill_name":    "test-skill",
            "skill_dir":     str(skill_dir),
            "files":         ["SKILL.md", "glossary.md", "chapters/ch01-intro.md"],
            "chapters":      1,
            "generated_at":  "2024-01-01T00:00:00+00:00",
            "llm_available": False,
            "error":         None,
        },
    }

    r = client.get(f"/api/jobs/{job_id}/skill")
    assert r.status_code == 200
    data = r.json()
    assert data["skill_status"] == "done"
    assert data["skill_result"]["skill_name"] == "test-skill"
    # Expect SKILL.md + glossary.md + ch01
    names = {f["name"] for f in data["files"]}
    assert "SKILL.md" in names
    assert "glossary.md" in names


def test_download_skill_file(client, tmp_text_file, tmp_path, monkeypatch):
    """GET /api/jobs/{id}/skill/download/{path} serves a skill file."""
    import uuid
    from backend.main import _jobs

    monkeypatch.setattr("backend.main.OUTPUT_DIR", tmp_path)

    job_id = str(uuid.uuid4())
    skill_dir = tmp_path / job_id / "skill" / "test-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("# Skill content\n", encoding="utf-8")

    _jobs[job_id] = {
        "job_id": job_id, "status": "done", "file_path": str(tmp_text_file),
        "mode": "text", "skill_name": "test-skill", "generate_skill": True,
        "started_at": None, "finished_at": None, "stdout": "", "stderr": "",
        "extracted_text": None, "metadata": None, "error": None,
        "skill_status": "done",
        "skill_result": {
            "skill_name":    "test-skill",
            "skill_dir":     str(skill_dir),
            "files":         ["SKILL.md"],
            "chapters":      0,
            "generated_at":  "2024-01-01T00:00:00+00:00",
            "llm_available": False,
            "error":         None,
        },
    }

    r = client.get(f"/api/jobs/{job_id}/skill/download/SKILL.md")
    assert r.status_code == 200


def test_download_skill_file_path_traversal(client, tmp_text_file, tmp_path, monkeypatch):
    """Path traversal in skill download is rejected."""
    import uuid
    from backend.main import _jobs

    monkeypatch.setattr("backend.main.OUTPUT_DIR", tmp_path)

    job_id = str(uuid.uuid4())
    skill_dir = tmp_path / job_id / "skill" / "test-skill"
    skill_dir.mkdir(parents=True)

    _jobs[job_id] = {
        "job_id": job_id, "status": "done", "file_path": str(tmp_text_file),
        "mode": "text", "skill_name": "test-skill", "generate_skill": True,
        "started_at": None, "finished_at": None, "stdout": "", "stderr": "",
        "extracted_text": None, "metadata": None, "error": None,
        "skill_status": "done",
        "skill_result": {
            "skill_name": "test-skill",
            "skill_dir":  str(skill_dir),
            "files": [], "chapters": 0,
            "generated_at": "2024-01-01T00:00:00+00:00",
            "llm_available": False, "error": None,
        },
    }

    r = client.get(f"/api/jobs/{job_id}/skill/download/../../etc/passwd")
    assert r.status_code in (400, 404)


def test_process_generate_skill_false(client, tmp_text_file):
    """generate_skill=False is accepted and stored in the job."""
    r = client.post(
        "/api/process",
        json={"file_path": str(tmp_text_file), "mode": "text", "generate_skill": False},
    )
    assert r.status_code == 200
    job_id = r.json()["job_id"]
    from backend.main import _jobs
    assert _jobs[job_id]["generate_skill"] is False


# ─────────────────────────────── Skill generator unit tests ──────────────────


def test_skill_generator_stub_output(tmp_path):
    """generate_skill produces stub files when LLM is unavailable."""
    from backend.skill_generator import generate_skill

    # Create fake extraction outputs
    full_text = tmp_path / "full_text.txt"
    full_text.write_text(
        "Chapter 1\n\nThis is the first chapter.\n\nChapter 2\n\nSecond chapter.\n",
        encoding="utf-8",
    )
    metadata = {
        "filename": "test-book.txt",
        "pages": 10,
        "words": 500,
        "chars": 3000,
        "estimated_tokens": 750,
        "chapters_detected": 2,
        "chapter_headings_sample": ["Chapter 1", "Chapter 2"],
    }

    import unittest.mock as mock
    with mock.patch("backend.skill_generator._llm_available", return_value=False):
        result = generate_skill(
            full_text_path=full_text,
            metadata=metadata,
            skill_name="test-book",
            output_dir=tmp_path,
        )

    assert result.skill_name == "test-book"
    assert result.error is None
    assert result.llm_available is False
    assert result.chapters >= 0

    skill_dir = tmp_path / "skill" / "test-book"
    assert (skill_dir / "SKILL.md").exists()
    assert (skill_dir / "glossary.md").exists()
    assert (skill_dir / "patterns.md").exists()
    assert (skill_dir / "cheatsheet.md").exists()


def test_skill_generator_skill_md_content(tmp_path):
    """SKILL.md contains the skill name and expected sections."""
    from backend.skill_generator import generate_skill
    import unittest.mock as mock

    full_text = tmp_path / "full_text.txt"
    full_text.write_text("Intro\n\nSome content.\n", encoding="utf-8")
    metadata = {
        "filename": "my-guide.txt",
        "pages": 5,
        "words": 200,
        "estimated_tokens": 300,
        "chapter_headings_sample": [],
    }

    with mock.patch("backend.skill_generator._llm_available", return_value=False):
        result = generate_skill(
            full_text_path=full_text,
            metadata=metadata,
            skill_name="my-guide",
            output_dir=tmp_path,
        )

    skill_md = (tmp_path / "skill" / "my-guide" / "SKILL.md").read_text(encoding="utf-8")
    assert "my-guide" in skill_md
    assert "Supporting Files" in skill_md


def test_skill_generator_to_dict(tmp_path):
    """SkillGenerationResult.to_dict() returns all expected keys."""
    from backend.skill_generator import generate_skill
    import unittest.mock as mock

    full_text = tmp_path / "full_text.txt"
    full_text.write_text("Content\n", encoding="utf-8")

    with mock.patch("backend.skill_generator._llm_available", return_value=False):
        result = generate_skill(
            full_text_path=full_text,
            metadata={},
            skill_name="dict-test",
            output_dir=tmp_path,
        )

    d = result.to_dict()
    for key in ("skill_name", "skill_dir", "files", "chapters", "generated_at", "llm_available", "error"):
        assert key in d, f"Missing key: {key}"


def test_skill_generator_missing_full_text(tmp_path):
    """generate_skill handles missing full_text.txt gracefully (produces stubs)."""
    from backend.skill_generator import generate_skill
    import unittest.mock as mock

    with mock.patch("backend.skill_generator._llm_available", return_value=False):
        result = generate_skill(
            full_text_path=tmp_path / "nonexistent.txt",
            metadata={"filename": "ghost.txt"},
            skill_name="ghost",
            output_dir=tmp_path,
        )

    assert result.error is None
    assert (tmp_path / "skill" / "ghost" / "SKILL.md").exists()


def test_slugify():
    """_slugify converts titles to clean slugs."""
    from backend.skill_generator import _slugify

    assert _slugify("Designing Data-Intensive Applications") == "designing-data-intensive-applications"
    assert _slugify("Chapter 1: The Basics!") == "chapter-1-the-basics"
    assert _slugify("") == "chapter"


def test_run_skill_generation_sets_skill_status(tmp_text_file, tmp_path):
    """_run_skill_generation updates job skill_status to done."""
    import asyncio
    import uuid
    from backend.main import _jobs
    import unittest.mock as mock

    job_id = str(uuid.uuid4())
    _jobs[job_id] = {
        "job_id": job_id, "status": "done", "file_path": str(tmp_text_file),
        "mode": "text", "skill_name": None, "generate_skill": True,
        "started_at": None, "finished_at": None, "stdout": "", "stderr": "",
        "extracted_text": None,
        "metadata": {"filename": "sample.txt", "chapter_headings_sample": []},
        "error": None, "skill_status": None, "skill_result": None,
    }

    job_workdir = tmp_path / job_id
    job_workdir.mkdir()
    (job_workdir / "full_text.txt").write_text("Chapter 1\n\nContent.\n", encoding="utf-8")

    from backend.main import _run_skill_generation

    import unittest.mock as mock
    with mock.patch("backend.skill_generator._llm_available", return_value=False):
        asyncio.run(_run_skill_generation(
            job_id=job_id,
            job_workdir=job_workdir,
            metadata=_jobs[job_id]["metadata"],
            mode="text",
            skill_name=None,
        ))

    assert _jobs[job_id]["skill_status"] == "done"
    assert _jobs[job_id]["skill_result"] is not None
    assert _jobs[job_id]["skill_result"]["skill_name"] == "sample"


# ─── _resolve_skill_dir + auto-heal tests ───────────────────────────────────


def test_resolve_skill_dir_from_skill_result(tmp_path):
    """_resolve_skill_dir returns the path stored in skill_result when it exists."""
    from backend.main import _resolve_skill_dir

    skill_dir = tmp_path / "job1" / "skill" / "my-skill"
    skill_dir.mkdir(parents=True)

    result = _resolve_skill_dir("job1", {"skill_dir": str(skill_dir)})
    assert result == skill_dir


def test_resolve_skill_dir_fallback_to_disk(tmp_path, monkeypatch):
    """_resolve_skill_dir scans output/<job_id>/skill/ when skill_result is None."""
    from backend.main import _resolve_skill_dir

    monkeypatch.setattr("backend.main.OUTPUT_DIR", tmp_path)

    job_id = "disk-fallback-job"
    skill_dir = tmp_path / job_id / "skill" / "discovered-skill"
    skill_dir.mkdir(parents=True)

    result = _resolve_skill_dir(job_id, None)
    assert result == skill_dir


def test_resolve_skill_dir_returns_none_when_no_dir(tmp_path, monkeypatch):
    """_resolve_skill_dir returns None when neither skill_result nor disk dir exists."""
    from backend.main import _resolve_skill_dir

    monkeypatch.setattr("backend.main.OUTPUT_DIR", tmp_path)

    result = _resolve_skill_dir("no-such-job", None)
    assert result is None


def test_get_job_skill_disk_scan_fallback(client, tmp_text_file, tmp_path, monkeypatch):
    """GET /api/jobs/{id}/skill returns files found on disk even when skill_result is None."""
    import uuid
    from backend.main import _jobs

    monkeypatch.setattr("backend.main.OUTPUT_DIR", tmp_path)

    job_id = str(uuid.uuid4())
    skill_dir = tmp_path / job_id / "skill" / "auto-skill"
    chapters_dir = skill_dir / "chapters"
    chapters_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("# Auto Skill\n", encoding="utf-8")
    (chapters_dir / "ch01-intro.md").write_text("# Chapter 1\n", encoding="utf-8")

    _jobs[job_id] = {
        "job_id": job_id, "status": "done", "file_path": str(tmp_text_file),
        "mode": "text", "skill_name": None, "generate_skill": True,
        "started_at": None, "finished_at": None, "stdout": "", "stderr": "",
        "extracted_text": None, "metadata": None, "error": None,
        "skill_status": "running",   # stuck — simulates server restart
        "skill_result": None,        # never set
    }

    r = client.get(f"/api/jobs/{job_id}/skill")
    assert r.status_code == 200
    data = r.json()

    # Auto-heal: status should be "done" now
    assert data["skill_status"] == "done"
    # Files discovered from disk
    names = {f["name"] for f in data["files"]}
    assert "SKILL.md" in names
    assert "ch01-intro.md" in names
    # In-memory job also healed
    assert _jobs[job_id]["skill_status"] == "done"
    # Synthesised skill_result should be populated from disk data
    assert data["skill_result"] is not None
    assert data["skill_result"]["skill_name"] == "auto-skill"
    assert data["skill_result"]["chapters"] == 1
    assert data["skill_result"]["llm_available"] is None   # unknown for recovered jobs


def test_get_job_skill_running_no_files_stays_running(client, tmp_text_file, tmp_path, monkeypatch):
    """GET /api/jobs/{id}/skill does NOT auto-heal when skill_status=running but no files yet."""
    import uuid
    from backend.main import _jobs

    monkeypatch.setattr("backend.main.OUTPUT_DIR", tmp_path)

    job_id = str(uuid.uuid4())
    # No skill dir created on disk

    _jobs[job_id] = {
        "job_id": job_id, "status": "done", "file_path": str(tmp_text_file),
        "mode": "text", "skill_name": None, "generate_skill": True,
        "started_at": None, "finished_at": None, "stdout": "", "stderr": "",
        "extracted_text": None, "metadata": None, "error": None,
        "skill_status": "running",
        "skill_result": None,
    }

    r = client.get(f"/api/jobs/{job_id}/skill")
    assert r.status_code == 200
    data = r.json()

    # Should still be "running" since no files exist
    assert data["skill_status"] == "running"
    assert data["files"] == []
    assert _jobs[job_id]["skill_status"] == "running"


def test_download_skill_file_disk_scan_fallback(client, tmp_text_file, tmp_path, monkeypatch):
    """download_skill_file resolves the skill dir via disk scan when skill_result is None."""
    import uuid
    from backend.main import _jobs

    monkeypatch.setattr("backend.main.OUTPUT_DIR", tmp_path)

    job_id = str(uuid.uuid4())
    skill_dir = tmp_path / job_id / "skill" / "recovered-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("# Recovered skill\n", encoding="utf-8")

    _jobs[job_id] = {
        "job_id": job_id, "status": "done", "file_path": str(tmp_text_file),
        "mode": "text", "skill_name": None, "generate_skill": True,
        "started_at": None, "finished_at": None, "stdout": "", "stderr": "",
        "extracted_text": None, "metadata": None, "error": None,
        "skill_status": "done",
        "skill_result": None,  # not populated (server restart scenario)
    }

    r = client.get(f"/api/jobs/{job_id}/skill/download/SKILL.md")
    assert r.status_code == 200
