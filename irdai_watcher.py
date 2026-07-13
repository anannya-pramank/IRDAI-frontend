#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
IRDAI Watcher v2
----------------
Upgrades over v1 (Bhanu Tak's clean-row-filtering build):

  COVERAGE   All 11 Legal-menu buckets (Acts .. Exposure Drafts), incl. the
             Consolidated & Updated Regulations version buckets and
             Notifications (distinct from Notices).
  PAGINATION Explicit Liferay delta/cur paging. Incremental mode reads the
             first page (20 rows) per category; --backfill walks every page.
  METADATA   Reference-number parsing (department vertical, year), archive
             flag column, ALL attachments per issuance (not just the first),
             ISO dates, stable slugs.
  RELATIONS  Post-pass mines withdrawal circulars ("Withdrawal of Circular
             No. X"), amendment instruments ("(Nth Amendment)"), and
             modification circulars, resolving them against the corpus into
             withdraws/amends/modifies edges where a target is found.
  STATE      Upserting JSON corpus (data/corpus.json) keyed by slug, plus the
             original append-only CSV log and a per-run new-entries JSON for
             the notification hook.
  WIKI       Emits data/docs.generated.js -> window.IRDAI_DOCS in the exact
             DOCS schema of irdai-repository-wiki-v3.html. New docs land as
             status "Unclassified" with maintenance flags ["stub","verify"],
             i.e. they surface in the wiki's review queue for human
             classification (two-tier gate: machine ingests, human classifies).
  RESILIENCE Retry/backoff session, per-category error isolation, polite
             delay, UTC timestamps, logging instead of prints.

Usage:
  python irdai_watcher.py                 # incremental (first page/category)
  python irdai_watcher.py --backfill      # walk all pages per category
  python irdai_watcher.py --pages 3       # incremental depth of 3 pages
  python irdai_watcher.py --categories Circulars Orders
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ================= CONFIG =================

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)

MASTER_CSV = DATA_DIR / "irdai_master.csv"
CORPUS_JSON = DATA_DIR / "corpus.json"
NEW_JSON = DATA_DIR / "irdai_new_entries.json"
WIKI_JS = DATA_DIR / "docs.generated.js"

BASE_URL = "https://irdai.gov.in"

# Full Legal-menu map. Slugs for the buckets beyond the four originals are
# best-effort friendly URLs; a wrong slug logs a warning for that category
# only and never kills the run — verify once against the live site.
PAGES = {
    "Acts": "/acts",
    "Rules": "/rules",
    "Regulations": "/regulations",
    "Consolidated & Gazette Notified Regulations": "/consolidated-gazette-notified-regulations",
    "Updated Regulations": "/updated-regulations",
    "Notifications": "/notifications",
    "Circulars": "/circulars",
    "Guidelines": "/guidelines",
    "Orders": "/orders",
    "Notices": "/notices",
    "Exposure Drafts": "/exposure-drafts",
}

PORTLET_NS = "_com_irdai_document_media_IRDAIDocumentMediaPortlet_"
DELTA = 20
BACKFILL_MAX_PAGES = 250

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}

CSV_FIELDS = [
    "id", "slug", "category", "type", "title", "reference_no", "dept",
    "year", "date_issued", "archived", "attachments", "detail_page",
    "pdf_links", "file_sizes", "source_page", "first_seen", "scraped_at",
]

# Department verticals encoded in IRDAI reference numbers
# (e.g. IRDAI/HLT/CIR/PRO/84/5/2024 -> HLT).
DEPT_CODES = {
    "NL": "Non-Life", "HLT": "Health", "F&I": "Finance & Investment",
    "F&A": "Finance & Accounts", "ACTL": "Actuarial", "ACT": "Actuarial",
    "LGL": "Legal", "LEGAL": "Legal", "REIN": "Reinsurance",
    "PP&GR": "Policyholder Protection & GR", "PPGR": "Policyholder Protection & GR",
    "INT": "Intermediaries", "LIFE": "Life", "GA&HR": "General Admin & HR",
    "GAHR": "General Admin & HR", "SDD": "Supervision", "IT": "Information Technology",
}
DEPT_ALIAS = {"PPGR": "PP&GR", "GAHR": "GA&HR", "ACT": "ACTL", "LEGAL": "LGL", "F&A": "F&I"}

