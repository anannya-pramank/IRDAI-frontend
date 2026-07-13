#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
IRDAI Loader — corpus.json -> Supabase (irdai_documents + irdai_chunks)
-----------------------------------------------------------------------
The CERC/APTEL pattern, IRDAI edition:

  - Reads data/corpus.json produced by irdai_watcher.py (the watcher stays
    network-light; this script owns all heavy lifting).
  - Metadata upsert for EVERY doc on every run (cheap, keeps wiki fields,
    relations, and status edits in sync).
  - PDFs are fetched INTO MEMORY only — extracted with pymupdf4llm
    (fallback: plain pymupdf get_text), then the bytes are discarded.
    Nothing is written to disk, nothing is committed to git.
  - content_sha1 gate: a document is extracted + embedded exactly once;
    re-runs skip it unless the text changes or --refresh is passed.
  - Chunking: heading-aware markdown split, packed to ~1800 chars with
    200-char overlap; each chunk keeps its nearest heading and source
    attachment filename (multi-attachment issuances).
  - Embeddings: sentence-transformers all-MiniLM-L6-v2 (384-dim,
    normalized for cosine), lazy-loaded, batch-encoded.
  - Upserts via the Supabase transaction pooler (port 6543), psycopg2 +
    execute_values, ON CONFLICT DO UPDATE.

Env:
  SUPABASE_DB_URL  postgresql://postgres.<ref>:<pw>@<region>.pooler.supabase.com:6543/postgres

Usage:
  python load_irdai.py                 # metadata sync + extract/embed new docs
  python load_irdai.py --skip-pdf      # metadata sync only
  python load_irdai.py --refresh       # force re-extract + re-embed everything
  python load_irdai.py --only <slug>   # single document
  python load_irdai.py --limit 25      # cap extraction work per run
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
import psycopg2
from psycopg2.extras import execute_values, Json

BASE_DIR = Path(__file__).resolve().parent
CORPUS_JSON = BASE_DIR / "data" / "corpus.json"

MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"
EMBED_DIM = 384
CHUNK_CHARS = 1800
CHUNK_OVERLAP = 200
MAX_PDF_MB = 40
DOWNLOAD_TIMEOUT = 60
# Above this page count, skip pymupdf4llm's layout analysis (0.5-2 s/page on
# runner CPUs) and use plain text extraction — statutes and consolidated Acts
# need grep/embedding quality, not table fidelity. Override with --md-max-pages.
MD_MAX_PAGES = 120

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}

log = logging.getLogger("load_irdai")

# Heavy deps are lazy so --skip-pdf / --dry-run runs never import them.
_model = None
_fitz = None
_pymupdf4llm = None


def get_model():
    global _model
    if _model is None:
        log.info("Loading %s (lazy)…", MODEL_NAME)
        from sentence_transformers import SentenceTransformer
        _model = SentenceTransformer(MODEL_NAME)
    return _model


def get_pdf_libs():
    global _fitz, _pymupdf4llm
    if _fitz is None:
        import fitz  # pymupdf
        _fitz = fitz
        try:
            import pymupdf4llm
            _pymupdf4llm = pymupdf4llm
        except ImportError:
            _pymupdf4llm = None
            log.warning("pymupdf4llm unavailable — falling back to plain pymupdf text")
    return _fitz, _pymupdf4llm


# ================= EXTRACTION (in-memory only) =================


def fetch_pdf_bytes(session: requests.Session, url: str) -> bytes | None:
    for attempt in (1, 2, 3):
        try:
            r = session.get(url, timeout=DOWNLOAD_TIMEOUT, stream=True)
            r.raise_for_status()
            size = int(r.headers.get("content-length") or 0)
            if size and size > MAX_PDF_MB * 1024 * 1024:
                log.warning("skip oversized PDF (%.1f MB): %s", size / 1e6, url)
                return None
            data = r.content
            if len(data) > MAX_PDF_MB * 1024 * 1024:
                log.warning("skip oversized PDF (%.1f MB): %s", len(data) / 1e6, url)
                return None
            return data
        except requests.RequestException as e:
            log.warning("fetch attempt %d failed for %s — %s", attempt, url, e)
            time.sleep(2 * attempt)
    return None


