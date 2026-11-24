"""Greenhouse company boards, read through the public boards-api.

One GET per company returns that employer's whole board as JSON, with no auth,
no paging and no key:

    GET https://boards-api.greenhouse.io/v1/boards/{slug}/jobs

Unlike the aggregator sources, these postings are the employer's own, so a
closed role disappears from the board instead of lingering as a dead link that
only the apply engine discovers.

`?content=true` would add each posting's full body inline, and is deliberately
NOT used: measured on anthropic's board 2026-11-23, the plain list is 632 jobs
in 0.45 MB while the same board with bodies inline is 9.57 MB -- 21x for text
that only the few postings surviving the filters ever need. `descriptions.py`
already fetches one body on demand from `/jobs/{id}` for screening and for
years-of-experience inference, so the bodies stay lazy and the scan stays small
at any board size. career-ops's own greenhouse.mjs reaches the same conclusion
after hitting a 45.7 MB response on a 2,600-job board.

Rate limits: one request per configured board per scan, 150-250 ms apart
(the interval career-ops documents for this API).

Ported in design, not code, from career-ops providers/greenhouse.mjs (MIT).
"""
from __future__ import annotations

import re
import time
import urllib.parse
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from jobFilter.http_client import requests
from jobFilter.models import Job

API_HOST = "boards-api.greenhouse.io"
API_URL = "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"
BOARD_URL = "https://job-boards.greenhouse.io/{slug}"

# A configured board may be written as a slug or as any of the URLs a company
# careers page actually links to. Anything outside this set is refused rather
# than fetched: the board list is data copied from elsewhere, and an unchecked
# `api:` field would otherwise make the scanner fetch an arbitrary URL.
ALLOWED_HOSTS = frozenset({
    "boards-api.greenhouse.io",
    "boards.greenhouse.io",
    "boards.eu.greenhouse.io",
    "job-boards.greenhouse.io",
    "job-boards.eu.greenhouse.io",
})

# Greenhouse board tokens are lowercase alphanumeric with dashes/underscores.
_SLUG_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,98}[A-Za-z0-9])?$")

# A board's plain list is small (0.45 MB for 632 jobs), so a response far past
# that is a changed endpoint or a hostile one, not a big company.
MAX_BYTES = 32 * 1024 * 1024
REQUEST_DELAY_S = 0.2

# A location string that carries a work model but no geography at all
# ("Hybrid", "Remote; Flexible"). Anything with a place in it ("Remote (Canada)",
# "Hybrid - London") has something to filter on and is left alone.
_WORK_MODEL = re.compile(r"^(?:hybrid|in[-\s]?office|on[-\s]?site|onsite|distributed|remote|flexible)$", re.I)
_PLACE_SPLIT = re.compile(r"\s*[|;/]\s*|\s+or\s+", re.I)
_US_STATE_RE = re.compile(r",\s*([A-Z]{2})\b")


def is_work_model_only(name: Any) -> bool:
    """True when every part of a location string is a work model, not a place."""
    if not isinstance(name, str):
        return False
    parts = [p.strip() for p in _PLACE_SPLIT.split(name) if p.strip()]
    return bool(parts) and all(_WORK_MODEL.match(p) for p in parts)


def board_slug(entry: Any) -> str:
    """The board token for one configured entry.

    Accepts a bare string, or a mapping with `slug`, `api` or `careers_url`.
    Raises ValueError on an unknown host or a malformed token, so a bad row
    fails loudly instead of being silently skipped.
    """
    if isinstance(entry, str):
        candidate, source = entry.strip(), entry
    elif isinstance(entry, dict):
        candidate = (entry.get("slug") or entry.get("api") or entry.get("careers_url") or "").strip()
        source = candidate
    else:
        raise ValueError(f"greenhouse board entry must be a string or object, got {type(entry).__name__}")
    if not candidate:
        raise ValueError("greenhouse board entry needs a slug, api or careers_url")

    if "/" in candidate or ":" in candidate:
        parsed = urllib.parse.urlparse(candidate if "//" in candidate else "https://" + candidate)
        if parsed.scheme not in ("https", "http") or (parsed.hostname or "").lower() not in ALLOWED_HOSTS:
            raise ValueError(f"not a Greenhouse board URL: {source}")
        # /v1/boards/<slug>/jobs  or  /<slug>[/jobs/<id>]
        parts = [p for p in parsed.path.split("/") if p]
        if parts[:2] == ["v1", "boards"] and len(parts) >= 3:
            candidate = parts[2]
        elif parts:
            candidate = parts[0]
        else:
            raise ValueError(f"no board token in URL: {source}")

    if not _SLUG_RE.match(candidate):
        raise ValueError(f"malformed Greenhouse board token: {candidate!r}")
    return candidate


def _epoch_millis(value: Any) -> tuple[Optional[str], Optional[int]]:
    if not isinstance(value, str) or not value:
        return None, None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None, None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds"), int(dt.timestamp() * 1000)


