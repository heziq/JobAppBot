"""Ashby company boards, read through the public posting-api.

One GET per company returns that employer's whole board as JSON, with no auth
and no paging:

    GET https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true

Unlike the aggregator sources these postings are the employer's own, so a
closed role disappears from the board instead of lingering as a dead link that
only the apply engine discovers.

Two differences from the Greenhouse source:

  * The response always carries each posting's body (`descriptionPlain`); there
    is no list-only mode. Ramp's board measured 3.26 MB for 180 postings on
    2026-11-24. That is the cost of using this API at all, so the bodies are
    put to work: the experience rule reads them without a second request.
  * Locations are structured (`address.postalAddress`, plus
    `secondaryLocations`), so country and state come from fields rather than
    from parsing a display string.

career-ops documents a ~10s server-side latency floor on this endpoint, hence
the longer default timeout.

Ported in design, not code, from career-ops providers/ashby.mjs (MIT).
"""
from __future__ import annotations

import re
import time
import urllib.parse
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from jobFilter.greenhouse import _epoch_millis, is_work_model_only
from jobFilter.http_client import requests
from jobFilter.models import Job

API_URL = "https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true"
BOARD_URL = "https://jobs.ashbyhq.com/{slug}"

ALLOWED_HOSTS = frozenset({"api.ashbyhq.com", "jobs.ashbyhq.com"})
_SLUG_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,98}[A-Za-z0-9])?$")

# Bodies are always inline, so a board is far larger than a Greenhouse list:
# 3.26 MB for 180 postings. The ceiling is for a changed or hostile endpoint.
MAX_BYTES = 64 * 1024 * 1024
REQUEST_TIMEOUT = 90
REQUEST_DELAY_S = 0.2

_US_STATES = {"AL","AK","AZ","AR","CA","CO","CT","DE","FL","GA","HI","ID","IL","IN","IA","KS","KY","LA","ME","MD",
              "MA","MI","MN","MS","MO","MT","NE","NV","NH","NJ","NM","NY","NC","ND","OH","OK","OR","PA","RI","SC",
              "SD","TN","TX","UT","VT","VA","WA","WV","WI","WY","DC"}
_US_COUNTRY = {"usa", "us", "u.s.", "u.s.a.", "united states", "united states of america"}

# Ashby's employmentType vocabulary, mapped onto the `commitment` values the
# other sources already use.
_EMPLOYMENT = {"FullTime": "Full Time", "PartTime": "Part Time", "Intern": "Internship",
               "Contract": "Contract", "Temporary": "Temporary"}


def board_slug(entry: Any) -> str:
    """The board token for one configured entry, or ValueError.

    Accepts a bare string, or a mapping with `slug`, `api` or `careers_url`.
    An unknown host is refused rather than fetched: the board list is data
    copied from elsewhere, and an unchecked URL would make the scanner fetch
    whatever it names.
    """
    if isinstance(entry, str):
        candidate = entry.strip()
    elif isinstance(entry, dict):
        candidate = (entry.get("slug") or entry.get("api") or entry.get("careers_url") or "").strip()
    else:
        raise ValueError(f"ashby board entry must be a string or object, got {type(entry).__name__}")
    source = candidate
    if not candidate:
        raise ValueError("ashby board entry needs a slug, api or careers_url")

    if "/" in candidate or ":" in candidate:
        parsed = urllib.parse.urlparse(candidate if "//" in candidate else "https://" + candidate)
        if parsed.scheme not in ("https", "http") or (parsed.hostname or "").lower() not in ALLOWED_HOSTS:
            raise ValueError(f"not an Ashby board URL: {source}")
        parts = [p for p in parsed.path.split("/") if p]
        # /posting-api/job-board/<slug>  or  /<slug>[/<posting id>]
        if parts[:2] == ["posting-api", "job-board"] and len(parts) >= 3:
            candidate = parts[2]
        elif parts:
            candidate = parts[0]
        else:
            raise ValueError(f"no board token in URL: {source}")

    if not _SLUG_RE.match(candidate):
        raise ValueError(f"malformed Ashby board token: {candidate!r}")
    return candidate


def _places(row: dict[str, Any]) -> list[dict[str, Any]]:
    """The posting's own location plus any secondary ones, as {display, postal}."""
    out = [{"display": row.get("location"), "postal": ((row.get("address") or {}).get("postalAddress") or {})}]
    for extra in row.get("secondaryLocations") or []:
        if isinstance(extra, dict):
            out.append({"display": extra.get("location"),
                        "postal": ((extra.get("address") or {}).get("postalAddress") or {})})
    return out


def _country_code(postal: dict[str, Any], display: Optional[str]) -> Optional[str]:
    """ISO-ish code from the structured address, falling back to the display string."""
    from jobFilter.sources import guess_countries
    country = str(postal.get("addressCountry") or "").strip()
    if country.lower() in _US_COUNTRY:
        return "US"
    if country:
        found = guess_countries(country)
        if found:
            return found[0]
    region = str(postal.get("addressRegion") or "").strip().upper()
    if region in _US_STATES:
        return "US"       # a US state name implies the country even when it is left out
    found = guess_countries(display or "")
    return found[0] if found else None