def extract_text(pdf_bytes: bytes) -> tuple[str, str, int]:
    """Return (text, method, page_count). Bytes never touch disk.

    Two pymupdf4llm profiles:
      full — default; identical call profile to CERC/APTEL (tables kept).
      lean — for docs over MD_MAX_PAGES: markdown headings preserved for the
             chunker, but table detection off and drawing analysis capped.
             This targets gazette-style PDFs (bilingual consolidated Acts,
             watermarked pages) whose per-page graphics make lines_strict
             table detection pathological — the cost centre APTEL's clean
             digitally-born orders never hit.
    """
    fitz, pymupdf4llm = get_pdf_libs()
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        pages = doc.page_count
        if pymupdf4llm is not None:
            lean = pages > MD_MAX_PAGES
            try:
                t0 = time.time()
                if lean:
                    try:
                        md = pymupdf4llm.to_markdown(
                            doc, table_strategy=None, graphics_limit=5000)
                        method = "pymupdf4llm-lean"
                    except TypeError:
                        # older pymupdf4llm without these kwargs
                        md = pymupdf4llm.to_markdown(doc)
                        method = "pymupdf4llm"
                else:
                    md = pymupdf4llm.to_markdown(doc)
                    method = "pymupdf4llm"
                log.debug("to_markdown: %d pages in %.1fs (%s)",
                          pages, time.time() - t0, method)
                if md and md.strip():
                    return md, method, pages
            except Exception as e:
                log.warning("pymupdf4llm failed (%s) — plain-text fallback", e)
        text = "\n\n".join(p.get_text("text") for p in doc)
        return text, "pymupdf", pages
    finally:
        doc.close()


def extract_issuance(session: requests.Session, rec: dict, delay: float) -> dict | None:
    """Fetch + extract ALL attachments of one issuance, in memory."""
    src = rec.get("_source", {})
    links = src.get("pdf_links") or []
    names = src.get("pdf_filenames") or []
    if not links:
        return None

    parts, methods, total_pages = [], set(), 0
    for i, url in enumerate(links):
        t0 = time.time()
        data = fetch_pdf_bytes(session, url)
        time.sleep(delay)
        if not data:
            continue
        fetch_s = time.time() - t0
        size_mb = len(data) / 1e6
        t0 = time.time()
        text, method, pages = extract_text(data)
        del data  # explicit: bytes discarded here
        label = names[i] if i < len(names) and names[i] else f"attachment-{i + 1}"
        log.info("  %s: %.1f MB fetched in %.1fs; %d pages extracted in %.1fs (%s)",
                 label, size_mb, fetch_s, pages, time.time() - t0, method)
        methods.add(method)
        total_pages += pages
        parts.append((label, text))

    if not parts:
        return None

    if len(parts) == 1:
        full_text = parts[0][1]
    else:
        full_text = "\n\n".join(f"# [Attachment: {label}]\n\n{text}" for label, text in parts)

    return {
        "full_text": full_text,
        "content_sha1": hashlib.sha1(full_text.encode("utf-8")).hexdigest(),
        "extraction_method": "+".join(sorted(methods)),
        "page_count": total_pages,
        "parts": parts,
    }


# ================= CHUNKING =================

# Headings come in two flavours: markdown (#) from pymupdf4llm's layout pass,
# and statute-style bold lines ('**3. Registration.** — (1) ...',
# '**CHAPTER IV**') which is how Indian Acts/Regulations actually render.
HEADING_RE = re.compile(
    r"^(?:"
    r"#{1,4}\s+(?P<md>[^\n]+)"
    r"|\*\*(?P<sec>\d{1,3}[A-Z]{0,3}\.\s[^*\n]{2,140}?)\.?\*\*"
    r"|\*\*(?P<chp>(?:CHAPTER|PART|SCHEDULE)\s+[IVXLCM\d][^*\n]{0,100}?)\*\*"
    r")",
    re.M,
)

DEVANAGARI_RE = re.compile(r"[\u0900-\u097F]")


def devanagari_ratio(s: str) -> float:
    return len(DEVANAGARI_RE.findall(s)) / max(len(s), 1)


def _heading_of(m: re.Match) -> str:
    return (m.group("md") or m.group("sec") or m.group("chp")).strip()[:180]


def _snap(body: str, start: int, end: int) -> tuple[int, int]:
    """Nudge chunk boundaries to sentence breaks (., ।) where one is near."""
    if start > 0:
        m = re.search(r"[.।]\s+", body[start:start + 250])
        if m:
            start = start + m.end()
    if end < len(body):
        m = re.search(r"[.।]\s", body[end:end + 200])
        if m:
            end = end + m.end()
    return start, end