def parse_board(payload: Any, slug: str, company: Optional[str] = None,
                max_age_days: Optional[float] = None, now: Optional[datetime] = None,
                assume_us_for_remote: bool = True) -> list[Job]:
    """Map one board response to Jobs. Separated from fetching so it is testable."""
    from jobFilter.sources import _job, guess_countries

    rows = (payload or {}).get("jobs") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return []
    now = now or datetime.now(timezone.utc)
    max_age_s = float(max_age_days) * 86400 if max_age_days is not None else None
    board = BOARD_URL.format(slug=slug)
    jobs: list[Job] = []

    for row in rows:
        if not isinstance(row, dict):
            continue
        job_id, apply_url = row.get("id"), row.get("absolute_url")
        title = row.get("title")
        if job_id is None or not isinstance(apply_url, str) or not isinstance(title, str) or not title.strip():
            continue
        # The board is the employer's own, but the link still comes from data:
        # only Greenhouse-hosted apply URLs are accepted.
        if (urllib.parse.urlparse(apply_url).hostname or "").lower() not in ALLOWED_HOSTS:
            continue

        published_at, published_millis = _epoch_millis(row.get("first_published") or row.get("updated_at"))
        if max_age_s is not None:
            if published_millis is None:
                continue  # undated posting: cannot be judged against a freshness window
            if (now.timestamp() * 1000 - published_millis) / 1000 > max_age_s:
                continue

        raw_location = ((row.get("location") or {}).get("name")
                        if isinstance(row.get("location"), dict) else None)
        work_model_only = is_work_model_only(raw_location)
        places = [] if work_model_only else [
            p.strip() for p in _PLACE_SPLIT.split(raw_location or "") if p.strip()]

        countries = sorted({c for p in places for c in guess_countries(p, assume_us_for_remote=assume_us_for_remote)})
        if not countries and (work_model_only or not places):
            # A bare "Remote" names no geography. Recovering the city would need
            # /v1/boards/{slug}/offices, which repeats the whole job list per
            # office (career-ops measured Stripe's at 29.2 MB), so the posting is
            # treated the way the Simplify source treats its remote rows instead.
            countries = guess_countries(raw_location or "remote", assume_us_for_remote=assume_us_for_remote)

        jobs.append(_job(
            "greenhouse",
            id=f"greenhouse___{slug}___{job_id}",
            title=title.strip(),
            company=(row.get("company_name") or company or slug).strip(),
            location=raw_location or None,
            workplace_type="Remote" if re.search(r"\bremote\b", raw_location or "", re.I) else None,
            countries=countries,
            states=sorted({m.group(1) for p in places for m in [_US_STATE_RE.search(p)] if m}),
            # Greenhouse states no seniority, experience minimum or category: a
            # company board carries every department. The title rules and the
            # description-based experience inference do that work instead.
            seniority=None, min_yoe=None, category=None,
            published_at=published_at, published_millis=published_millis,
            apply_url=apply_url,
            listing_url=board,
            source="greenhouse",
            dedup_key=f"greenhouse___{slug}___{job_id}",
            raw={"slug": slug, "requisition_id": row.get("requisition_id"),
                 "internal_job_id": row.get("internal_job_id"),
                 "updated_at": row.get("updated_at"),
                 "application_deadline": row.get("application_deadline")},
        ))
    return jobs


def fetch_board(slug: str, timeout: int = 60) -> Any:
    url = API_URL.format(slug=slug)
    r = requests.get(url, impersonate="chrome", timeout=timeout,
                     headers={"accept": "application/json"})
    r.raise_for_status()
    declared = r.headers.get("content-length")
    size = int(declared) if declared and declared.isdigit() else len(r.content)
    if size > MAX_BYTES:
        raise ValueError(f"greenhouse board {slug}: response of {size} bytes exceeds {MAX_BYTES}")
    return r.json()


def fetch_greenhouse(cfg: dict[str, Any], log: Callable[[str], None] = lambda s: None) -> list[Job]:
    """Every enabled board in `cfg["boards"]`. One broken board does not stop the rest."""
    max_age_days = cfg.get("max_age_days")
    jobs: list[Job] = []
    entries = [e for e in (cfg.get("boards") or [])
               if not (isinstance(e, dict) and e.get("enabled") is False)]
    for index, entry in enumerate(entries):
        name = entry.get("name") if isinstance(entry, dict) else None
        try:
            slug = board_slug(entry)
        except ValueError as e:
            log(f"    greenhouse: skipped {name or entry!r}: {e}")
            continue
        if index:
            time.sleep(REQUEST_DELAY_S)
        try:
            got = parse_board(fetch_board(slug), slug, company=name, max_age_days=max_age_days)
        except Exception as e:
            log(f"    greenhouse/{slug}: FAILED {type(e).__name__}: {e}")
            continue
        jobs.extend(got)
        log(f"    greenhouse/{slug}: {len(got)} jobs")
    return jobs
