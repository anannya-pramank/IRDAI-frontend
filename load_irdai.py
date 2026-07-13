#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
load_irdai.py
--------------
Loader half of the IRDAI pipeline (irdai_watcher.py stays network-light and
only writes data/corpus.json). This script does the heavy lifting:

  1. Read data/corpus.json (keyed by slug, same shape irdai_watcher.py emits).
  2. For each doc, download its attachment PDFs (all attachments concatenated,
     each one header-tagged so chunks can carry an `attachment` filename).
  3. Extract text with pymupdf4llm (markdown-aware), falling back to plain
     PyMuPDF (fitz) text extraction if pymupdf4llm errors or is unavailable.
  4. Hash the extracted text (sha1). Skip re-chunk/re-embed if unchanged from
     what's already in Supabase, unless --refresh is passed.
  5. Chunk (~1800 chars, 200 overlap), embed with all-MiniLM-L6-v2 (384-dim,
     cosine-normalized), and upsert into irdai_documents / irdai_chunks via
     the Supabase transaction pooler (port 6543).

Mirrors the cerc_scraper.py / load_cerc.py split: watcher = discovery +
metadata, loader = extraction + embeddings + DB. Point a copy of the
cerc_mcp.py-style MCP server at irdai_hybrid_search / irdai_grep once loaded.

Usage:
  python load_irdai.py                        # load everything new/changed
  python load_irdai.py --limit 25              # cap this run to 25 docs
  python load_irdai.py --slugs cir-foo-2024    # only named slugs
  python load_irdai.py --refresh                # force re-extract + re-embed
  python load_irdai.py --skip-extract           # metadata-only upsert, no PDFs
  python load_irdai.py --dry-run                # log actions, write nothing

Env vars (same names as the CERC/APTEL loaders):
  SUPABASE_DB_URL   postgres connection string, pooler on :6543
                     e.g. postgresql://postgres.xxxx:PASSWORD@aws-0-ap-south-1
                          .pooler.supabase.com:6543/postgres
  HF_HUB_OFFLINE=1  set automatically once the MiniLM model is cached locally,
                     to avoid a network check on every run.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

# ================= CONFIG =================

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
CORPUS_JSON = DATA_DIR / "corpus.json"
PDF_CACHE_DIR = DATA_DIR / "pdf_cache"
PDF_CACHE_DIR.mkdir(parents=True, exist_ok=True)

EMBED_MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"
EMBED_DIM = 384

CHUNK_SIZE = 1800
CHUNK_OVERLAP = 200

MAX_PDF_MB = 40          # skip (with a warning) any single attachment over this
DOWNLOAD_TIMEOUT = 60
STATEMENT_TIMEOUT_MS = 120_000  # 2 min safety cap per DB statement

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
}

log = logging.getLogger("load_irdai")

# ================= LAZY MODEL / HEAVY IMPORTS =================
# Keep top-level imports light (psycopg2 + requests only) so --skip-extract
# and --dry-run runs, plus syntax checks, don't need torch/sentence-transformers
# or pymupdf4llm installed.

_embed_model = None


def get_embed_model():
    global _embed_model
    if _embed_model is None:
        os.environ.setdefault("HF_HUB_OFFLINE", "1")
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError:
            os.environ.pop("HF_HUB_OFFLINE", None)
            from sentence_transformers import SentenceTransformer
        log.info("Loading embedding model %s ...", EMBED_MODEL_NAME)
        _embed_model = SentenceTransformer(EMBED_MODEL_NAME)
    return _embed_model


def embed_texts(texts: list[str]) -> list[list[float]]:
    if not texts:
        return []
    model = get_embed_model()
    vecs = model.encode(
        texts,
        batch_size=32,
        show_progress_bar=False,
        normalize_embeddings=True,  # unit vectors -> cosine == dot product
    )
    return [v.tolist() for v in vecs]


def extract_pdf_text(pdf_path: Path) -> tuple[str, str, int]:
    """Return (markdown_text, method, page_count). Tries pymupdf4llm first."""
    try:
        import pymupdf4llm

        md = pymupdf4llm.to_markdown(str(pdf_path))
        import fitz  # PyMuPDF, for page count

        with fitz.open(str(pdf_path)) as doc:
            pages = doc.page_count
        if md and md.strip():
            return md, "pymupdf4llm", pages
    except Exception as e:  # noqa: BLE001 - fall back below regardless of cause
        log.warning("pymupdf4llm failed on %s (%s) — falling back to fitz", pdf_path.name, e)

    try:
        import fitz

        with fitz.open(str(pdf_path)) as doc:
            text = "\n\n".join(page.get_text() for page in doc)
            pages = doc.page_count
        return text, "pymupdf", pages
    except Exception as e:  # noqa: BLE001
        log.error("fitz extraction also failed on %s (%s)", pdf_path.name, e)
        return "", "none", 0


