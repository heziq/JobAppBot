"""Applicant profile, answer bank and resume access for the apply engine.

Files:
    setup/profile.json          structured facts, see setup/profile.template.json   (local, ignored)
    setup/answers.json          [{id, question, answer, updated}] reused across applications (local, ignored)
    setup/resume/*.pdf          resumes to upload, versioned by name (*_SDE_*, *_MLE_*) (local, ignored)
    setup/cover_letter/*.docx   cover letter templates, versioned the same way      (local, ignored)
    setup/role_descriptions.json  hand-written experience/project text for form fields (local, ignored)
    data/profile/resume*.txt    cached text extraction of each resume version        (local)
    data/profile/role_descriptions.json  the most complete description of every job and
                                project across resume versions; rebuilt when a resume changes (local)
    data/profile/settings.json  engine settings such as the model                    (local)
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, Optional

from jobFilter.providers import CLAUDE, migrate_engine, validate_engine, validate_thinking_level

ROOT = Path(__file__).resolve().parent.parent
SETUP_DIR = ROOT / "setup"
PROFILE_DIR = ROOT / "data" / "profile"          # local caches and settings
PROFILE_PATH = SETUP_DIR / "profile.json"
ANSWERS_PATH = SETUP_DIR / "answers.json"
RESUME_TEXT_PATH = PROFILE_DIR / "resume.txt"
ROLES_PATH = PROFILE_DIR / "role_descriptions.json"
ROLE_OVERRIDES_PATH = SETUP_DIR / "role_descriptions.json"   # hand-written, wins over the parsed resume
RESUME_DIR = SETUP_DIR / "resume"
COVER_LETTER_DIR = SETUP_DIR / "cover_letter"
SETTINGS_PATH = PROFILE_DIR / "settings.json"
DEFAULT_SETTINGS: dict[str, Any] = {"engine": CLAUDE, "model": "opus", "max_turns": 120,
                                  "codex_model": "gpt-5.6-luna", "codex_reasoning_effort": "", "codex_timeout": 900,
                                  "resume": "auto"}
MODEL_CHOICES = ["opus", "sonnet", "haiku"]
TEMPLATE_PATH = SETUP_DIR / "profile.template.json"


def _strip(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _strip(v) for k, v in obj.items() if not k.startswith("_")}
    if isinstance(obj, list):
        return [_strip(v) for v in obj]
    return obj


# ----- profile -------------------------------------------------------------
def load_profile() -> dict[str, Any]:
    """The applicant facts handed to the engines, with `_` notes stripped out."""
    if PROFILE_PATH.exists():
        return _strip(json.loads(PROFILE_PATH.read_text()))
    return _strip(json.loads(TEMPLATE_PATH.read_text()))


def load_profile_raw() -> dict[str, Any]:
    """The file exactly as stored, including `_` comment keys.

    The Profile tab edits this. Serving the stripped version there would delete
    every note in the file the first time the user pressed Save.
    """
    path = PROFILE_PATH if PROFILE_PATH.exists() else TEMPLATE_PATH
    return json.loads(path.read_text())


def profile_exists() -> bool:
    return PROFILE_PATH.exists()


def availability_instructions(applicant: Optional[dict[str, Any]] = None) -> str:
    """Current start-date preference, also refreshed when resuming an old session."""
    preferences = (applicant if applicant is not None else load_profile()).get("preferences", {})
    date = preferences.get("earliest_start_date")
    if not date:
        return ""
    return (f"For earliest availability / earliest start date questions, use {date}, formatted as the form requires. "
            f"Applicant's explanation: {preferences.get('availability_note') or '(none supplied)'}. "
            "This current preference replaces older general start-date assumptions in the session or answer bank. "
            "Do not infer availability from the resume's graduation date or change education dates to match it. "
            "For graduation questions, use the education record. Do not choose a start date earlier than this "
            "preference; if the form only offers earlier dates or conflicting requirements, ask the user.")


def save_profile(profile: dict[str, Any]) -> None:
    SETUP_DIR.mkdir(parents=True, exist_ok=True)
    PROFILE_PATH.write_text(json.dumps(profile, indent=2, ensure_ascii=False) + "\n")


def init_profile(overwrite: bool = False) -> Path:
    if PROFILE_PATH.exists() and not overwrite:
        return PROFILE_PATH
    save_profile(_strip(json.loads(TEMPLATE_PATH.read_text())))
    return PROFILE_PATH


# ----- answer bank ---------------------------------------------------------
def load_answers() -> list[dict[str, Any]]:
    if ANSWERS_PATH.exists():
        return json.loads(ANSWERS_PATH.read_text())
    return []


def save_answers(answers: list[dict[str, Any]]) -> None:
    SETUP_DIR.mkdir(parents=True, exist_ok=True)
    ANSWERS_PATH.write_text(json.dumps(answers, indent=2, ensure_ascii=False) + "\n")


def _norm(q: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", "", q.lower()).strip()


def add_answer(question: str, answer: str) -> dict[str, Any]:
    """Insert or update by normalized question text."""
    answers = load_answers()
    key = _norm(question)
    for a in answers:
        if _norm(a["question"]) == key:
            a["answer"] = answer
            a["updated"] = int(time.time())
            save_answers(answers)
            return a
    entry = {"id": max([a.get("id", 0) for a in answers] + [0]) + 1, "question": question.strip(),
             "answer": answer, "updated": int(time.time())}
    answers.append(entry)
    save_answers(answers)
    return entry


def delete_answer(answer_id: int) -> bool:
    answers = load_answers()
    kept = [a for a in answers if a.get("id") != answer_id]
    if len(kept) == len(answers):
        return False
    save_answers(kept)
    return True


# ----- resume --------------------------------------------------------------
# One resume per job category, told apart by a version word in the file name
# (Resume_Jason_Zhu_SDE_2026-08-12.pdf, Resume_Jason_Zhu_MLE_2026-09-23.pdf).
# A PDF without one counts as the default (SDE) resume. A job uses the version
# whose rule its title matches; a version without a PDF of its own falls back
# to the default resume.
DEFAULT_RESUME = "sde"
RESUME_RULES = {
    "mle": re.compile(r"\b(machine[- ]learning|ml|mle|mlops|ai|genai|artificial intelligence|deep learning|llms?|nlp|"
                      r"natural language|computer vision|perception|agentic|data scien(ce|tist)|"
                      r"applied scien(ce|tist)|research (engineer|scientist))\b", re.I),
}
RESUME_VERSIONS = (DEFAULT_RESUME, *RESUME_RULES)
RESUME_CHOICES = ("auto", *RESUME_VERSIONS)
# "AI-native" / "AI-first" describe the company, not the role.
_AI_DESCRIPTOR = re.compile(r"\bAI[- ](native|first|powered|enabled|driven)\b", re.I)


def validate_resume_choice(choice: Any) -> str:
    choice = str(choice or "auto").strip().lower()
    if choice not in RESUME_CHOICES:
        raise ValueError(f"resume must be one of: {', '.join(RESUME_CHOICES)}")
    return choice


def job_resume(job: dict[str, Any], choice: Optional[str] = "auto") -> tuple[str, str]:
    """(version, reason): the version the user chose, else the first rule the job title matches."""
    choice = validate_resume_choice(choice)
    if choice != "auto":
        return choice, "chosen when the application was started"
    title = _AI_DESCRIPTOR.sub(" ", job.get("title") or "")
    for version, rule in RESUME_RULES.items():
        match = rule.search(title)
        if match:
            return version, f'title mentions "{match.group(0)}"'
    return DEFAULT_RESUME, "title matches no other resume version"


def file_version(path: Path) -> str:
    """Version named in the file name as a separate word (_MLE_, -mle, " MLE"), else the default."""
    words = re.split(r"[^a-z0-9]+", path.stem.lower())
    return next((v for v in RESUME_VERSIONS if v in words), DEFAULT_RESUME)


def _newest(folder: Path, pattern: str, version: str) -> Optional[Path]:
    files = [p for p in folder.glob(pattern) if not p.name.startswith("~$") and file_version(p) == version] \
        if folder.is_dir() else []   # ~$ files are Word's lock files for an open document
    return max(files, key=lambda p: p.stat().st_mtime, default=None)


def resume_path(version: Optional[str] = None) -> Optional[Path]:
    """Newest PDF of that resume version, falling back to the default resume."""
    return _newest(RESUME_DIR, "*.pdf", version or DEFAULT_RESUME) or _newest(RESUME_DIR, "*.pdf", DEFAULT_RESUME)


def cover_letter_template(version: Optional[str] = None) -> Optional[Path]:
    """Newest Word template of that version in setup/cover_letter/, falling back to the default one."""
    return (_newest(COVER_LETTER_DIR, "*.docx", version or DEFAULT_RESUME)
            or _newest(COVER_LETTER_DIR, "*.docx", DEFAULT_RESUME))


def application_resume(job: dict[str, Any], choice: Optional[str] = "auto") -> dict[str, Any]:
    """Resume version, file and text for one job's application."""
    version, reason = job_resume(job, choice)
    path = resume_path(version)
    if path and file_version(path) != version:
        reason += f"; setup/resume/ has no *_{version.upper()}_* PDF, so the default resume is used"
    return {"version": version, "reason": reason, "path": path, "text": resume_text(path=path) if path else ""}