# Entity / subtype / portal-subject hints from title keywords.
# Order matters: first hit wins. These are HINTS ONLY - every auto-ingested
# doc still carries maintenance flags and lands in the review queue.
ENTITY_HINTS = [
    (r"\bbroker", "Intermediaries", "Insurance Brokers", ["distribution"]),
    (r"\bcorporate agent", "Intermediaries", "Corporate Agents", ["distribution"]),
    (r"\bweb aggregator", "Intermediaries", "Web Aggregators", ["distribution"]),
    (r"\bsurveyor|loss assessor", "Intermediaries", "Surveyors & Loss Assessors", ["distribution"]),
    (r"\bthird party administrator|\btpa\b", "Intermediaries", "Third Party Administrators (TPAs)", ["health"]),
    (r"insurance marketing firm|\bimf\b", "Intermediaries", "Insurance Marketing Firms (IMFs)", ["distribution"]),
    (r"\brepositor", "Intermediaries", "Insurance Repositories", ["distribution"]),
    (r"\bmisp\b|motor insurance service provider", "Intermediaries", "MISPs", ["distribution"]),
    (r"\breinsur|lloyd", "Insurers", "Reinsurers", ["reinsurance"]),
    (r"standalone health|health insur", "Insurers", "Standalone Health Insurers", ["health"]),
    (r"life insur", "Insurers", "Life Insurers", ["governance"]),
    (r"general insur", "Insurers", "General Insurers", ["governance"]),
    (r"\binsurer", "Insurers", "—", ["governance"]),
    (r"corporate governance|advertis|grievance|policyholder", "Common",
     "Applicable across multiple regulated entities", ["governance"]),
]

TYPE_SLUG_PREFIX = {
    "Acts": "act", "Rules": "rules", "Regulations": "reg", "Notifications": "not",
    "Master Circulars": "mc", "Circulars": "cir", "Guidelines": "gl",
    "Orders": "ord", "Notices": "ntc", "Exposure Drafts": "ed",
}

log = logging.getLogger("irdai_watcher")

# ================= HTTP =================


def make_session() -> requests.Session:
    s = requests.Session()
    retry = Retry(
        total=4, backoff_factor=1.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET",),
    )
    s.mount("https://", HTTPAdapter(max_retries=retry))
    s.headers.update(HEADERS)
    return s


def fetch_page(session: requests.Session, url: str, cur: int = 1) -> str:
    params = {PORTLET_NS + "delta": str(DELTA), PORTLET_NS + "cur": str(cur)}
    r = session.get(url, params=params, timeout=40)
    r.raise_for_status()
    return r.text


# ================= PARSING =================


def parse_date(raw: str) -> str | None:
    """'19-06-2024' / '19/06/2024' -> '2024-06-19'."""
    if not raw:
        return None
    m = re.search(r"(\d{1,2})[-/](\d{1,2})[-/](\d{4})", raw)
    if not m:
        return None
    d, mo, y = m.groups()
    return f"{y}-{int(mo):02d}-{int(d):02d}"


def parse_refno(ref: str) -> tuple[str | None, int | None]:
    """Extract (dept_code, year) from an IRDAI reference number."""
    dept = None
    year = None
    if not ref:
        return dept, year
    tokens = [t.strip().upper() for t in ref.split("/")]
    for tok in tokens[1:4]:
        if tok in DEPT_CODES:
            dept = DEPT_ALIAS.get(tok, tok)
            break
    years = [int(t) for t in re.findall(r"\b(19\d{2}|20\d{2})\b", ref)]
    if years:
        year = years[-1]
    return dept, year


def slugify(text: str, max_len: int = 44) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:max_len].rstrip("-")


def norm_ref(ref: str) -> str:
    return re.sub(r"\s+", "", (ref or "")).upper().strip(".")


def norm_title(t: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (t or "").lower())


