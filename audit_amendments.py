#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
audit_amendments.py — grammar audit for provision_engine over the live corpus.

READ-ONLY. Pulls fulltext of amendment-titled instruments from irdai_chunks,
runs parse_amendment, and reports coverage: edges by action/confidence,
principal-resolution outcomes, and — the point of the exercise — every
operative line the grammar could not parse, aggregated for iteration.

Usage:
  SUPABASE_DB_URL=postgres://... python audit_amendments.py
  python audit_amendments.py --slugs reg-irdai-meetings-amendment-regulations-2025-2025
  python audit_amendments.py --out audit_report.jsonl

Skips consolidated texts ("as amended", "incorporating all amendments") —
those are principal texts, not amendment instruments. Strips lines that are
majority-Devanagari (bilingual gazettes) before parsing. Trims the loader's
~200-char chunk overlap during reassembly so operative lines at chunk seams
are not double-counted.
"""

from __future__ import annotations
import argparse, json, os, re, sys, unicodedata
from collections import Counter
from dataclasses import asdict

from provision_engine import parse_amendment, resolve_principal, OPERATIVE

DEVANAGARI = re.compile(r"[\u0900-\u097F]")
CONSOLIDATED = re.compile(r"as amended|incorporating", re.I)
OVERLAP_MAX = 260


def strip_hindi(text: str) -> str:
    out = []
    for line in text.splitlines():
        letters = [c for c in line if unicodedata.category(c).startswith("L")]
        if letters and sum(1 for c in letters if DEVANAGARI.match(c)) / len(letters) > 0.5:
            continue
        out.append(line)
    return "\n".join(out)


def join_trim_overlap(pieces: list[str]) -> str:
    """Concatenate chunks, trimming the loader's suffix/prefix overlap."""
    text = ""
    for p in pieces:
        if not text:
            text = p
            continue
        k = min(OVERLAP_MAX, len(text), len(p))
        cut = 0
        for n in range(k, 40, -1):
            if text[-n:] == p[:n]:
                cut = n
                break
        text += p[cut:]
    return text


def fetch_docs(conn, slugs):
    q = """select id, title from irdai_documents
           where title ~* 'amendment' order by year desc, id"""
    with conn.cursor() as cur:
        cur.execute(q)
        rows = cur.fetchall()
    docs = [(s, t) for s, t in rows if not CONSOLIDATED.search(t or "")]
    if slugs:
        docs = [(s, t) for s, t in docs if s in set(slugs)]
    return docs


def fetch_titles(conn) -> dict:
    with conn.cursor() as cur:
        cur.execute("select id, title, type from irdai_documents")
        return {r[0]: {"title": r[1], "type": r[2]} for r in cur.fetchall()}


def fetch_fulltext(conn, slug) -> str:
    with conn.cursor() as cur:
        cur.execute("""select attachment, chunk_index, content from irdai_chunks
                       where doc_id = %s order by attachment, chunk_index""", (slug,))
        rows = cur.fetchall()
    parts, cur_att, buf = [], None, []
    for att, _, content in rows:
        if att != cur_att and buf:
            parts.append(join_trim_overlap(buf)); buf = []
        cur_att = att
        buf.append(content or "")
    if buf:
        parts.append(join_trim_overlap(buf))
    return "\n\n".join(parts)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--slugs", nargs="*", default=None)
    ap.add_argument("--out", default="audit_report.jsonl")
    args = ap.parse_args()

    dsn = os.environ.get("SUPABASE_DB_URL")
    if not dsn:
        sys.exit("SUPABASE_DB_URL not set")
    import psycopg2
    conn = psycopg2.connect(dsn)

    corpus_titles = fetch_titles(conn)
    docs = fetch_docs(conn, args.slugs)
    print(f"auditing {len(docs)} amendment instruments\n")

    agg_conf, agg_action, unparsed_all = Counter(), Counter(), []
    resolved = pending = empty = 0

    with open(args.out, "w", encoding="utf-8") as out:
        for slug, title in docs:
            text = strip_hindi(fetch_fulltext(conn, slug))
            if len(text) < 200:
                empty += 1
                print(f"  {slug[:58]:58} NO-FULLTEXT")
                continue
            res = parse_amendment(text)
            ops = sum(1 for l in text.splitlines()
                      if OPERATIVE.search(l) and "come into force" not in l)
            edges = res["edges"]
            agg_conf.update(e.confidence for e in edges)
            agg_action.update(e.action for e in edges)
            unparsed_all += [(slug, u) for u in res["unparsed"]]

            pslug, score = (None, 0.0)
            if res["principal"]:
                pslug, score = resolve_principal(res["principal"], corpus_titles)
                # an amendment must not resolve to itself
                if pslug == slug:
                    pslug = None
            resolved += bool(pslug); pending += bool(res["principal"] and not pslug)

            rec = {"slug": slug, "title": title, "operative_lines": ops,
                   "edges": len(edges), "unparsed": res["unparsed"],
                   "confidence": dict(Counter(e.confidence for e in edges)),
                   "principal": res["principal"], "principal_slug": pslug,
                   "principal_score": round(score, 2),
                   "edge_list": [asdict(e) for e in edges]}
            out.write(json.dumps(rec, ensure_ascii=False) + "\n")
            print(f"  {slug[:58]:58} ops={ops:3} edges={len(edges):3} "
                  f"unparsed={len(res['unparsed']):2} principal={'->'+pslug if pslug else ('PENDING' if res['principal'] else '—')}")

    print(f"\n=== SUMMARY ===")
    print(f"docs audited: {len(docs)} (no fulltext: {empty})")
    print(f"edges: {sum(agg_conf.values())}  by confidence: {dict(agg_conf)}")
    print(f"       by action: {dict(agg_action)}")
    print(f"principal resolution: {resolved} resolved, {pending} pending")
    print(f"unparsed operative lines: {len(unparsed_all)}")
    for slug, u in unparsed_all[:40]:
        print(f"  [{slug[:36]}] {u[:110]}")
    print(f"\nreport: {args.out}")


if __name__ == "__main__":
    main()
