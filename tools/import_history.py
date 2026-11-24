"""Import the dashboard.html application ledger into JobAppBot's database.

Reads the `const data=[...]` array embedded in dashboard.html and creates, for
each record, one hidden `jobs` row plus one `applications` row, so that:

  * the Applications tab becomes the full application history;
  * a newly scanned posting for a company/URL you already applied to raises a
    duplicate warning carrying the prior application status;
  * none of the imported rows clutter the Jobs tab, enter the daily Excel
    export, or cost money in LLM screening.

Hiding works by setting `filter_reason`: `Store.unscreened()` skips those rows
and the frontend filters them out, while `Store.duplicate_candidates()` keeps
any row that has an application attached.

Idempotent: re-running updates existing rows instead of duplicating them.

    python -m tools.import_history [--dashboard PATH] [--db PATH] [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from jobFilter.models import Job                                  # noqa: E402
from jobFilter.store import Store                                 # noqa: E402
from jobFilter.suitability import IMPORTED_HISTORY_REASON          # noqa: E402

FILTER_REASON = IMPORTED_HISTORY_REASON
VIA = "history"

# Dashboard status -> (JobAppBot application status, is it an actual submission)
STATUS_MAP = {
    "applied": ("submitted", True),
    "rejected": ("rejected", True),
    "interview": ("submitted", True),
    "oa": ("submitted", True),
    "withdrawn": ("skipped", False),
    "to_apply": (None, False),      # never submitted: mark the job Pending instead
}

DIRECTION_CATEGORY = {
    "sde": "Software Development",
    "ai_engineer": "Data and Analytics",
    "aie": "Data and Analytics",
    "agent": "Data and Analytics",
    "graphics_cv": "Software Development",
}
BASE_LOCATION = {"usa": "United States", "china": "China"}
BASE_COUNTRY = {"usa": ["US"], "china": ["CN"]}


def extract_records(dashboard: Path) -> list[dict]:
    """Pull every record object out of the dashboard's `const data=` expression."""
    src = dashboard.read_text(encoding="utf-8")
    anchor = src.index("const data=")
    start = src.index("[", anchor)
    end = _balanced_end(src, start)
    region = src[start:end]

    records, depth, in_string, escaped, obj_start = [], 0, False, False, None
    for i, ch in enumerate(region):
        if escaped:
            escaped = False
            continue
        if in_string:
            if ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            if depth == 0:
                obj_start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                records.append(json.loads(region[obj_start:i + 1]))
    return records


def _balanced_end(src: str, start: int) -> int:
    depth, in_string, escaped = 0, False, False
    for i in range(start, len(src)):
        ch = src[i]
        if escaped:
            escaped = False
            continue
        if in_string:
            if ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch in "[{":
            depth += 1
        elif ch in "]}":
            depth -= 1
            if depth == 0:
                return i + 1
    raise ValueError("unterminated data array in dashboard")


def build_job(rec: dict) -> Job:
    base = rec.get("base") or ""
    url = (rec.get("job_url") or "").strip() or None
    job_id = "history___" + rec["id"]
    return Job(
        id=job_id, dedup_key=job_id, via=VIA,
        title=rec.get("role") or "(title not recorded)",
        company=rec.get("company") or "(company not recorded)",
        company_website=None,
        location=BASE_LOCATION.get(base),
        workplace_type=None,
        countries=BASE_COUNTRY.get(base, []),
        states=[], seniority=None, min_yoe=None,
        visa_sponsorship=None, security_clearance=None, commitment=[],
        category=DIRECTION_CATEGORY.get(rec.get("direction") or ""),
        requirements_summary=None, technical_tools=[],
        yearly_min_comp=None, yearly_max_comp=None,
        published_at=None, published_millis=None,
        apply_url=url, source=None,
        listing_url=rec.get("application_url") or url,
        raw={},
    )


def note_for(rec: dict) -> str:
    bits = [f"Imported from dashboard.html (ledger id {rec['id']})",
            f"ledger status: {rec.get('status')}"]
    for key in ("direction", "base", "source", "applied_date"):
        if rec.get(key):
            bits.append(f"{key}: {rec[key]}")
    if rec.get("next_action"):
        bits.append("next action: " + rec["next_action"])
    for line in rec.get("notes") or []:
        bits.append("note: " + line)
    if rec.get("application_url"):
        bits.append("application url: " + rec["application_url"])
    return " | ".join(bits)


