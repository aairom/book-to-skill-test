"""
Skill Generator
===============
Reads extraction output (full_text.txt + metadata.json) produced by the
book-to-skill extractor and calls an OpenAI-compatible LLM to generate a
structured agent skill following the SKILL.md spec:

  output/<job_id>/skill/<skill_name>/
    SKILL.md          – master index + core frameworks (~4 000 tokens)
    glossary.md       – alphabetical key-term definitions
    patterns.md       – techniques & design patterns
    cheatsheet.md     – decision rules & quick-reference tables
    chapters/
      ch01-<slug>.md  – per-chapter summary
      ...

The generator degrades gracefully when no LLM is configured: it produces
stub files so the application always has something to show.

Environment variables (all optional — defaults work with Ollama):
  LLM_BASE_URL  – OpenAI-compatible base URL (default: http://localhost:11434/v1)
  LLM_API_KEY   – API key (default: "ollama" — Ollama ignores it)
  LLM_MODEL     – Model name (default: llama3.2:latest).
                  Must exactly match a model installed in Ollama (use
                  `ollama list` to check). Legacy alias "llama3" was removed
                  from Ollama; the current default is "llama3.2:latest".
  LLM_TIMEOUT   – Per-request timeout in seconds (default: 120)
"""

from __future__ import annotations

import json
import logging
import os
import re
import textwrap
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Tuple

logger = logging.getLogger("book_to_skill_app.skill_generator")

# ─────────────────────────────── LLM config ───────────────────────────────────
# Values are read lazily on every call via _cfg() so that editing .env while
# the server is running takes effect immediately — no restart required.

from dataclasses import dataclass as _dataclass  # local alias avoids shadowing

@_dataclass(frozen=True)
class _LLMConfig:
    base_url: str
    api_key:  str
    model:    str
    timeout:  int


def _cfg() -> _LLMConfig:
    """Return the current LLM configuration, re-reading .env on every call.

    Uses python-dotenv with override=True so values written to .env after
    server startup are immediately visible to the next request without a
    restart.  The .env file is located relative to this file's project root.
    """
    from dotenv import load_dotenv
    from pathlib import Path as _Path
    _env_path = _Path(__file__).resolve().parent.parent / ".env"
    load_dotenv(_env_path, override=True)
    return _LLMConfig(
        base_url = os.environ.get("LLM_BASE_URL", "http://localhost:11434/v1"),
        api_key  = os.environ.get("LLM_API_KEY",  "ollama"),
        model    = os.environ.get("LLM_MODEL",    "llama3.2:latest"),
        timeout  = int(os.environ.get("LLM_TIMEOUT", "120")),
    )

# ─────────────────────────────── Result dataclass ─────────────────────────────


@dataclass
class SkillGenerationResult:
    skill_name: str
    skill_dir: Path
    files: List[str] = field(default_factory=list)   # relative paths inside skill_dir
    chapters: int = 0
    generated_at: str = ""
    llm_available: bool = False
    error: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "skill_name":    self.skill_name,
            "skill_dir":     str(self.skill_dir),
            "files":         self.files,
            "chapters":      self.chapters,
            "generated_at":  self.generated_at,
            "llm_available": self.llm_available,
            "error":         self.error,
        }


# ─────────────────────────────── LLM helpers ──────────────────────────────────