def parse_rows(html: str, category: str, source_url: str) -> tuple[list[dict], int]:
    """Return (valid rows, raw row count). Raw count drives pagination."""
    soup = BeautifulSoup(html, "html.parser")
    table = soup.select_one("table.table")
    if not table:
        return [], 0

    rows = table.select("tbody tr")
    results = []

    for tr in rows:
        tds = tr.find_all("td")
        if len(tds) < 7:
            continue

        archived_txt = tds[1].get_text(strip=True)
        archived = None
        if archived_txt in ("Archived", "Non-Archived"):
            archived = archived_txt == "Archived"

        title = tds[2].get_text(strip=True)
        date_issued = parse_date(tds[3].get_text(strip=True))

        detail_link = None
        a = tds[4].select_one("a[href]")
        if a:
            detail_link = urljoin(source_url, a["href"])

        ref = tds[5].get_text(strip=True)

        # ALL attachments in the download cell (the "+3 more" nuance):
        pdf_links, filenames, sizes = [], [], []
        for pa in tds[6].select("a[href*='download=true']"):
            pdf_links.append(urljoin(source_url, pa["href"]))
            filenames.append(pa.get_text(strip=True))
        for sp in tds[6].select("p.text-muted"):
            sizes.append(sp.get_text(strip=True))

        if not title or not (detail_link or pdf_links):
            continue

        checkbox = tr.select_one("input.checkSingle")
        liferay_id = checkbox["value"] if checkbox and checkbox.get("value") else None
        raw_id = liferay_id or f"sha1:{__import__('hashlib').sha1((category + '|' + title + '|' + ref).encode()).hexdigest()}"

        results.append({
            "liferay_id": raw_id,
            "category": category,
            "title": title,
            "reference_no": ref,
            "date_issued": date_issued,
            "archived": archived,
            "detail_page": detail_link,
            "pdf_links": pdf_links,
            "pdf_filenames": filenames,
            "file_sizes": sizes,
            "source_page": source_url,
        })

    return results, len(rows)


# ================= CLASSIFICATION =================


def curated_type(category: str, title: str) -> str:
    """Repository type vs IRDAI source category.

    - Master Circulars live under Circulars on the site; the repository
      elevates them to a first-class type.
    - Exposure drafts occasionally surface in other buckets by title.
    - The two regulations version buckets both map to type Regulations;
      the bucket itself is preserved in sourceCategory + versions.
    """
    tl = title.lower()
    if "master circular" in tl:
        return "Master Circulars"
    if "exposure draft" in tl:
        return "Exposure Drafts"
    if category in ("Consolidated & Gazette Notified Regulations", "Updated Regulations"):
        return "Regulations"
    return category


def entity_guess(title: str) -> tuple[str, str, list[str]]:
    tl = title.lower()
    for pat, entity, subtype, subjects in ENTITY_HINTS:
        if re.search(pat, tl):
            return entity, subtype, subjects
    return "Common", "—", []


def to_wiki_record(raw: dict, now_iso: str) -> dict:
    dept, ref_year = parse_refno(raw["reference_no"])
    year = ref_year
    if not year and raw["date_issued"]:
        year = int(raw["date_issued"][:4])
    if not year:
        year = datetime.now(timezone.utc).year

    doc_type = curated_type(raw["category"], raw["title"])
    entity, subtype, subjects = entity_guess(raw["title"])

    rec = {
        "type": doc_type,
        "title": raw["title"],
        "entity": entity,
        "subtype": subtype,
        "status": "Unclassified",
        "year": year,
        "dept": dept,
        "refNo": raw["reference_no"] or None,
        "dateIssued": raw["date_issued"],
        "archived": bool(raw["archived"]) if raw["archived"] is not None else False,
        "sourceCategory": raw["category"],
        "aliases": [],
        "subjects": subjects,
        "attachments": max(len(raw["pdf_links"]), 1),
        "maintenance": ["stub", "verify"],
        "lede": raw["title"] + " — auto-ingested; entity classification and relationship mapping pending human review.",
        "history": f"Auto-created by irdai_watcher on {now_iso[:10]}. Classification pending.",
        "relations": {},
        "_source": {
            "liferay_id": raw["liferay_id"],
            "detail_page": raw["detail_page"],
            "pdf_links": raw["pdf_links"],
            "pdf_filenames": raw["pdf_filenames"],
            "file_sizes": raw["file_sizes"],
            "source_page": raw["source_page"],
            "first_seen": now_iso,
            "last_seen": now_iso,
        },
    }

    # Version bucket preserved for the wiki's dual-publication chips.
    if raw["category"] in ("Consolidated & Gazette Notified Regulations", "Updated Regulations"):
        rec["versions"] = [{
            "label": raw["title"][:80],
            "bucket": raw["category"],
            "date": raw["date_issued"] or f"{year}-01-01",
        }]

    return rec


# ================= RELATION MINING (post-pass) =================

WITHDRAW_RE = re.compile(
    r"withdrawal\s+of\s+circular\s+no\.?\s*([A-Z0-9/&\.\- ]+?)(?:\s+dated\b|,|$)", re.I)
AMEND_RE = re.compile(
    r"\(\s*(?:first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth|\d+(?:st|nd|rd|th))\s+amendment\s*\)", re.I)
MODIFY_RE = re.compile(
    r"modificat\w*\s+(?:to|in|of)\s+(?:the\s+)?(.+?)(?:\s+dated\b|$)", re.I)