def chunk_text(text: str, attachment: str | None = None) -> list[dict]:
    """Heading-aware packing: split on markdown AND statute-bold headings,
    pack sections to ~CHUNK_CHARS with CHUNK_OVERLAP carry-over, snapping
    boundaries to sentence breaks. Each chunk records its nearest heading."""
    sections: list[tuple[str | None, str]] = []
    last = 0
    heading = None
    for m in HEADING_RE.finditer(text):
        body = text[last:m.start()].strip()
        if body:
            sections.append((heading, body))
        heading = _heading_of(m)
        last = m.end()
    tail = text[last:].strip()
    if tail:
        sections.append((heading, tail))
    if not sections:
        sections = [(None, text.strip())]

    chunks = []
    for heading, body in sections:
        raw_start = 0
        while raw_start < len(body):
            s, e = _snap(body, raw_start, min(raw_start + CHUNK_CHARS, len(body)))
            piece = body[s:e]
            if piece.strip():
                chunks.append({
                    "heading": heading,
                    "attachment": attachment,
                    "content": piece.strip(),
                })
            if raw_start + CHUNK_CHARS >= len(body):
                break
            raw_start += CHUNK_CHARS - CHUNK_OVERLAP
    return chunks


def chunk_issuance(extraction: dict) -> list[dict]:
    parts = extraction["parts"]
    out = []
    for label, text in parts:
        out.extend(chunk_text(text, attachment=label if len(parts) > 1 else None))
    for i, c in enumerate(out):
        c["chunk_index"] = i
    return out


# ================= DB =================


def connect():
    dsn = os.environ.get("SUPABASE_DB_URL")
    if not dsn:
        log.error("SUPABASE_DB_URL not set")
        sys.exit(1)
    conn = psycopg2.connect(dsn)
    conn.autocommit = False
    with conn.cursor() as cur:
        cur.execute("set statement_timeout = '120s'")
    return conn


DOC_COLS = [
    "id", "liferay_id", "type", "source_category", "title", "ref_no", "dept",
    "entity", "subtype", "status", "year", "date_issued", "archived",
    "subjects", "maintenance", "aliases", "relations", "pending_relations",
    "attachments", "detail_page", "pdf_links", "source_page",
    "first_seen", "last_seen",
]

DOC_UPSERT = f"""
insert into irdai_documents ({", ".join(DOC_COLS)})
values %s
on conflict (id) do update set
  {", ".join(f"{c} = excluded.{c}" for c in DOC_COLS if c not in ("id", "status", "entity", "subtype"))},
  -- human-edited classification fields are preserved unless still Unclassified:
  status  = case when irdai_documents.status  = 'Unclassified' then excluded.status  else irdai_documents.status  end,
  entity  = case when irdai_documents.status  = 'Unclassified' then excluded.entity  else irdai_documents.entity  end,
  subtype = case when irdai_documents.status  = 'Unclassified' then excluded.subtype else irdai_documents.subtype end
"""


def doc_row(slug: str, d: dict) -> tuple:
    src = d.get("_source", {})
    return (
        slug, src.get("liferay_id"), d.get("type"), d.get("sourceCategory"),
        d.get("title"), d.get("refNo"), d.get("dept"), d.get("entity"),
        d.get("subtype"), d.get("status", "Unclassified"), d.get("year"),
        d.get("dateIssued"), bool(d.get("archived")),
        d.get("subjects") or [], d.get("maintenance") or [], d.get("aliases") or [],
        Json(d.get("relations") or {}), src.get("pending_relations") or [],
        d.get("attachments") or 1, src.get("detail_page"),
        src.get("pdf_links") or [], src.get("source_page"),
        src.get("first_seen"), src.get("last_seen"),
    )


def upsert_documents(conn, corpus: dict) -> None:
    rows = [doc_row(slug, d) for slug, d in corpus.items()]
    with conn.cursor() as cur:
        execute_values(cur, DOC_UPSERT, rows, page_size=200)
    conn.commit()
    log.info("metadata upserted for %d documents", len(rows))


def existing_hashes(conn) -> dict:
    with conn.cursor() as cur:
        cur.execute("select id, content_sha1 from irdai_documents")
        return dict(cur.fetchall())


