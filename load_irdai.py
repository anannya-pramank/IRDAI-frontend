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
TEXT_DIR = BASE_DIR / "data" / "text"   # per-doc extracted text for the wiki's Text tab

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


STORAGE_BUCKET = os.environ.get("SUPABASE_STORAGE_BUCKET", "irdai-pdfs")


def storage_enabled() -> bool:
    return bool(os.environ.get("SUPABASE_URL") and os.environ.get("SUPABASE_SERVICE_KEY"))


def upload_pdf(slug: str, idx: int, filename: str, data: bytes) -> str | None:
    """Mirror one attachment to Supabase Storage; returns the public URL.
    Source URLs on irdai.gov.in force download (Content-Disposition) and may
    block framing, so the wiki's inline viewer needs a mirrored copy."""
    base = os.environ["SUPABASE_URL"].rstrip("/")
    key = os.environ["SUPABASE_SERVICE_KEY"]
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", filename or "").strip("-")[:80] or f"attachment-{idx}"
    if not safe.lower().endswith(".pdf"):
        safe += ".pdf"
    path = f"{slug}/{idx:02d}-{safe}"
    try:
        r = requests.post(
            f"{base}/storage/v1/object/{STORAGE_BUCKET}/{path}",
            data=data,
            headers={"Authorization": f"Bearer {key}",
                     "Content-Type": "application/pdf",
                     "x-upsert": "true"},
            timeout=120,
        )
        if r.status_code in (200, 201):
            return f"{base}/storage/v1/object/public/{STORAGE_BUCKET}/{path}"
        log.warning("  storage upload failed for %s: HTTP %s %s", path, r.status_code, r.text[:200])
    except requests.RequestException as e:
        log.warning("  storage upload failed for %s: %s", path, e)
    return None


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


def _ocr_text(doc, pages: int) -> str:
    """OCR every page via pymupdf's Tesseract bridge, for scanned/image-only
    PDFs that carry no text layer. Needs the tesseract binary + language data
    (eng + hin) reachable via TESSDATA_PREFIX. Returns '' on any failure, so a
    missing or broken OCR install degrades cleanly to the pre-OCR behaviour
    (the doc is simply left untextized, exactly as before)."""
    lang = os.environ.get("OCR_LANG", "eng+hin")
    t0 = time.time()
    out = []
    for page in doc:
        try:
            tp = page.get_textpage_ocr(flags=0, language=lang, dpi=200, full=True)
            out.append(page.get_text("text", textpage=tp))
        except Exception as e:
            log.warning("  OCR unavailable/failed (%s) — need tesseract + "
                        "TESSDATA_PREFIX with eng+hin; leaving doc untextized", e)
            return ""
    text = "\n\n".join(out)
    log.info("  OCR: %d pages in %.1fs (%d chars)",
             pages, time.time() - t0, len(text.strip()))
    return text


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
        # Scanned / image-only PDFs have no text layer, so both pymupdf4llm
        # and get_text come back (near-)empty. Fall back to OCR before giving
        # up — many older IRDAI notices are bilingual scans. Threshold ~16
        # chars/page distinguishes "no text layer" from a real text PDF.
        if len(text.strip()) < 16 * max(pages, 1):
            ocr = _ocr_text(doc, pages)
            if len(ocr.strip()) > len(text.strip()):
                return ocr, "tesseract-ocr", pages
        return text, "pymupdf", pages
    finally:
        doc.close()


def looks_like_pdf(data: bytes) -> bool:
    return data[:1024].lstrip().startswith(b"%PDF")


def sniff_kind(data: bytes) -> str:
    head = data[:300].lstrip().lower()
    if head.startswith((b"<!doctype", b"<html", b"<?xml")):
        return "html/xml page (likely a stale download token or error page)"
    if data[:2] == b"PK":
        return "zip-based file (docx/xlsx/zip)"
    return f"unknown ({len(data)} bytes, starts {data[:8]!r})"


