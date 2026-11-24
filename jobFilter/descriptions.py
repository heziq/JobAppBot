"""Plain-text job descriptions without an LLM, where an ATS exposes JSON.

Used by the screening step and by the experience inference for sources that
carry no seniority metadata (startup.jobs).
"""
from __future__ import annotations

import json
import re
import urllib.parse
from typing import Any, Optional

from jobFilter.http_client import requests

from jobFilter.hiringcafe import HiringCafeClient, html_to_text


def _get_json(url: str, **kw) -> Any:
    r = requests.get(url, impersonate="chrome", timeout=60, headers={"accept": "application/json"}, **kw)
    r.raise_for_status()
    return r.json()


def fetch_description(row: dict[str, Any]) -> str:
    """Best-effort plain-text description; empty string when no server-side path exists."""
    job = row["job"]
    url = job.get("apply_url") or ""
    parsed = urllib.parse.urlparse(url)
    # YC's careers board loads job details separately, including a visa sidebar
    # that HiringCafe's description-only API drops. Prefer the original posting.
    if parsed.hostname in {"www.ycombinator.com", "ycombinator.com"} and parsed.path.rstrip("/") == "/careers":
        job_id = urllib.parse.parse_qs(parsed.query).get("job_id", [""])[0]
        if job_id.isdigit():
            try:
                r = requests.get(f"https://www.workatastartup.com/embed/y-combinator/jobs/{job_id}",
                                 impersonate="chrome", timeout=60, headers={"X-Page-Url": url})
                r.raise_for_status()
                posting = r.json()["job"]
                if str(posting["id"]) != job_id or not posting.get("description"):
                    raise ValueError("YC job details missing or mismatched")
                visa = posting.get("pretty_sponsors_visa") or "Not specified"
                return f"YC Visa Sponsorship: {visa}\n\n" + html_to_text(posting["description"])
            except Exception:
                # Do not silently replace an unverified original with aggregator text.
                return "YC Visa Sponsorship: verification unavailable\nFetch the original apply URL, including its visa sidebar."
    try:
        if job.get("via") == "hiringcafe":
            return HiringCafeClient().job_description_text(row["id"])
        m = re.match(r"https://([a-z0-9-]+)\.(wd\d+)\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?([^/?#]+)/job/(.+?)(?:\?|$)", url, re.I)
        if m:  # Workday: same path under /wday/cxs/<tenant>/<site>/job/...
            tenant, wd, site, rest = m.groups()
            d = _get_json(f"https://{tenant}.{wd}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/job/{rest}")
            return html_to_text((d.get("jobPostingInfo") or {}).get("jobDescription") or "")
        m = re.search(r"greenhouse\.io/([^/?#]+)/jobs/(\d+)", url)
        if m:
            d = _get_json(f"https://boards-api.greenhouse.io/v1/boards/{m.group(1)}/jobs/{m.group(2)}")
            return html_to_text(urllib.parse.unquote(d.get("content") or "").replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&"))
        m = re.search(r"jobs\.lever\.co/([^/?#]+)/([0-9a-f-]{36})", url)
        if m:
            d = _get_json(f"https://api.lever.co/v0/postings/{m.group(1)}/{m.group(2)}")
            return html_to_text(d.get("description") or "") + "\n" + "\n".join(html_to_text(x.get("content") or "") for x in d.get("lists") or [])
        m = re.search(r"jobs\.smartrecruiters\.com/([^/?#]+)/(\d+)", url)
        if m:
            d = _get_json(f"https://api.smartrecruiters.com/v1/companies/{m.group(1)}/postings/{m.group(2)}")
            secs = (d.get("jobAd") or {}).get("sections") or {}
            return "\n\n".join(html_to_text(v.get("text") or "") for v in secs.values() if isinstance(v, dict))
        if job.get("via") == "startupjobs" and "startup.jobs/" in url:  # JobPosting JSON-LD on the listing page
            r = requests.get(url, impersonate="chrome", timeout=60)
            for block in re.findall(r'<script type="application/ld\+json">(.*?)</script>', r.text, re.S):
                try:
                    items = json.loads(block)
                except ValueError:
                    continue
                for it in (items if isinstance(items, list) else [items]):
                    if isinstance(it, dict) and it.get("@type") == "JobPosting" and it.get("description"):
                        return html_to_text(it["description"])
            return ""
        m = re.search(r"jobs\.ashbyhq\.com/([^/?#]+)/([0-9a-f-]{36})", url)
        if m:
            d = _get_json(f"https://api.ashbyhq.com/posting-api/job-board/{m.group(1)}?includeCompensation=true")
            for jp in d.get("jobs") or []:
                if jp.get("id") == m.group(2):
                    return jp.get("descriptionPlain") or html_to_text(jp.get("descriptionHtml") or "")
    except Exception:
        return ""
    return ""




# ----- stated sponsorship refusals -------------------------------------------------
# Phrase list ported from career-ops scan.mjs `visaRefusalPhrases()` (MIT), which
# measured it over 7,168 public Greenhouse/Ashby/Lever postings on 2026-11-19.
#
# This only catches a posting that SAYS it will not sponsor. It can never catch
# the other reason a screening lands on "unlikely" -- a company with no H-1B
# filing history -- because that needs a web search, not a regex. Backtested
# against this install's own first 40 screenings it matched 2, so treat it as a
# small saving on an explicit subset, not as a replacement for screening.
def _refusal_phrases() -> tuple[str, ...]:
    sponsorship = ("sponsorship", "visa sponsorship", "work visa sponsorship")
    any_sponsorship = sponsorship + ("company sponsorship", "employer sponsorship")
    cross = lambda prefixes, objects: [f"{p} {o}" for p in prefixes for o in objects]
    phrases = cross(
        [f"{neg} {verb}"
         for neg in ("not", "n't", "unable to", "not able to", "not currently able to", "not in a position to")
         for verb in ("offer", "provide", "support", "sponsor")],
        sponsorship)
    # "work authorization that does not require sponsorship", "must not require
    # visa sponsorship now or in the future". Only in these framings: a bare
    # "not require sponsorship" would also drop "we sponsor visas and welcome
    # those who do not require sponsorship", which is an offer, not a refusal.
    phrases += cross(
        [f"{a} that does not require" for a in ("authorization", "authorisation")]
        + [f"{a} that doesn't require" for a in ("authorization", "authorisation")]
        + ["must not require", "must not now or in the future require",
           "does not require", "will not require"],
        any_sponsorship)
    phrases += cross(("without requiring", "without needing", "without the need for"), any_sponsorship)
    phrases += [
        "without visa sponsorship", "without sponsorship",
        "not eligible for sponsorship", "not eligible for visa sponsorship",
        "ineligible for sponsorship", "ineligible for visa sponsorship",
        "sponsorship is not offered", "sponsorship not offered",
        "sponsorship is not provided", "sponsorship not provided",
        "sponsorship is not available", "sponsorship not available",
        "sponsorship will not be offered", "sponsorship will not be provided",
        "sponsorship cannot be offered", "sponsorship cannot be provided",
        "not sponsoring for work authorization", "not sponsoring work visas",
        "cannot sponsor", "can not sponsor", "unable to sponsor", "do not sponsor",
        "does not sponsor", "will not sponsor", "no visa sponsorship",
    ]
    return tuple(dict.fromkeys(phrases))


REFUSAL_PHRASES = _refusal_phrases()
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?;])\s+|\n+")