def _llm_available() -> bool:
    """Quick probe: returns True when the LLM endpoint answers a tiny request.

    Two-step strategy
    -----------------
    1. GET /models — confirms the server is reachable without requiring a
       specific model name.  If this fails the endpoint is down → False.
    2. POST /chat/completions with max_tokens=1 — confirms the configured
       model is loaded.  An HTTP 404 ("model not found") means the server is
       running but LLM_MODEL is wrong; we log a clear warning rather than
       silently returning False.
    """
    import urllib.request
    import urllib.error

    cfg  = _cfg()
    base = cfg.base_url.rstrip("/")

    # ── Step 1: confirm the server is up at all ────────────────────────────
    try:
        models_req = urllib.request.Request(
            f"{base}/models",
            headers={"Authorization": f"Bearer {cfg.api_key}"},
            method="GET",
        )
        with urllib.request.urlopen(models_req, timeout=8):
            pass  # 200 → server is up
    except Exception as exc:
        logger.debug("LLM server not reachable (%s): %s", base, exc)
        return False

    # ── Step 2: confirm the configured model exists ────────────────────────
    try:
        payload = json.dumps({
            "model":      cfg.model,
            "messages":   [{"role": "user", "content": "ping"}],
            "max_tokens": 1,
        }).encode()
        req = urllib.request.Request(
            f"{base}/chat/completions",
            data=payload,
            headers={
                "Content-Type":  "application/json",
                "Authorization": f"Bearer {cfg.api_key}",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=8):
            return True
    except urllib.error.HTTPError as exc:
        body = exc.read(512).decode(errors="replace")
        if exc.code == 404:
            logger.warning(
                "LLM server is reachable but model %r was not found "
                "(HTTP 404). Check LLM_MODEL in .env.  Server said: %s",
                cfg.model, body,
            )
        else:
            logger.debug("LLM chat probe HTTP %d: %s", exc.code, body)
        return False
    except Exception as exc:
        logger.debug("LLM chat probe failed: %s", exc)
        return False


def _chat(system: str, user: str, max_tokens: int = 2000) -> str:
    """
    Call the LLM chat endpoint.  Returns the assistant's reply text, or raises
    RuntimeError when the call fails.
    """
    import urllib.request
    import urllib.error

    cfg = _cfg()
    payload = json.dumps({
        "model": cfg.model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user",   "content": user},
        ],
        "max_tokens": max_tokens,
        "temperature": 0.3,
    }).encode()

    req = urllib.request.Request(
        f"{cfg.base_url.rstrip('/')}/chat/completions",
        data=payload,
        headers={
            "Content-Type":  "application/json",
            "Authorization": f"Bearer {cfg.api_key}",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=cfg.timeout) as resp:
        body = json.load(resp)

    try:
        return body["choices"][0]["message"]["content"]
    except (KeyError, IndexError) as exc:
        raise RuntimeError(f"Unexpected LLM response shape: {body}") from exc


# ─────────────────────────────── Text utilities ───────────────────────────────


def _slugify(text: str) -> str:
    """Convert a title string to a lowercase hyphen slug."""
    slug = re.sub(r"[^\w\s-]", "", text.lower())
    slug = re.sub(r"[\s_]+", "-", slug)
    slug = re.sub(r"-+", "-", slug).strip("-")
    return slug[:60] or "chapter"


def _read_chunk(full_text_path: Path, start_line: int, end_line: int) -> str:
    """Read a line range from full_text.txt without loading the whole file."""
    lines: List[str] = []
    with open(full_text_path, encoding="utf-8", errors="replace") as fh:
        for i, line in enumerate(fh, 1):
            if i < start_line:
                continue
            if i > end_line:
                break
            lines.append(line)
    return "".join(lines)


def _find_chapter_offsets(full_text_path: Path) -> List[Tuple[int, str]]:
    """
    Scan full_text.txt for chapter headings, return list of (line_number, heading).
    Caps at 30 chapters to avoid pathological cases.
    """
    pattern = re.compile(
        r"^\s*(Chapter|CHAPTER|PART|Part)\s+(\w+)|^#{1,2}\s+\S",
        re.MULTILINE,
    )
    offsets: List[Tuple[int, str]] = []
    with open(full_text_path, encoding="utf-8", errors="replace") as fh:
        for i, line in enumerate(fh, 1):
            if pattern.match(line):
                offsets.append((i, line.strip()))
                if len(offsets) >= 30:
                    break
    return offsets


def _total_lines(path: Path) -> int:
    count = 0
    with open(path, encoding="utf-8", errors="replace") as fh:
        for _ in fh:
            count += 1
    return count


# ─────────────────────────────── Stub generators (no LLM) ────────────────────


def _stub_chapter(n: int, heading: str, _slug: str) -> str:
    return textwrap.dedent(f"""\
        # Chapter {n}: {heading}

        ## Core Idea
        *(LLM not available — run with a configured LLM to generate summaries)*

        ## Key Takeaways
        1. See full_text.txt for extracted chapter content.

        ## Connects To
        - *(generate with LLM for cross-chapter links)*
    """)


def _stub_glossary(meta: dict) -> str:
    sample = meta.get("chapter_headings_sample") or []
    terms = "\n".join(f"**{h}** — *(see full text)*" for h in sample[:10])
    return textwrap.dedent(f"""\
        # Glossary

        *(Generated without LLM — start the LLM and re-process for full definitions)*

        {terms or "*(no terms detected)*"}
    """)


def _stub_patterns(meta: dict) -> str:
    return textwrap.dedent("""\
        # Patterns & Techniques

        *(Generated without LLM — start the LLM and re-process for full patterns)*

        ## Pattern Template
        **When to use**: ...
        **How**: ...
        **Trade-offs**: ...
    """)


def _stub_cheatsheet(meta: dict) -> str:
    fname = meta.get("filename", "unknown")
    return textwrap.dedent(f"""\
        # Cheatsheet — {fname}

        *(Generated without LLM — start the LLM and re-process for decision rules)*

        | Situation | Action |
        |-----------|--------|
        | *(see chapters)* | *(see chapters)* |
    """)


def _stub_skill_md(skill_name: str, meta: dict, chapter_files: List[str]) -> str:
    fname   = meta.get("filename", "unknown")
    pages   = meta.get("pages", "?")
    words   = meta.get("words", "?")
    tokens  = meta.get("estimated_tokens", 0)
    today   = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    ch_rows = "\n".join(
        f"| [{Path(f).stem}]({f}) | *(see file)* | *(see file)* |"
        for f in chapter_files
    )

    return textwrap.dedent(f"""\
        ---
        name: {skill_name}
        description: "Knowledge base from \\"{fname}\\". Use when applying its frameworks or referencing its concepts."
        ---

        # {fname}
        **Pages**: ~{pages} | **Words**: ~{words} | **Tokens**: ~{tokens} | **Generated**: {today}

        ## How to Use This Skill

        - **Without arguments** — load core frameworks for reference
        - **With a topic** — ask about a concept; the agent finds the relevant chapter
        - **With chapter** — ask for `ch01` to dive into a specific chapter

        *(LLM not available — core frameworks section requires a configured LLM)*

        ---

        ## Chapter Index

        | File | Title | Key Frameworks |
        |------|-------|----------------|
        {ch_rows or "| *(none)* | | |"}

        ## Supporting Files

        - [glossary.md](glossary.md) — key terms
        - [patterns.md](patterns.md) — techniques & patterns
        - [cheatsheet.md](cheatsheet.md) — quick reference

        ---

        ## Scope & Limits

        This skill covers the extracted book content only.
    """)


# ─────────────────────────────── LLM generators ───────────────────────────────

_SYSTEM_EXTRACT = textwrap.dedent("""\
    You are a precise knowledge extractor.  Given a chapter excerpt from a book,
    produce a structured markdown summary strictly following the template provided
    by the user.  Be concise, preserve the author's exact framework names, and
    never invent content not present in the excerpt.
""")

_SYSTEM_SKILL = textwrap.dedent("""\
    You are a knowledge synthesiser.  Given book metadata and the contents of
    chapter summary files, generate a master SKILL.md following the template
    provided by the user.  Keep the body under 4 000 tokens, front-load the most
    important frameworks, and preserve exact names.
""")


def _llm_chapter(n: int, heading: str, excerpt: str, book_type: str) -> str:
    template_hint = (
        "Include: Core Idea, Frameworks Introduced, Key Concepts, Mental Models, "
        "Anti-patterns, Key Takeaways, Connects To."
    )
    if book_type == "technical":
        template_hint += " Also include Code Examples and Reference Tables sections if relevant."

    user = textwrap.dedent(f"""\
        Book chapter excerpt (Chapter {n}: {heading}):
        ---
        {excerpt[:6000]}
        ---
        Generate a chapter summary in markdown following this structure:
        # Chapter {n}: {heading}
        {template_hint}
        Keep the summary under 1 800 tokens.
    """)
    return _chat(_SYSTEM_EXTRACT, user, max_tokens=1800)


def _llm_glossary(full_text_excerpt: str, skill_name: str) -> str:
    user = textwrap.dedent(f"""\
        Source text excerpt:
        ---
        {full_text_excerpt[:8000]}
        ---
        Extract all significant technical/conceptual terms from this book and produce a
        glossary in markdown.  Format each entry as:
        **Term** — one-sentence definition (Ch N if applicable)
        Sort alphabetically.  Keep total under 1 500 tokens.
        Start with: # Glossary — {skill_name}
    """)
    return _chat(_SYSTEM_EXTRACT, user, max_tokens=1500)


def _llm_patterns(full_text_excerpt: str, skill_name: str) -> str:
    user = textwrap.dedent(f"""\
        Source text excerpt:
        ---
        {full_text_excerpt[:8000]}
        ---
        Identify all concrete techniques, design patterns, algorithms, and methods from this
        book.  Format each as:
        ## Pattern Name
        **When to use**: ...
        **How**: ...
        **Trade-offs**: ...
        Keep total under 2 000 tokens.
        Start with: # Patterns & Techniques — {skill_name}
    """)
    return _chat(_SYSTEM_EXTRACT, user, max_tokens=2000)


def _llm_cheatsheet(full_text_excerpt: str, skill_name: str) -> str:
    user = textwrap.dedent(f"""\
        Source text excerpt:
        ---
        {full_text_excerpt[:8000]}
        ---
        Create a cheatsheet of decision rules, trade-off matrices, thresholds, and
        quick heuristics from this book.  Prioritise "When X do Y because Z" rules and
        compact tables.  Keep total under 1 200 tokens.
        Start with: # Cheatsheet — {skill_name}
    """)
    return _chat(_SYSTEM_EXTRACT, user, max_tokens=1200)


def _llm_skill_md(skill_name: str, meta: dict, chapter_summaries: List[Tuple[str, str]]) -> str:
    fname   = meta.get("filename", "unknown")
    pages   = meta.get("pages", "?")
    words   = meta.get("words", "?")
    today   = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # Condense chapter summaries for the prompt
    ch_context = "\n\n".join(
        f"=== {Path(fname_).stem} ===\n{summary[:1200]}"
        for fname_, summary in chapter_summaries
    )[:10000]

    ch_rows = "\n".join(
        f"| [{Path(f).stem}](chapters/{Path(f).name}) | *(see file)* | *(see file)* |"
        for f, _ in chapter_summaries
    )

    user = textwrap.dedent(f"""\
        Book: {fname}  |  Pages: {pages}  |  Words: {words}  |  Date: {today}
        Skill slug: {skill_name}

        Chapter summaries:
        {ch_context}

        Generate a master SKILL.md with these sections:
        1. YAML frontmatter (name, description)
        2. # Book Title  with metadata line
        3. ## How to Use This Skill (3-4 bullet points)
        4. ## Core Frameworks & Mental Models (~2 000 tokens of the most critical frameworks)
        5. ## Chapter Index (table with links, use these rows as a template):
        {ch_rows or "| ch01 | Chapter 1 | framework1 |"}
        6. ## Topic Index (alphabetical, major terms → chapter numbers)
        7. ## Supporting Files (glossary, patterns, cheatsheet)
        8. ## Scope & Limits

        Keep body under 4 000 tokens.  Preserve exact framework names.
    """)
    return _chat(_SYSTEM_SKILL, user, max_tokens=4000)


# ─────────────────────────────── Main entry point ─────────────────────────────


def generate_skill(
    full_text_path: Path,
    metadata: dict,
    skill_name: str,
    output_dir: Path,
    book_type: str = "text",
) -> SkillGenerationResult:
    """
    Generate a structured skill from extraction outputs.

    Parameters
    ----------
    full_text_path : Path
        Path to the extracted full_text.txt.
    metadata : dict
        Parsed metadata.json dict.
    skill_name : str
        Slug for the skill (e.g. "my-book").
    output_dir : Path
        Root for skill output; writes to output_dir/skill/<skill_name>/.
    book_type : str
        "text" | "technical" — controls which SKILL.md sections are generated.

    Returns
    -------
    SkillGenerationResult
    """
    result = SkillGenerationResult(
        skill_name=skill_name,
        skill_dir=output_dir / "skill" / skill_name,
        generated_at=datetime.now(timezone.utc).isoformat(),
    )

    skill_dir = result.skill_dir
    chapters_dir = skill_dir / "chapters"
    skill_dir.mkdir(parents=True, exist_ok=True)
    chapters_dir.mkdir(exist_ok=True)

    # ── Probe LLM ────────────────────────────────────────────────────────────
    result.llm_available = _llm_available()
    logger.info("Skill generator: LLM available = %s (model=%s)", result.llm_available, _cfg().model)

    # ── Identify chapters ────────────────────────────────────────────────────
    headings_sample = metadata.get("chapter_headings_sample") or []
    chapter_offsets  = _find_chapter_offsets(full_text_path) if full_text_path.exists() else []

    # Merge: prefer offsets from file scan, fall back to metadata headings
    if not chapter_offsets and headings_sample:
        chapter_offsets = [(0, h) for h in headings_sample[:20]]

    total_lines = _total_lines(full_text_path) if full_text_path.exists() else 0

    # ── Generate chapter files ───────────────────────────────────────────────
    chapter_files: List[str] = []
    chapter_summaries: List[Tuple[str, str]] = []  # (relative_path, content)

    for idx, (start_ln, heading) in enumerate(chapter_offsets, 1):
        slug       = _slugify(heading)
        ch_filename = f"ch{idx:02d}-{slug}.md"
        ch_path    = chapters_dir / ch_filename
        rel_path   = f"chapters/{ch_filename}"

        if result.llm_available and full_text_path.exists():
            # Pull the slice for this chapter (up to next heading or +500 lines)
            end_ln = (
                chapter_offsets[idx][0] - 1
                if idx < len(chapter_offsets)
                else min(start_ln + 500, total_lines)
            )
            excerpt = _read_chunk(full_text_path, max(1, start_ln), end_ln)
            try:
                content = _llm_chapter(idx, heading, excerpt, book_type)
            except Exception as exc:
                logger.warning("LLM chapter %d failed: %s", idx, exc)
                content = _stub_chapter(idx, heading, slug)
        else:
            content = _stub_chapter(idx, heading, slug)

        ch_path.write_text(content, encoding="utf-8")
        chapter_files.append(rel_path)
        chapter_summaries.append((rel_path, content))
        logger.debug("Wrote %s", ch_path)

    result.chapters = len(chapter_files)

    # ── Read opening text for supporting files (first ~8 000 lines) ──────────
    opening_text = ""
    if full_text_path.exists():
        opening_text = _read_chunk(full_text_path, 1, 200)

    # ── glossary.md ──────────────────────────────────────────────────────────
    if result.llm_available and opening_text:
        try:
            glossary_content = _llm_glossary(opening_text, skill_name)
        except Exception as exc:
            logger.warning("LLM glossary failed: %s", exc)
            glossary_content = _stub_glossary(metadata)
    else:
        glossary_content = _stub_glossary(metadata)

    (skill_dir / "glossary.md").write_text(glossary_content, encoding="utf-8")
    result.files.append("glossary.md")

    # ── patterns.md ──────────────────────────────────────────────────────────
    if result.llm_available and opening_text:
        try:
            patterns_content = _llm_patterns(opening_text, skill_name)
        except Exception as exc:
            logger.warning("LLM patterns failed: %s", exc)
            patterns_content = _stub_patterns(metadata)
    else:
        patterns_content = _stub_patterns(metadata)

    (skill_dir / "patterns.md").write_text(patterns_content, encoding="utf-8")
    result.files.append("patterns.md")

    # ── cheatsheet.md ─────────────────────────────────────────────────────────
    if result.llm_available and opening_text:
        try:
            cheatsheet_content = _llm_cheatsheet(opening_text, skill_name)
        except Exception as exc:
            logger.warning("LLM cheatsheet failed: %s", exc)
            cheatsheet_content = _stub_cheatsheet(metadata)
    else:
        cheatsheet_content = _stub_cheatsheet(metadata)

    (skill_dir / "cheatsheet.md").write_text(cheatsheet_content, encoding="utf-8")
    result.files.append("cheatsheet.md")

    # ── master SKILL.md ───────────────────────────────────────────────────────
    if result.llm_available and chapter_summaries:
        try:
            skill_md_content = _llm_skill_md(skill_name, metadata, chapter_summaries)
        except Exception as exc:
            logger.warning("LLM SKILL.md failed: %s", exc)
            skill_md_content = _stub_skill_md(skill_name, metadata, chapter_files)
    else:
        skill_md_content = _stub_skill_md(skill_name, metadata, chapter_files)

    (skill_dir / "SKILL.md").write_text(skill_md_content, encoding="utf-8")
    result.files.append("SKILL.md")
    result.files.extend(chapter_files)

    logger.info(
        "Skill '%s' generated: %d chapters, LLM=%s, dir=%s",
        skill_name, result.chapters, result.llm_available, skill_dir,
    )
    return result