def resume_text(refresh: bool = False, path: Optional[Path] = None) -> str:
    path = path or resume_path()
    if not path:
        return ""
    version = file_version(path)
    cache = RESUME_TEXT_PATH if version == DEFAULT_RESUME else RESUME_TEXT_PATH.with_name(f"resume.{version}.txt")
    if cache.exists() and not refresh and cache.stat().st_mtime >= path.stat().st_mtime:
        return cache.read_text()
    try:
        import fitz  # PyMuPDF, optional
    except ImportError:
        return ""
    doc = fitz.open(path)
    text = reflow_resume("\n".join(page.get_text() for page in doc))
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(text)
    return text


_BULLET_RE = re.compile(r"^(?:[\uf09f\uf0b7\u2022\u25cf\u25aa\u2023\u2043\u00b7\u25e6\u2219]|[-*o](?=\s|$))\s*(.*)$")
_DATE_RE = re.compile(r"\b((Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.?\s+\d{4}|\d{4})\s*[–—-]\s*((Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.?\s+\d{4}|\d{4}|Present|Current|Expected.*)", re.I)


def reflow_resume(raw: str) -> str:
    """Turn PDF line-wrapped text into one line per bullet.

    PDF extraction yields the bullet glyph on its own line (or as a prefix)
    followed by the bullet's text broken at the column width. This joins
    those fragments back together so each bullet is a single `• ...` line,
    which is what application forms expect in a description box.
    """
    out: list[str] = []
    frags: list[str] = []
    in_bullet = False
    prev_len = 0
    lines = raw.splitlines()

    def flush() -> None:
        nonlocal frags, in_bullet
        if in_bullet and frags:
            text = ""
            for f in (f.strip() for f in frags if f.strip()):
                # A line broken after a hyphen ("hot-" / "swaps") joins without a space.
                text += f if re.search(r"\w-$", text) else (" " + f if text else f)
            out.append("• " + text)
        frags, in_bullet = [], False

    def dates_follow(i: int) -> bool:
        """The next non-blank line is only a date range, as LaTeX PDFs put it under an entry title."""
        nxt = next((l.strip() for l in lines[i + 1:] if l.strip()), "")
        m = _DATE_RE.match(nxt)
        return bool(m) and len(nxt) - m.end() <= 12   # room for " (Expected)"

    for i, line in enumerate(lines):
        stripped = re.sub(r"[ \t]{2,}", "  ", line).strip()
        if not stripped:
            flush(); prev_len = 0
            continue
        if stripped.lower() in _SECTIONS:
            # A section heading must never be swallowed into the bullet above it,
            # or everything after it is attributed to the wrong section.
            flush(); out.append(stripped); prev_len = 0
            continue
        m = _BULLET_RE.match(stripped)
        if m:
            flush()
            in_bullet = True
            frags = [m.group(1)] if m.group(1).strip() else []
            prev_len = len(m.group(1)) if frags else 999   # 999: next line is the first fragment
            continue
        is_header = bool(_DATE_RE.search(stripped)) or dates_follow(i)
        continues = in_bullet and not is_header and (prev_len >= 80 or stripped[:1].islower() or stripped[:1] in "(,;&")
        if continues:
            frags.append(stripped)
            prev_len = len(stripped)
            continue
        flush()
        out.append(stripped)
        prev_len = len(stripped)
    flush()
    return "\n".join(out)