def _fold(text: str) -> str:
    """Lowercase, straighten curly apostrophes so "n't" covers "can't"/"won't"."""
    return re.sub(r"\s+", " ", (text or "").replace("’", "'").replace("‘", "'").lower())


def stated_refusal(text: str) -> Optional[str]:
    """The sentence in which the posting states it will not sponsor, or None.

    The matched SENTENCE is returned rather than a boolean so the decision can
    be shown as evidence and checked by hand: this is used to skip a paid
    screening, and an unexplained rejection is worse than the money it saves.
    """
    if not text:
        return None
    for sentence in _SENTENCE_SPLIT.split(text):
        folded = _fold(sentence)
        if not folded or "sponsor" not in folded:
            continue
        if any(p in folded for p in REFUSAL_PHRASES):
            return " ".join(sentence.split())[:300]
    return None


# ----- years-of-experience inference -----------------------------------------------
_YOE = re.compile(r"(\d{1,2})\s*(?:\+|\s*(?:-|–|to)\s*\d{1,2})?\s*\+?\s*(?:years?|yrs?)\b[^.\n]{0,60}?\bexperience"
                  r"|experience[^.\n]{0,60}?\b(\d{1,2})\s*\+?\s*(?:years?|yrs?)\b", re.I)
_NEW_GRAD = re.compile(r"new\s*grad|recent(?:ly)?\s*graduat|entry[- ]level|early[- ]career|no (?:prior )?experience(?: required| necessary)?"
                       r"|0\s*(?:-|–|to)\s*[12]\s*(?:years?|yrs?)|university graduate|graduating (?:in|by|class of)|class of 20\d\d", re.I)


def infer_min_yoe(text: str) -> tuple[Any, bool]:
    """Return (min years of experience or None, new_grad_phrase_found).

    Takes the smallest number that appears next to the word "experience"; a
    new-grad phrase forces 0. None when the text states nothing.
    """
    if not text:
        return None, False
    ng = bool(_NEW_GRAD.search(text))
    years = [int(a or b) for a, b in _YOE.findall(text)]
    years = [y for y in years if y <= 20]
    if ng:
        return 0, True
    return (min(years) if years else None), False