def mine_relations(corpus: dict) -> int:
    """Resolve withdraws / amends / modifies edges across the corpus.

    Only sets relations where a target resolves; anything unresolved is
    recorded in _source.pending_relations for the human pass. Never sets
    status on the target — that stays a human call in the review queue.
    """
    by_ref = {norm_ref(d.get("refNo")): sid for sid, d in corpus.items() if d.get("refNo")}
    titles = {sid: norm_title(d["title"]) for sid, d in corpus.items()}
    edges = 0

    for sid, d in corpus.items():
        title = d["title"]
        rel = d.setdefault("relations", {})
        pending = d["_source"].setdefault("pending_relations", [])

        m = WITHDRAW_RE.search(title)
        if m:
            target = by_ref.get(norm_ref(m.group(1)))
            if target and target != sid:
                if target not in rel.setdefault("withdraws", []):
                    rel["withdraws"].append(target)
                    corpus[target].setdefault("relations", {}).setdefault("withdrawnBy", [])
                    if sid not in corpus[target]["relations"]["withdrawnBy"]:
                        corpus[target]["relations"]["withdrawnBy"].append(sid)
                    edges += 1
            elif f"withdraws:{m.group(1).strip()}" not in pending:
                pending.append(f"withdraws:{m.group(1).strip()}")

        if AMEND_RE.search(title):
            base = norm_title(AMEND_RE.sub("", title))
            base = re.sub(r"(19|20)\d{2}$", "", base)
            cands = [oid for oid, nt in titles.items()
                     if oid != sid and nt and (nt in base or base in nt)
                     and not AMEND_RE.search(corpus[oid]["title"])]
            if cands:
                target = max(cands, key=lambda o: len(titles[o]))
                if target not in rel.setdefault("amends", []):
                    rel["amends"].append(target)
                    corpus[target].setdefault("relations", {}).setdefault("amendedBy", [])
                    if sid not in corpus[target]["relations"]["amendedBy"]:
                        corpus[target]["relations"]["amendedBy"].append(sid)
                    edges += 1
            elif "amends:?" not in pending:
                pending.append("amends:?")

        m = MODIFY_RE.search(title)
        if m and not AMEND_RE.search(title):
            frag = norm_title(m.group(1))
            cands = [oid for oid, nt in titles.items()
                     if oid != sid and frag and (frag in nt or nt in frag)]
            if cands:
                target = max(cands, key=lambda o: len(titles[o]))
                if target not in rel.setdefault("modifies", []):
                    rel["modifies"].append(target)
                    corpus[target].setdefault("relations", {}).setdefault("modifiedBy", [])
                    if sid not in corpus[target]["relations"]["modifiedBy"]:
                        corpus[target]["relations"]["modifiedBy"].append(sid)
                    edges += 1
            elif f"modifies:{m.group(1)[:60]}" not in pending:
                pending.append(f"modifies:{m.group(1)[:60]}")

    return edges


# ================= STATE =================


def load_corpus() -> dict:
    if CORPUS_JSON.exists():
        with open(CORPUS_JSON, encoding="utf-8") as f:
            return json.load(f).get("docs", {})
    return {}