# ----- role descriptions ---------------------------------------------------
# Resume versions trim bullets to fit a page (the MLE one keeps 2 of the 4 Seattle
# bullets), but experience fields in application forms have room for everything.
# So forms get one stored set of role descriptions, the most complete wording of
# each job and project across all resume versions; the version only picks the file.
_SECTIONS = {"education": "", "technical skills": "", "skills": "", "highlights of qualifications": "",
             "teaching and leadership": "", "leadership": "", "interests": "", "awards": "",
             "experience": "experience", "work experience": "experience", "professional experience": "experience",
             "internship": "experience", "internships": "experience",
             "industry and research experience": "experience", "industry experience": "experience",
             "projects": "projects", "project & research": "projects", "research & projects": "projects",
             "projects & research": "projects", "research experience": "projects", "research": "projects",
             "projects and research": "projects", "research and projects": "projects",
             "ai and software projects": "projects", "software projects": "projects",
             "graphics, vision, and interactive systems projects": "projects",
             "selected projects": "projects", "personal projects": "projects"}

# Lines a template puts between an entry's title and its date range: a bare
# separator, a location, a team name. Used only to bridge that gap.
_SEPARATOR_ONLY = re.compile(r"^[|/\\·•\-–—,:;\s]*$")


# A multi-page resume repeats the role as "<role> (continued)" on the next page.
_CONTINUED = re.compile(r"\(\s*cont(?:inued|\.)?\s*\)", re.I)