def extract_issuance(session: requests.Session, rec: dict, delay: float,
                     slug: str | None = None) -> dict | None:
    """Fetch + extract ALL attachments of one issuance, in memory.
    If Supabase Storage env is configured, each valid PDF is also mirrored
    (bytes are still never written to local disk). A bad attachment is
    logged and skipped — never fatal to the document, let alone the run."""
    src = rec.get("_source", {})
    links = src.get("pdf_links") or []
    names = src.get("pdf_filenames") or []
    if not links:
        return None

    parts, methods, pdfs, total_pages = [], set(), [], 0
    for i, url in enumerate(links):
        label = names[i] if i < len(names) and names[i] else f"attachment-{i + 1}"
        t0 = time.time()
        data = fetch_pdf_bytes(session, url)
        time.sleep(delay)
        if not data:
            continue
        if not looks_like_pdf(data):
            log.warning("  %s: response is not a PDF — %s — skipped", label, sniff_kind(data))
            continue
        fetch_s = time.time() - t0
        size_mb = len(data) / 1e6
        storage_url = None
        if slug and storage_enabled():
            storage_url = upload_pdf(slug, i + 1, label, data)
        pdfs.append({"name": label, "url": storage_url, "source": url})
        t0 = time.time()
        try:
            text, method, pages = extract_text(data)
        except Exception as e:
            log.warning("  %s: extraction failed — %s — skipped", label, e)
            continue
        finally:
            del data  # explicit: bytes discarded here
        log.info("  %s: %.1f MB fetched in %.1fs; %d pages extracted in %.1fs (%s)%s",
                 label, size_mb, fetch_s, pages, time.time() - t0, method,
                 " — mirrored" if storage_url else "")
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
        "pdfs": pdfs,
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
  {", ".join(f"{c} = excluded.{c}" for c in DOC_COLS if c != "id")}
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
                   page_count = %s, pdf_storage = %s, loaded_at = %s
               where id = %s""",
            (extraction["full_text"], extraction["content_sha1"],
             extraction["extraction_method"], extraction["page_count"],
             Json(extraction.get("pdfs") or []),
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


def write_text_json(slug: str, title: str, method: str, pages: int,
                    full_text: str, chunk_rows: list[dict],
                    pdfs: list[dict] | None = None) -> None:
    """Per-doc payload consumed by the wiki's Document tab (lazy fetch)."""
    TEXT_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "slug": slug, "title": title, "method": method, "pages": pages,
        "full_text": full_text,
        "chunks": chunk_rows,
        "pdfs": pdfs or [],
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    with open(TEXT_DIR / f"{slug}.json", "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)


def export_text_from_db(conn) -> int:
    """Backfill data/text/*.json for everything already loaded in Supabase
    (the wiki reads static files, not the DB — this bridges existing rows)."""
    with conn.cursor() as cur:
        cur.execute("""select id, title, extraction_method, page_count, full_text,
                              coalesce(pdf_storage, '[]'::jsonb)
                       from irdai_documents where full_text is not null""")
        docs = cur.fetchall()
        n = 0
        for slug, title, method, pages, full_text, pdfs in docs:
            cur.execute("""select chunk_index, heading, attachment, content,
                                  (embedding is not null) as embedded
                           from irdai_chunks where doc_id = %s
                           order by chunk_index""", (slug,))
            chunk_rows = [
                {"i": ci, "heading": h, "attachment": a, "content": c, "embedded": e}
                for ci, h, a, c, e in cur.fetchall()
            ]
            write_text_json(slug, title, method, pages, full_text, chunk_rows, pdfs)
            n += 1
    log.info("exported text payloads for %d documents -> %s", n, TEXT_DIR)
    return n


# ================= MAIN =================


def run(args) -> None:
    if args.export_text:
        conn = connect()
        try:
            export_text_from_db(conn)
        finally:
            conn.close()
        return

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
            try:
                extraction = extract_issuance(session, d, args.delay, slug=slug)
                if extraction is None:
                    failed += 1
                    log.warning("%s: no extractable attachment", slug)
                    continue
                if not args.refresh and hashes.get(slug) == extraction["content_sha1"]:
                    skipped += 1
                    continue
                if args.refresh and hashes.get(slug) == extraction["content_sha1"] \
                        and any(p.get("url") for p in extraction.get("pdfs", [])):
                    # text unchanged, but we (re)mirrored the PDFs — record them
                    with conn.cursor() as cur:
                        cur.execute("update irdai_documents set pdf_storage = %s where id = %s",
                                    (Json(extraction["pdfs"]), slug))
                    conn.commit()
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
                write_text_json(
                    slug, d.get("title", slug), extraction["extraction_method"],
                    extraction["page_count"], extraction["full_text"],
                    [{"i": c["chunk_index"], "heading": c["heading"],
                      "attachment": c["attachment"], "content": c["content"],
                      "embedded": bool(m)} for c, m in zip(chunks, mask)],
                    extraction.get("pdfs"))
                done += 1
                log.info("%s: %d pages -> %d chunks (%s), embedded %d in %.1fs%s",
                         slug, extraction["page_count"], len(chunks),
                         extraction["extraction_method"], sum(mask), embed_s,
                         f", {skipped_hi} Hindi-dominant kept FTS-only" if skipped_hi else "")
            except Exception as e:
                conn.rollback()
                failed += 1
                log.warning("%s: failed — %s (run continues)", slug, e)

        log.info("loader done: embedded=%d unchanged=%d failed=%d", done, skipped, failed)
    finally:
        conn.close()


def main() -> None:
    p = argparse.ArgumentParser(description="Load IRDAI corpus into Supabase")
    p.add_argument("--refresh", action="store_true", help="force re-extract + re-embed")
    p.add_argument("--skip-pdf", action="store_true", help="metadata sync only")
    p.add_argument("--export-text", action="store_true",
                   help="regenerate data/text/*.json from Supabase for all loaded docs")
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
