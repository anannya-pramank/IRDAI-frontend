"""Confirm whether the loader's 'produced no chunks' docs are scanned
(no text layer -> need OCR) or a genuine extraction bug. Reads the committed
corpus for fresh PDF URLs, fetches each attachment in memory, and reports
per-page extracted-text length. Zero chars across all pages == scanned."""
import json
import requests
import fitz  # pymupdf

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

SLUGS = [
    "not-reconstitution-of-reinsurance-advisory-commi-2026",
    "cir-circular-2025",
    "gl-guidelines-on-remuneration-of-directors-and-2023",
    "gl-guidelines-on-information-and-cyber-security-2022",
    "gl-guidelines-on-settlement-of-life-insurance-c-2021",
    "act-the-insurance-act-1938-2007",  # the .doc case, for contrast
]


def sniff(data: bytes) -> str:
    head = data[:8]
    if data[:1024].lstrip().startswith(b"%PDF"):
        return "PDF"
    if head[:4] == b"\xd0\xcf\x11\xe0":
        return "legacy MS Office .doc/.xls (OLE2)"
    if head[:2] == b"PK":
        return "zip-based (docx/xlsx)"
    if data[:300].lstrip().lower().startswith((b"<!doctype", b"<html")):
        return "html error page"
    return f"unknown (starts {head!r})"


def probe_pdf(data: bytes) -> None:
    doc = fitz.open(stream=data, filetype="pdf")
    total = 0
    per_page = []
    for pg in doc:
        n = len(pg.get_text("text").strip())
        per_page.append(n)
        total += n
    pages = doc.page_count
    doc.close()
    verdict = "SCANNED / no text layer -> OCR needed" if total < 16 * max(pages, 1) \
        else "has text layer (extraction bug?)"
    print(f"    pages={pages} per_page_chars={per_page} total={total}  => {verdict}")


def main() -> None:
    docs = json.load(open("data/corpus.json", encoding="utf-8"))["docs"]
    sess = requests.Session()
    sess.headers.update({"User-Agent": UA})
    for slug in SLUGS:
        d = docs.get(slug)
        print(f"\n### {slug}")
        if not d:
            print("    MISSING from corpus"); continue
        print(f"    title: {d.get('title', '')[:80]}")
        links = (d.get("_source") or {}).get("pdf_links") or []
        if not links:
            print("    no pdf_links"); continue
        for i, url in enumerate(links):
            if not url:
                continue
            try:
                r = sess.get(url, timeout=40, allow_redirects=True)
            except Exception as e:
                print(f"    attachment {i+1}: fetch error {e}"); continue
            data = r.content
            kind = sniff(data)
            print(f"    attachment {i+1}: {len(data)} bytes, {kind}")
            if kind == "PDF":
                try:
                    probe_pdf(data)
                except Exception as e:
                    print(f"      pdf open failed: {e}")


if __name__ == "__main__":
    main()