def _bridges_to_dates(line: str) -> bool:
    return bool(_SEPARATOR_ONLY.match(line)) or len(line) <= 48


def resume_entries(text: str) -> list[dict[str, Any]]:
    """Jobs and projects with bullets in a reflowed resume: [{kind, heading, dates, bullets}].

    Handles both layouts: "Title, Company  Jun 2026 – Sep 2026" on one line (Word)
    and the dates on the line after the title (LaTeX).
    """
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    entries: list[dict[str, Any]] = []
    kind, current = "", None
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("• "):
            if current is not None:
                current["bullets"].append(line[2:].strip())
            i += 1
            continue
        if line.lower() in _SECTIONS:
            kind, current = _SECTIONS[line.lower()], None
            i += 1
            continue
        heading = dates = None
        consumed = 0
        inline = _DATE_RE.search(line)
        if inline and inline.start() > 0:
            heading, dates = line[:inline.start()], line[inline.start():]
        elif not inline:
            # The dates may be on the next line (LaTeX) or a few lines down with
            # only a location / separator in between (also LaTeX, two columns).
            for ahead in range(1, 5):
                if i + ahead >= len(lines):
                    break
                nxt = lines[i + ahead]
                match = _DATE_RE.match(nxt)
                if match and len(nxt) - match.end() <= 12:
                    heading, dates, consumed = line, nxt, ahead
                    break
                if nxt.startswith("• ") or nxt.lower() in _SECTIONS or not _bridges_to_dates(nxt):
                    break
        if dates is None:
            # A bare date line belongs to the heading above. Metadata before an
            # entry's first bullet (team, tech stack, links) must not end it;
            # once bullets have started, any other line does -- except a
            # "... (continued)" banner where a multi-page resume resumes a role.
            if (not (inline and inline.start() == 0) and current is not None and current["bullets"]
                    and not _CONTINUED.search(line)):
                current = None
            i += 1
            continue
        current = {"kind": kind, "heading": heading.strip(), "dates": dates.strip(), "bullets": []}
        if kind:
            entries.append(current)
        i += consumed + 1
    return [e for e in entries if e["bullets"]]


def _resume_files() -> list[tuple[str, Path]]:
    """The newest PDF of each resume version, default first."""
    seen, out = set(), []
    for version in RESUME_VERSIONS:
        path = resume_path(version)
        if path and path not in seen and file_version(path) == version:
            seen.add(path)
            out.append((version, path))
    return out


def _entry_key(heading: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (heading or "").lower()).strip()


def load_role_overrides() -> list[dict[str, Any]]:
    """Hand-written role descriptions from setup/role_descriptions.json.

    These are the authoritative text for application form fields. A resume PDF
    trims bullets to fit one page and its layout is parsed heuristically, so the
    user may keep fuller, cleaner wording here; it survives every resume rebuild.
    """
    if not ROLE_OVERRIDES_PATH.exists():
        return []
    try:
        data = json.loads(ROLE_OVERRIDES_PATH.read_text())
    except ValueError as e:
        raise ValueError(f"{ROLE_OVERRIDES_PATH.name} is not valid JSON: {e}") from e
    entries = data.get("entries") if isinstance(data, dict) else data
    if not isinstance(entries, list):
        raise ValueError(f"{ROLE_OVERRIDES_PATH.name} must hold an 'entries' list")
    cleaned: list[dict[str, Any]] = []
    for raw in entries:
        if not isinstance(raw, dict) or str(raw.get("heading", "")).startswith("_"):
            continue
        heading = str(raw.get("heading") or "").strip()
        bullets = [str(b).strip() for b in (raw.get("bullets") or []) if str(b).strip()]
        if not heading or not bullets:
            continue
        kind = raw.get("kind") if raw.get("kind") in ("experience", "projects") else "experience"
        cleaned.append({"kind": kind, "heading": heading, "dates": str(raw.get("dates") or "").strip(),
                        "bullets": bullets, "from": "manual"})
    return cleaned