def parse_board(payload: Any, slug: str, company: Optional[str] = None,
                max_age_days: Optional[float] = None, now: Optional[datetime] = None,
                assume_us_for_remote: bool = True) -> list[tuple[Job, str]]:
    """Map one board response to (Job, description) pairs.

    The description rides along because this API always sends it: returning it
    lets the caller use it without the second request `descriptions.py` would
    otherwise make, which for Ashby means re-downloading the whole board.
    """
    from jobFilter.sources import _job, guess_countries

    rows = (payload or {}).get("jobs") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return []
    now = now or datetime.now(timezone.utc)
    max_age_s = float(max_age_days) * 86400 if max_age_days is not None else None
    board = BOARD_URL.format(slug=slug)
    out: list[tuple[Job, str]] = []

    for row in rows:
        if not isinstance(row, dict):
            continue
        posting_id, title = row.get("id"), row.get("title")
        apply_url = row.get("applyUrl") or row.get("jobUrl")
        if not posting_id or not isinstance(title, str) or not title.strip() or not isinstance(apply_url, str):
            continue
        if (urllib.parse.urlparse(apply_url).hostname or "").lower() not in ALLOWED_HOSTS:
            continue
        if row.get("isListed") is False:
            continue        # withdrawn from the public board; only reachable by direct link

        published_at, published_millis = _epoch_millis(row.get("publishedAt"))
        if max_age_s is not None:
            if published_millis is None:
                continue
            if (now.timestamp() * 1000 - published_millis) / 1000 > max_age_s:
                continue

        places = _places(row)
        display = str(row.get("location") or "").strip()
        countries = sorted({c for p in places if (c := _country_code(p["postal"], p["display"]))})
        states = sorted({r for p in places
                         if (r := str(p["postal"].get("addressRegion") or "").strip().upper()) in _US_STATES})
        if not countries and (row.get("isRemote") or is_work_model_only(display)):
            countries = guess_countries(display or "remote", assume_us_for_remote=assume_us_for_remote)

        commitment = _EMPLOYMENT.get(str(row.get("employmentType") or ""))
        description = str(row.get("descriptionPlain") or "")

        out.append((_job(
            "ashby",
            id=f"ashby___{slug}___{posting_id}",
            title=title.strip(),
            company=(company or slug).strip(),
            location=display or None,
            workplace_type=("Remote" if row.get("isRemote") else None) or (row.get("workplaceType") or None),
            countries=countries, states=states,
            commitment=[commitment] if commitment else [],
            # Ashby states no seniority, experience minimum or category: a
            # company board carries every department. The title rules and the
            # description-based experience inference do that work instead.
            seniority=None, min_yoe=None, category=None,
            published_at=published_at, published_millis=published_millis,
            apply_url=apply_url,
            listing_url=row.get("jobUrl") or board,
            source="ashby",
            dedup_key=f"ashby___{slug}___{posting_id}",
            raw={"slug": slug, "department": row.get("department"), "team": row.get("team"),
                 "employment_type": row.get("employmentType"),
                 "compensation": (row.get("compensation") or {}).get("compensationTierSummary")},
        ), description))
    return out


def fetch_board(slug: str, timeout: int = REQUEST_TIMEOUT) -> Any:
    r = requests.get(API_URL.format(slug=slug), impersonate="chrome", timeout=timeout,
                     headers={"accept": "application/json"})
    r.raise_for_status()
    declared = r.headers.get("content-length")
    size = int(declared) if declared and declared.isdigit() else len(r.content)
    if size > MAX_BYTES:
        raise ValueError(f"ashby board {slug}: response of {size} bytes exceeds {MAX_BYTES}")
    return r.json()


def fetch_ashby(cfg: dict[str, Any], log: Callable[[str], None] = lambda s: None,
                descriptions: Optional[dict[str, str]] = None) -> list[Job]:
    """Every enabled board in `cfg["boards"]`. One broken board does not stop the rest.

    `descriptions`, when given, is filled with {job id: posting body} so the
    caller can run the experience rule without fetching each body again.
    """
    max_age_days = cfg.get("max_age_days")
    jobs: list[Job] = []
    entries = [e for e in (cfg.get("boards") or [])
               if not (isinstance(e, dict) and e.get("enabled") is False)]
    for index, entry in enumerate(entries):
        name = entry.get("name") if isinstance(entry, dict) else None
        try:
            slug = board_slug(entry)
        except ValueError as e:
            log(f"    ashby: skipped {name or entry!r}: {e}")
            continue
        if index:
            time.sleep(REQUEST_DELAY_S)
        try:
            pairs = parse_board(fetch_board(slug), slug, company=name, max_age_days=max_age_days)
        except Exception as e:
            log(f"    ashby/{slug}: FAILED {type(e).__name__}: {e}")
            continue
        for job, description in pairs:
            jobs.append(job)
            if descriptions is not None and description:
                descriptions[job.id] = description
        log(f"    ashby/{slug}: {len(pairs)} jobs")
    return jobs