# ================= HTTP =================


def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(HEADERS)
    return s


def download_pdf(session: requests.Session, url: str, dest: Path) -> bool:
    if dest.exists() and dest.stat().st_size > 0:
        return True
    try:
        with session.get(url, stream=True, timeout=DOWNLOAD_TIMEOUT) as r:
            r.raise_for_status()
            size = int(r.headers.get("content-length", 0) or 0)
            if size and size > MAX_PDF_MB * 1024 * 1024:
                log.warning("Skipping %s — %.1f MB exceeds cap (%d MB)",
                            url, size / 1024 / 1024, MAX_PDF_MB)
                return False
            tmp = dest.with_suffix(".part")
            with open(tmp, "wb") as f:
                for chunk in r.iter_content(chunk_size=1 << 16):
                    f.write(chunk)
            tmp.rename(dest)
        return True
    except requests.RequestException as e:
        log.warning("Download failed for %s: %s", url, e)
        return False


# ================= CHUNKING =================


def chunk_text(text: str, size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list[str]:
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= size:
        return [text]

    chunks = []
    start = 0
    n = len(text)
    while start < n:
        end = min(start + size, n)
        # try to break on a paragraph/sentence boundary near the end
        if end < n:
            window = text[start:end]
            for sep in ("\n\n", ". ", "\n"):
                idx = window.rfind(sep)
                if idx > size * 0.5:
                    end = start + idx + len(sep)
                    break
        piece = text[start:end].strip()
        if piece:
            chunks.append(piece)
        if end >= n:
            break
        start = max(end - overlap, start + 1)
    return chunks


def sha1(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8", errors="ignore")).hexdigest()


# ================= DB =================


def get_conn():
    import psycopg2

    dsn = os.environ.get("SUPABASE_DB_URL")
    if not dsn:
        raise SystemExit("SUPABASE_DB_URL is not set")
    conn = psycopg2.connect(dsn)
    conn.autocommit = False
    with conn.cursor() as cur:
        cur.execute(f"set statement_timeout = {STATEMENT_TIMEOUT_MS}")
    return conn


def fetch_existing_hashes(conn, doc_ids: list[str]) -> dict[str, str]:
    """slug -> content_sha1 already stored, for change detection."""
    if not doc_ids:
        return {}
    with conn.cursor() as cur:
        cur.execute(
            "select id, content_sha1 from irdai_documents where id = any(%s)",
            (doc_ids,),
        )
        return {r[0]: r[1] for r in cur.fetchall()}


def upsert_document(conn, slug: str, rec: dict, full_text: str | None,
                     content_hash: str | None, method: str | None,
                     pages: int | None) -> None:
    src = rec.get("_source", {})
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into irdai_documents (
                id, liferay_id, type, source_category, title, ref_no, dept,
                entity, subtype, status, year, date_issued, archived,
                subjects, maintenance, aliases, relations, pending_relations,
                attachments, detail_page, pdf_links, source_page,
                full_text, content_sha1, extraction_method, page_count,
                first_seen, last_seen
            ) values (
                %(id)s, %(liferay_id)s, %(type)s, %(source_category)s, %(title)s,
                %(ref_no)s, %(dept)s, %(entity)s, %(subtype)s, %(status)s,
                %(year)s, %(date_issued)s, %(archived)s, %(subjects)s,
                %(maintenance)s, %(aliases)s, %(relations)s, %(pending_relations)s,
                %(attachments)s, %(detail_page)s, %(pdf_links)s, %(source_page)s,
                %(full_text)s, %(content_sha1)s, %(extraction_method)s, %(page_count)s,
                %(first_seen)s, %(last_seen)s
            )
            on conflict (id) do update set
                liferay_id        = excluded.liferay_id,
                type               = excluded.type,
                source_category    = excluded.source_category,
                title              = excluded.title,
                ref_no             = excluded.ref_no,
                dept               = excluded.dept,
                entity             = excluded.entity,
                subtype            = excluded.subtype,
                status             = excluded.status,
                year               = excluded.year,
                date_issued        = excluded.date_issued,
                archived           = excluded.archived,
                subjects           = excluded.subjects,
                maintenance        = excluded.maintenance,
                aliases            = excluded.aliases,
                relations          = excluded.relations,
                pending_relations  = excluded.pending_relations,
                attachments        = excluded.attachments,
                detail_page        = excluded.detail_page,
                pdf_links          = excluded.pdf_links,
                source_page        = excluded.source_page,
                full_text          = coalesce(excluded.full_text, irdai_documents.full_text),
                content_sha1       = coalesce(excluded.content_sha1, irdai_documents.content_sha1),
                extraction_method  = coalesce(excluded.extraction_method, irdai_documents.extraction_method),
                page_count         = coalesce(excluded.page_count, irdai_documents.page_count),
                last_seen          = excluded.last_seen,
                loaded_at          = now()
            """,
            {
                "id": slug,
                "liferay_id": src.get("liferay_id"),
                "type": rec.get("type"),
                "source_category": rec.get("sourceCategory"),
                "title": rec.get("title"),
                "ref_no": rec.get("refNo"),
                "dept": rec.get("dept"),
                "entity": rec.get("entity"),
                "subtype": rec.get("subtype"),
                "status": rec.get("status", "Unclassified"),
                "year": rec.get("year"),
                "date_issued": rec.get("dateIssued"),
                "archived": bool(rec.get("archived", False)),
                "subjects": rec.get("subjects", []),
                "maintenance": rec.get("maintenance", []),
                "aliases": rec.get("aliases", []),
                "relations": json.dumps(rec.get("relations", {})),
                "pending_relations": src.get("pending_relations", []),
                "attachments": rec.get("attachments", 1),
                "detail_page": src.get("detail_page"),
                "pdf_links": src.get("pdf_links", []),
                "source_page": src.get("source_page"),
                "full_text": full_text,
                "content_sha1": content_hash,
                "extraction_method": method,
                "page_count": pages,
                "first_seen": src.get("first_seen"),
                "last_seen": src.get("last_seen"),
            },
        )


def replace_chunks(conn, doc_id: str, chunks: list[dict]) -> None:
    """chunks: [{index, heading, attachment, content, embedding}]"""
    with conn.cursor() as cur:
        cur.execute("delete from irdai_chunks where doc_id = %s", (doc_id,))
        for c in chunks:
            cid = sha1(f"{doc_id}|{c['index']}|{c['content'][:200]}")
            cur.execute(
                """
                insert into irdai_chunks (id, doc_id, chunk_index, heading, attachment, content, embedding)
                values (%s, %s, %s, %s, %s, %s, %s)
                on conflict (id) do update set
                    heading = excluded.heading, content = excluded.content,
                    embedding = excluded.embedding
                """,
                (cid, doc_id, c["index"], c.get("heading"), c.get("attachment"),
                 c["content"], c["embedding"]),
            )


# ================= CORE PIPELINE =================


def load_corpus() -> dict:
    if not CORPUS_JSON.exists():
        raise SystemExit(f"{CORPUS_JSON} not found — run irdai_watcher.py first")
    with open(CORPUS_JSON, encoding="utf-8") as f:
        return json.load(f).get("docs", {})


def process_doc(session: requests.Session, slug: str, rec: dict, args) -> dict | None:
    """Download attachments, extract, return {full_text, hash, method, pages, chunks_raw}."""
    src = rec.get("_source", {})
    pdf_links = src.get("pdf_links") or []
    filenames = src.get("pdf_filenames") or []

    if not pdf_links:
        return {"full_text": None, "hash": None, "method": "none", "pages": 0, "sections": []}

    sections = []  # (attachment_label, text)
    total_pages = 0
    method_used = None

    for i, url in enumerate(pdf_links):
        fname = filenames[i] if i < len(filenames) else f"attachment-{i+1}.pdf"
        cache_path = PDF_CACHE_DIR / f"{slug}__{i}.pdf"

        if not download_pdf(session, url, cache_path):
            continue
        time.sleep(args.delay)

        text, method, pages = extract_pdf_text(cache_path)
        method_used = method_used or method
        total_pages += pages
        if text.strip():
            sections.append((fname, text.strip()))

    if not sections:
        return {"full_text": None, "hash": None, "method": method_used or "none",
                 "pages": total_pages, "sections": []}

    full_text = "\n\n".join(f"## {name}\n\n{text}" for name, text in sections)
    return {
        "full_text": full_text,
        "hash": sha1(full_text),
        "method": method_used,
        "pages": total_pages,
        "sections": sections,
    }


def build_chunks(doc_id: str, sections: list[tuple[str, str]]) -> list[dict]:
    """Chunk each attachment's text separately so `attachment` stays accurate,
    but chunk_index is continuous across the whole document."""
    raw = []
    idx = 0
    for fname, text in sections:
        for piece in chunk_text(text):
            heading = None
            first_line = piece.strip().splitlines()[0] if piece.strip() else ""
            if first_line.startswith("#"):
                heading = first_line.lstrip("#").strip()
            raw.append({"index": idx, "heading": heading, "attachment": fname, "content": piece})
            idx += 1
    return raw


def run(args) -> None:
    corpus = load_corpus()
    slugs = list(corpus.keys())

    if args.slugs:
        wanted = set(args.slugs)
        slugs = [s for s in slugs if s in wanted]
        missing = wanted - set(slugs)
        if missing:
            log.warning("Requested slugs not found in corpus: %s", ", ".join(sorted(missing)))

    if args.limit:
        slugs = slugs[: args.limit]

    log.info("Processing %d document(s) from %s", len(slugs), CORPUS_JSON)

    conn = None
    existing_hashes = {}
    if not args.dry_run:
        conn = get_conn()
        existing_hashes = fetch_existing_hashes(conn, slugs)

    session = make_session()
    n_loaded = n_skipped_unchanged = n_metadata_only = n_failed = 0

    for slug in slugs:
        rec = corpus[slug]
        try:
            if args.skip_extract:
                extraction = {"full_text": None, "hash": None, "method": None, "pages": None, "sections": []}
                n_metadata_only += 1
            else:
                extraction = process_doc(session, slug, rec, args)

            unchanged = (
                not args.refresh
                and extraction["hash"] is not None
                and existing_hashes.get(slug) == extraction["hash"]
            )

            if args.dry_run:
                log.info("[dry-run] %s: text=%s hash=%s unchanged=%s sections=%d",
                          slug, bool(extraction["full_text"]), extraction["hash"],
                          unchanged, len(extraction["sections"]))
                continue

            upsert_document(
                conn, slug, rec,
                full_text=extraction["full_text"],
                content_hash=extraction["hash"],
                method=extraction["method"],
                pages=extraction["pages"],
            )

            if extraction["sections"] and not unchanged:
                raw_chunks = build_chunks(slug, extraction["sections"])
                if raw_chunks:
                    embeddings = embed_texts([c["content"] for c in raw_chunks])
                    for c, emb in zip(raw_chunks, embeddings):
                        c["embedding"] = emb
                    replace_chunks(conn, slug, raw_chunks)
                    log.info("%s: embedded %d chunk(s)", slug, len(raw_chunks))
                n_loaded += 1
            elif unchanged:
                n_skipped_unchanged += 1
                log.info("%s: content unchanged, chunks left as-is", slug)

            conn.commit()

        except Exception as e:  # noqa: BLE001 - one bad doc shouldn't kill the run
            n_failed += 1
            log.error("%s: failed — %s", slug, e)
            if conn:
                conn.rollback()

    if conn:
        conn.close()

    log.info(
        "Done: loaded=%d unchanged=%d metadata_only=%d failed=%d total=%d",
        n_loaded, n_skipped_unchanged, n_metadata_only, n_failed, len(slugs),
    )


def main() -> None:
    p = argparse.ArgumentParser(description="IRDAI corpus -> Supabase loader (PDF extraction + MiniLM embeddings)")
    p.add_argument("--limit", type=int, default=None, help="cap number of docs processed this run")
    p.add_argument("--slugs", nargs="*", default=None, help="only process these corpus slugs")
    p.add_argument("--refresh", action="store_true", help="force re-extract + re-embed even if unchanged")
    p.add_argument("--skip-extract", action="store_true", help="upsert metadata only, no PDF download/extraction")
    p.add_argument("--dry-run", action="store_true", help="log actions, write nothing to Supabase")
    p.add_argument("--delay", type=float, default=1.0, help="seconds between PDF downloads (default 1.0)")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    run(args)


if __name__ == "__main__":
    main()
