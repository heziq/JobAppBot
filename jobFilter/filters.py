"""Rule-based filtering on top of hiring.cafe's own search filters.

Each rule returns a rejection reason string, or None when the job passes.
Rules are configured under the `rules` key of setup/search.json.

Title keywords are matched by `jobFilter.title_keywords`, which supports the
`word:` / `stem:` / `" + "` forms; see that module for the semantics.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any, Callable, Optional

from jobFilter import title_keywords
from jobFilter.models import Job

Rule = Callable[[Job, dict[str, Any]], Optional[str]]


# Compiling a keyword list builds one regex per anchored entry, so it is cached
# across the jobs of a scan rather than redone per job. Keyed on the list itself:
# `rules` is re-read from config per call and the lists are small.
@lru_cache(maxsize=32)
def _compiled(words: tuple[Any, ...], positive: bool) -> tuple[tuple[str, Any], ...]:
    return title_keywords.compile_list(words, positive=positive)


def _countries(job: Job, rules: dict) -> Optional[str]:
    req = rules.get("require_countries")
    if req and not set(job.countries) & set(req):
        return f"country {job.countries or '?'} not in {req}"
    return None


def _categories(job: Job, rules: dict) -> Optional[str]:
    req = rules.get("require_categories")
    # An unknown category is not a mismatch, the same way _max_yoe passes a job
    # whose min_yoe the posting never stated. Sources that carry a category
    # taxonomy (hiring.cafe, Simplify) are still held to the list; a direct
    # employer board has no taxonomy at all -- every department is on one board
    # -- so there the title rules are the role filter.
    if req and job.category is not None and job.category not in req:
        return f"category '{job.category}'"
    return None


def _title_include(job: Job, rules: dict) -> Optional[str]:
    # An empty list is "no positive constraint", not "match nothing": an
    # exclude-only title filter is a legitimate configuration.
    compiled = _compiled(tuple(rules.get("title_include") or []), True)
    if compiled and not title_keywords.any_match(compiled, job.title):
        return "title lacks include keyword"
    return None


def _title_exclude(job: Job, rules: dict) -> Optional[str]:
    hit = title_keywords.first_match(_compiled(tuple(rules.get("title_exclude") or []), False), job.title)
    return f"title contains '{hit}'" if hit else None


def _max_yoe(job: Job, rules: dict) -> Optional[str]:
    limit = rules.get("max_min_yoe")
    if limit is not None and job.min_yoe is not None and job.min_yoe > limit:
        return f"requires {job.min_yoe} yoe > {limit}"
    return None


def _clearance(job: Job, rules: dict) -> Optional[str]:
    if rules.get("exclude_security_clearance") and job.security_clearance not in (None, "None"):
        return f"security clearance: {job.security_clearance}"
    return None


def _published_millis(job: Job) -> Optional[float]:
    """Epoch millis for the posting, falling back to parsing `published_at`.

    hiring.cafe -- the largest source -- sends a publish date but no millis
    field, so a rule reading only `published_millis` was blind to it and let
    through postings first published years earlier that the site had merely
    re-fetched. The ISO string is already stored; parsing it is enough.
    """
    if job.published_millis:
        return float(job.published_millis)
    if not job.published_at:
        return None
    try:
        dt = datetime.fromisoformat(job.published_at.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp() * 1000


def _max_age(job: Job, rules: dict) -> Optional[str]:
    hours = rules.get("max_age_hours")
    millis = _published_millis(job) if hours else None
    if hours and millis:
        age_h = (time.time() * 1000 - millis) / 3_600_000
        if age_h > hours:
            return f"posted {age_h:.0f}h ago > {hours}h"
    return None


RULES: list[Rule] = [_countries, _categories, _title_include, _title_exclude, _max_yoe, _clearance, _max_age]


def apply_rules(jobs: list[Job], rules: dict[str, Any]) -> tuple[list[Job], list[tuple[Job, str]]]:
    """Split jobs into (kept, [(job, reason), ...]). Also dedups within the batch."""
    kept: list[Job] = []
    rejected: list[tuple[Job, str]] = []
    seen: set[tuple[str, str]] = set()
    for job in jobs:
        identity = (job.via, job.id)
        if identity in seen:
            rejected.append((job, "duplicate in batch"))
            continue
        seen.add(identity)
        reason = next((r for rule in RULES if (r := rule(job, rules))), None)
        if reason:
            rejected.append((job, reason))
        else:
            kept.append(job)
    return kept, rejected


def enrich_experience(jobs: list[Job], rules: dict[str, Any],
                      fetch_text: Callable[[dict[str, Any]], str],
                      log: Callable[[str], None] = lambda s: None) -> tuple[list[Job], list[tuple[Job, str]]]:
    """Second pass for sources without seniority metadata.

    For jobs from `rules.infer_yoe_from_description` (default: startup.jobs) whose
    min_yoe is unknown, read the description, infer the minimum years of
    experience, then re-apply the experience rule. Jobs from
    `rules.require_stated_yoe` that still state nothing are dropped.
    """
    infer_for = set(rules.get("infer_yoe_from_description", ["startupjobs"]) or [])
    require_for = set(rules.get("require_stated_yoe", ["startupjobs"]) or [])
    kept: list[Job] = []
    rejected: list[tuple[Job, str]] = []
    from jobFilter.descriptions import infer_min_yoe
    for job in jobs:
        if job.via in infer_for and job.min_yoe is None:
            text = fetch_text({"id": job.id, "job": job.to_dict()})
            yoe, ng = infer_min_yoe(text)
            if yoe is not None:
                job.min_yoe = yoe
                job.seniority = job.seniority or ("No Prior Experience Required" if yoe <= 1 else "Mid Level")
                job.raw = {**job.raw, "inferred_yoe": yoe, "new_grad_phrase": ng}
            reason = _max_yoe(job, rules)
            if reason:
                rejected.append((job, reason)); continue
            if job.min_yoe is None and job.via in require_for:
                rejected.append((job, "experience not stated in posting")); continue
        kept.append(job)
    return kept, rejected