def save_corpus(corpus: dict) -> None:
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "count": len(corpus),
        "docs": corpus,
    }
    with open(CORPUS_JSON, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def write_wiki_js(corpus: dict) -> None:
    """Emit window.IRDAI_DOCS for irdai-repository-wiki-v3.html."""
    body = json.dumps(corpus, ensure_ascii=False, indent=2)
    header = (
        "/* Auto-generated by irdai_watcher.py — do not edit by hand.\n"
        f"   Generated {datetime.now(timezone.utc).isoformat()} · {len(corpus)} docs. */\n"
    )
    with open(WIKI_JS, "w", encoding="utf-8") as f:
        f.write(header + "window.IRDAI_DOCS = " + body + ";\n")


def make_slug(rec: dict, corpus: dict, liferay_id: str) -> str:
    prefix = TYPE_SLUG_PREFIX.get(rec["type"], "doc")
    base = f"{prefix}-{slugify(rec['title'])}-{rec['year']}"
    slug = base
    n = 2
    while slug in corpus and corpus[slug]["_source"].get("liferay_id") != liferay_id:
        slug = f"{base}-{n}"
        n += 1
    return slug


def append_csv(rows: list[dict]) -> None:
    exists = MASTER_CSV.exists()
    with open(MASTER_CSV, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
        if not exists:
            w.writeheader()
        w.writerows(rows)


# ================= MAIN =================


def run(args) -> None:
    session = make_session()
    corpus = load_corpus()
    by_source = {d["_source"].get("liferay_id"): sid for sid, d in corpus.items()
                 if d.get("_source", {}).get("liferay_id")}

    now_iso = datetime.now(timezone.utc).isoformat()
    new_entries = []
    csv_rows = []

    categories = {k: v for k, v in PAGES.items()
                  if not args.categories or k in args.categories}

    for category, path in categories.items():
        url = urljoin(BASE_URL, path)
        max_pages = BACKFILL_MAX_PAGES if args.backfill else args.pages
        cur, seen_here = 1, 0
        run_ids: set = set()  # row-IDs seen in THIS category walk (clamp detector)

        while cur <= max_pages:
            try:
                html = fetch_page(session, url, cur=cur)
            except requests.RequestException as e:
                log.warning("%s: fetch failed on page %d — %s (category skipped, run continues)",
                            category, cur, e)
                break

            rows, raw_count = parse_rows(html, category, url)

            # Liferay clamps out-of-range `cur` to the last page (or ignores
            # the param entirely and re-serves page 1), so raw_count never
            # drops below DELTA at the true end of a listing. A page whose
            # row-IDs were all already seen in this walk is a repeat — stop.
            page_ids = {r["liferay_id"] for r in rows}
            if cur > 1 and page_ids and page_ids <= run_ids:
                log.info("%s p%d: pagination clamp detected (repeated page), stopping walk",
                         category, cur)
                break
            run_ids |= page_ids
            seen_here += len(rows)

            for raw in rows:
                lid = raw["liferay_id"]
                if lid in by_source:
                    src = corpus[by_source[lid]]["_source"]
                    src["last_seen"] = now_iso
                    # Liferay download URLs embed a token (?version=&t=) that
                    # can go stale; refresh volatile fields on every sighting
                    # so the loader never fetches from a first-seen snapshot.
                    src["pdf_links"] = raw["pdf_links"]
                    src["pdf_filenames"] = raw["pdf_filenames"]
                    src["file_sizes"] = raw["file_sizes"]
                    src["detail_page"] = raw["detail_page"]
                    continue

                rec = to_wiki_record(raw, now_iso)
                slug = make_slug(rec, corpus, lid)
                corpus[slug] = rec
                by_source[lid] = slug
                new_entries.append({"slug": slug, **rec})
                csv_rows.append({
                    "id": lid, "slug": slug, "category": category,
                    "type": rec["type"], "title": rec["title"],
                    "reference_no": rec["refNo"], "dept": rec["dept"],
                    "year": rec["year"], "date_issued": rec["dateIssued"],
                    "archived": rec["archived"], "attachments": rec["attachments"],
                    "detail_page": raw["detail_page"],
                    "pdf_links": json.dumps(raw["pdf_links"], ensure_ascii=False),
                    "file_sizes": json.dumps(raw["file_sizes"], ensure_ascii=False),
                    "source_page": url, "first_seen": now_iso, "scraped_at": now_iso,
                })

            log.info("%s p%d: raw=%d valid=%d (cumulative %d)",
                     category, cur, raw_count, len(rows), seen_here)

            if raw_count == 0:
                log.info("%s p%d: empty page, stopping walk", category, cur)
                break
            if raw_count < DELTA:
                break
            cur += 1
            time.sleep(args.delay)

        time.sleep(args.delay)

    edges = mine_relations(corpus)

    save_corpus(corpus)
    write_wiki_js(corpus)
    if csv_rows:
        append_csv(csv_rows)
    with open(NEW_JSON, "w", encoding="utf-8") as f:
        json.dump(new_entries, f, ensure_ascii=False, indent=2)

    log.info("Done: %d new, corpus=%d, relation edges resolved=%d",
             len(new_entries), len(corpus), edges)
    log.info("Wiki payload: %s (include via <script src=\"data/docs.generated.js\">)", WIKI_JS)


def main() -> None:
    p = argparse.ArgumentParser(description="IRDAI Legal-section watcher v2")
    p.add_argument("--backfill", action="store_true",
                   help="walk all pages per category (default: incremental)")
    p.add_argument("--pages", type=int, default=1,
                   help="pages per category in incremental mode (default 1 = top 20)")
    p.add_argument("--delay", type=float, default=1.5,
                   help="seconds between requests (default 1.5)")
    p.add_argument("--categories", nargs="*", default=None,
                   help="restrict to named categories")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    run(args)


if __name__ == "__main__":
    main()