def store_extraction(conn, slug: str, extraction: dict, chunks: list[dict], embeddings) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """update irdai_documents
               set full_text = %s, content_sha1 = %s, extraction_method = %s,
                   page_count = %s, loaded_at = %s
               where id = %s""",
            (extraction["full_text"], extraction["content_sha1"],
             extraction["extraction_method"], extraction["page_count"],
             datetime.now(timezone.utc), slug),
        )
        cur.execute("delete from irdai_chunks where doc_id = %s", (slug,))
        rows = []
        for c, emb in zip(chunks, embeddings):
            cid = hashlib.sha1(f"{slug}|{c['chunk_index']}|{c['content']}".encode()).hexdigest()
            vec = "[" + ",".join(f"{x:.6f}" for x in emb) + "]" if emb is not None else None
            rows.append((cid, slug, c["chunk_index"], c["heading"], c["attachment"],
                         c["content"], vec))
        execute_values(
            cur,
            """insert into irdai_chunks
               (id, doc_id, chunk_index, heading, attachment, content, embedding)
               values %s on conflict (id) do nothing""",
            rows, page_size=100,
        )
    conn.commit()


# ================= MAIN =================


def run(args) -> None:
    if not CORPUS_JSON.exists():
        log.error("no corpus at %s — run irdai_watcher.py first", CORPUS_JSON)
        sys.exit(1)
    with open(CORPUS_JSON, encoding="utf-8") as f:
        corpus = json.load(f).get("docs", {})
    if args.only:
        corpus = {k: v for k, v in corpus.items() if k in args.only}
        if not corpus:
            log.error("--only matched nothing")
            sys.exit(1)
    log.info("corpus: %d documents", len(corpus))

    if args.dry_run:
        todo = [s for s, d in corpus.items() if d.get("_source", {}).get("pdf_links")]
        log.info("dry-run: %d docs have PDFs; would sync metadata for all %d",
                 len(todo), len(corpus))
        return

    conn = connect()
    try:
        upsert_documents(conn, corpus)

        if args.skip_pdf:
            log.info("--skip-pdf: metadata sync only, done")
            return

        hashes = existing_hashes(conn)
        session = requests.Session()
        session.headers.update(HEADERS)

        pending = [
            (slug, d) for slug, d in corpus.items()
            if d.get("_source", {}).get("pdf_links")
            and (args.refresh or not hashes.get(slug))
        ]
        if args.limit:
            pending = pending[: args.limit]
        log.info("extraction queue: %d documents", len(pending))

        done = failed = skipped = 0
        for slug, d in pending:
            extraction = extract_issuance(session, d, args.delay)
            if extraction is None:
                failed += 1
                log.warning("%s: no extractable attachment", slug)
                continue
            if not args.refresh and hashes.get(slug) == extraction["content_sha1"]:
                skipped += 1
                continue
            chunks = chunk_issuance(extraction)
            if not chunks:
                failed += 1
                log.warning("%s: extraction produced no chunks", slug)
                continue
            model = get_model()
            t0 = time.time()
            # Language gate: MiniLM-L6-v2 is English-only. Hindi-dominant
            # chunks (bilingual gazette files) are stored for grep/full-text
            # but kept OUT of the semantic index (embedding = NULL) so they
            # don't inject noise vectors into hybrid search.
            mask = [devanagari_ratio(c["content"]) < 0.3 for c in chunks]
            to_embed = [c["content"] for c, m in zip(chunks, mask) if m]
            vecs = iter(model.encode(
                to_embed,
                batch_size=64, normalize_embeddings=True, show_progress_bar=False,
            )) if to_embed else iter(())
            embeddings = [next(vecs) if m else None for m in mask]
            embed_s = time.time() - t0
            skipped_hi = len(mask) - sum(mask)
            store_extraction(conn, slug, extraction, chunks, embeddings)
            done += 1
            log.info("%s: %d pages -> %d chunks (%s), embedded %d in %.1fs%s",
                     slug, extraction["page_count"], len(chunks),
                     extraction["extraction_method"], sum(mask), embed_s,
                     f", {skipped_hi} Hindi-dominant kept FTS-only" if skipped_hi else "")

        log.info("loader done: embedded=%d unchanged=%d failed=%d", done, skipped, failed)
    finally:
        conn.close()


def main() -> None:
    p = argparse.ArgumentParser(description="Load IRDAI corpus into Supabase")
    p.add_argument("--refresh", action="store_true", help="force re-extract + re-embed")
    p.add_argument("--skip-pdf", action="store_true", help="metadata sync only")
    p.add_argument("--limit", type=int, default=None, help="cap extraction work this run")
    p.add_argument("--only", nargs="*", default=None, help="restrict to given slugs")
    p.add_argument("--delay", type=float, default=1.0, help="seconds between PDF fetches")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("urllib3").setLevel(logging.INFO)
    run(args)


if __name__ == "__main__":
    main()