def main() -> int:
    ap = argparse.ArgumentParser()
    # Relative to the repository, not to one machine: the ledger normally sits
    # beside the checkout rather than inside it.
    ap.add_argument("--dashboard", type=Path, default=ROOT.parent / "dashboard.html")
    ap.add_argument("--db", type=Path, default=ROOT / "data" / "jobs.db")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    records = extract_records(args.dashboard)
    print(f"read {len(records)} ledger records from {args.dashboard}")
    if args.dry_run:
        from collections import Counter
        print("statuses:", dict(Counter(r.get("status") for r in records)))
        return 0

    store = Store(args.db)
    jobs = [build_job(r) for r in records]
    store.upsert_many(jobs, "history-import",
                      rejection_reasons={j.id: FILTER_REASON for j in jobs})

    created = merged = apps = pending = 0
    cur = store.conn.cursor()
    for rec, job in zip(records, jobs):
        # upsert_many merges a record whose apply URL already exists, so the
        # history may have attached itself to a previously scanned posting.
        row = cur.execute("SELECT id FROM jobs WHERE id = ?", (job.id,)).fetchone()
        if row:
            job_id = job.id
            created += 1
            if rec.get("applied_date"):          # file the row under the real date
                cur.execute("UPDATE jobs SET first_seen = ?, first_seen_date = ? WHERE id = ?",
                            (rec["applied_date"] + "T12:00:00+00:00", rec["applied_date"], job_id))
        else:
            nurl = job.norm_url()
            row = cur.execute("SELECT id FROM jobs WHERE norm_url IS NOT NULL AND norm_url = ?",
                              (nurl,)).fetchone() if nurl else None
            if not row:
                print(f"  !! could not place {rec['id']}; skipped")
                continue
            job_id = row["id"]
            merged += 1

        status, submitted = STATUS_MAP.get(rec.get("status") or "", ("submitted", True))
        if status is None:                        # to_apply: not an application yet
            overrides = json.loads(cur.execute(
                "SELECT COALESCE(review_json,'{}') FROM jobs WHERE id = ?", (job_id,)).fetchone()[0])
            overrides["pending"] = True
            cur.execute("UPDATE jobs SET review_json = ? WHERE id = ?", (json.dumps(overrides), job_id))
            pending += 1
            continue

        when = (rec.get("applied_date") or "") + "T12:00:00+00:00" if rec.get("applied_date") else None
        cur.execute(
            """INSERT INTO applications (job_id, status, engine, created_at, updated_at,
                                         summary, note, page_url, submitted_at, submitted_at_estimated)
               VALUES (?,?,'manual',?,?,?,?,?,?,1)
               ON CONFLICT(job_id) DO UPDATE SET status=excluded.status, summary=excluded.summary,
                   note=excluded.note, updated_at=excluded.updated_at,
                   submitted_at=COALESCE(applications.submitted_at, excluded.submitted_at)""",
            (job_id, status, when or "", when or "",
             f"Imported history — ledger status “{rec.get('status')}”.",
             note_for(rec), rec.get("job_url") or None,
             when if submitted else None))
        apps += 1

    # A stale `to_apply` row can share a URL with the real `applied` row, which
    # merges both onto one job. Submitting clears Pending everywhere else in the
    # app (Store._clear_pending), so mirror that here.
    cleared = cur.execute(
        """UPDATE jobs SET review_json = json_remove(review_json, '$.pending')
           WHERE id IN (SELECT job_id FROM applications
                        WHERE status IN ('queued','running','submitted','rejected','already_applied','unavailable'))
             AND json_extract(review_json, '$.pending') IS NOT NULL""").rowcount

    store.conn.commit()
    print(f"pending flags cleared on jobs that were actually applied to: {cleared}")
    print(f"job rows created: {created}, merged into existing scanned jobs: {merged}")
    print(f"application rows written: {apps}, jobs marked pending (to_apply): {pending}")
    counts = store.application_counts()
    print("application status counts:", counts)
    store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