def _merge_overrides(derived: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Hand-written entries replace the parsed entry with the same heading."""
    overrides = {_entry_key(e["heading"]): e for e in load_role_overrides()}
    if not overrides:
        return derived
    merged, used = [], set()
    for entry in derived:
        key = _entry_key(entry["heading"])
        if key in overrides:
            merged.append(overrides[key])
            used.add(key)
        else:
            merged.append(entry)
    merged += [e for key, e in overrides.items() if key not in used]
    return sorted(merged, key=lambda e: e["kind"] != "experience")


def role_descriptions(refresh: bool = False) -> list[dict[str, Any]]:
    """The stored role descriptions: rebuilt only when a resume file changes.

    Hand-written entries in setup/role_descriptions.json always win; the parsed
    resume entries only fill in whatever they do not cover.
    """
    return _merge_overrides(_derived_role_descriptions(refresh=refresh))


def _derived_role_descriptions(refresh: bool = False) -> list[dict[str, Any]]:
    files = _resume_files()
    fingerprint = [[p.name, p.stat().st_mtime, p.stat().st_size] for _, p in files]
    if not refresh and ROLES_PATH.exists():
        try:
            stored = json.loads(ROLES_PATH.read_text())
            if stored.get("fingerprint") == fingerprint:
                return stored["entries"]
        except ValueError:
            pass
    best: dict[str, dict[str, Any]] = {}
    for version, path in files:
        for entry in resume_entries(resume_text(path=path)):
            key = re.sub(r"[^a-z0-9]+", " ", entry["heading"].lower()).strip()
            size = sum(len(b) for b in entry["bullets"])
            if key not in best or size > sum(len(b) for b in best[key]["bullets"]):
                best[key] = {**entry, "from": version, "order": best.get(key, {}).get("order", len(best))}
    entries = sorted(best.values(), key=lambda e: (e["kind"] != "experience", e["order"]))
    for e in entries:
        e.pop("order")
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    ROLES_PATH.write_text(json.dumps({"fingerprint": fingerprint, "updated": time.strftime("%Y-%m-%dT%H:%M:%S"),
                                      "entries": entries}, indent=2, ensure_ascii=False) + "\n")
    return entries


def role_descriptions_text(entries: Optional[list[dict[str, Any]]] = None) -> str:
    entries = role_descriptions() if entries is None else entries
    blocks = []
    for kind, title in (("experience", "JOBS (for work experience entries)"), ("projects", "PROJECTS & RESEARCH")):
        items = [e for e in entries if e["kind"] == kind]
        if items:
            blocks.append(title + "\n" + "\n\n".join(
                f"{e['heading']} | {e['dates']}\n" + "\n".join("• " + b for b in e["bullets"]) for e in items))
    return "\n\n".join(blocks)


# ----- engine settings -----------------------------------------------------
def load_settings() -> dict[str, Any]:
    data = dict(DEFAULT_SETTINGS)
    if SETTINGS_PATH.exists():
        try:
            data.update(json.loads(SETTINGS_PATH.read_text()))
        except ValueError:
            pass
    data["engine"] = migrate_engine(data["engine"])
    return data


def application_settings(updates: dict[str, Any]) -> dict[str, Any]:
    data = load_settings()
    if "engine" in updates:
        data["engine"] = validate_engine(updates["engine"])
    if "model" in updates:
        data["model"] = str(updates["model"]).strip() or DEFAULT_SETTINGS["model"]
    if "max_turns" in updates:
        data["max_turns"] = max(10, min(400, int(updates["max_turns"])))
    if "codex_model" in updates:
        data["codex_model"] = str(updates["codex_model"]).strip()
    if "codex_reasoning_effort" in updates:
        data["codex_reasoning_effort"] = validate_thinking_level(updates["codex_reasoning_effort"])
    if "codex_timeout" in updates:
        data["codex_timeout"] = max(30, min(3600, int(updates["codex_timeout"])))
    if "resume" in updates:
        data["resume"] = validate_resume_choice(updates["resume"])
    return data


def save_settings(updates: dict[str, Any]) -> dict[str, Any]:
    data = application_settings(updates)
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    SETTINGS_PATH.write_text(json.dumps(data, indent=2) + "\n")
    return data


def status() -> dict[str, Any]:
    p = resume_path()
    return {
        "profile_exists": profile_exists(),
        "answers": len(load_answers()),
        "resume": str(p) if p else None,
        "resume_text_chars": len(resume_text()) if p else 0,
        # Versions with their own PDF; the others fall back to the default resume.
        "resume_versions": {v: str(f) if (f := _newest(RESUME_DIR, "*.pdf", v)) else None for v in RESUME_RULES},
        "cover_letter_templates": {v: str(f) if (f := _newest(COVER_LETTER_DIR, "*.docx", v)) else None
                                   for v in RESUME_VERSIONS},
        "role_descriptions": len(role_descriptions()) if p else 0,
    }
