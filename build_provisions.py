#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
build_provisions.py — clause-level traceability builder (IRDAI wiki).

Pipeline position: irdai_watcher.py -> load_irdai.py (--export-text) -> THIS
-> data/provisions.generated.js consumed by index.html.

What it does:
  1. Classifies corpus docs into amendment instruments vs principals.
  2. parse_amendment() on each amendment's extracted text; resolves the
     principal via the parsed title (fallback: the amendment's own title
     with amendment words stripped).
  3. segment_provisions() on each amended principal's text.
  4. Anchors each typed edge to a segmented provision (unit+ref match,
     scope-path disambiguation). Anchored high/medium edges become
     provision-level EVENTS: substitute / omit / insert / renumber / amend
     (word-level). Insertions create NEW provision records whose
     introducedBy is the amendment instrument — the "which notification
     brought this clause in" link.
  5. Emits window.IRDAI_PROVISIONS (+ per-doc maintenance queue of
     unresolved / low-confidence edges) for the frontend merge.

Confidence policy: high -> applied & verified; medium -> applied, flagged
unverified; low -> maintenance queue only, never rendered as an event.

Usage:
  python build_provisions.py                       # full corpus
  python build_provisions.py --slugs <principal>   # rebuild one lineage
  python build_provisions.py --report              # print anchoring stats
Reads  : data/corpus.json, data/text/<slug>.json
Writes : data/provisions.generated.js
"""

from __future__ import annotations
import argparse, json, re, sys, unicodedata
from collections import Counter, defaultdict
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from provision_engine import (parse_amendment, segment_provisions,
                              resolve_principal, normalize_extracted_text)

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
TEXT_DIR = DATA / "text"
OUT_JS = DATA / "provisions.generated.js"

DEVANAGARI = re.compile(r"[\u0900-\u097F]")
CONSOLIDATED = re.compile(r"as amended|incorporating|updated with", re.I)
AMENDMENT_TITLE = re.compile(r"\bamendment\b", re.I)

# engine unit -> segmenter level. Units with no structural level (proviso,
# explanation, note, form, row, ...) anchor to their scoped parent instead.
LEVEL_MAP = {
    "regulation": "regulation", "rule": "regulation", "section": "regulation",
    "clause": "clause", "sub-regulation": "clause", "sub-rule": "clause",
    "sub-section": "clause",
    "sub-clause": "subclause",
    "schedule": "schedule", "part": "part", "chapter": "chapter",
}

WORD_LEVEL_ACTION = "amend"   # word-level edits render as "Amended" on the provision


# ================= text plumbing =================

def strip_hindi(text: str) -> str:
    out = []
    for line in text.splitlines():
        letters = [c for c in line if unicodedata.category(c).startswith("L")]
        if letters and sum(1 for c in letters if DEVANAGARI.match(c)) / len(letters) > 0.5:
            continue
        out.append(line)
    return "\n".join(out)


def load_corpus() -> dict:
    p = DATA / "corpus.json"
    if not p.exists():
        sys.exit(f"no corpus at {p} — run irdai_watcher.py first")
    c = json.loads(p.read_text(encoding="utf-8"))
    return c.get("docs", c)   # watcher store wraps docs; accept either shape


def full_text(slug: str) -> str | None:
    p = TEXT_DIR / f"{slug}.json"
    if not p.exists():
        return None
    t = json.loads(p.read_text(encoding="utf-8")).get("full_text")
    return normalize_extracted_text(strip_hindi(t)) if t else None


DRAFT_STAGE = re.compile(r"exposure\s+draft|consultation\s+paper|draft\b", re.I)


def is_amendment(slug: str, d: dict) -> bool:
    t = d.get("title", "")
    if d.get("type") == "Exposure Drafts" or DRAFT_STAGE.search(t):
        return False   # proposed amendments are not in force — no events
    return bool(AMENDMENT_TITLE.search(t)) and not CONSOLIDATED.search(t)


# ================= ref / anchor matching =================

from provision_engine import UNIT, REF, _norm_unit

LOCATOR = re.compile(rf"(?P<unit>{UNIT})\s*[-\s]?\(?(?P<ref>{REF})\)?", re.I)
PATH_LEVEL = {"sch": "schedule", "par": "part", "cha": "chapter",
              "reg": "regulation", "cla": "clause", "sub": "subclause"}
DESCEND = ["regulation", "clause", "subclause"]


def norm_ref(r: str | None) -> str:
    if not r:
        return ""
    return re.sub(r"\s+", "", r).strip("().").upper()


def path_locators(path: str) -> list[tuple[str, str]]:
    out = []
    for comp in path.split("/"):
        if "-" in comp:
            pre, ref = comp.split("-", 1)
            lvl = PATH_LEVEL.get(pre)
            if lvl:
                out.append((lvl, norm_ref(ref)))
    return out


def decompose_ref(unit: str | None, tref: str) -> list[tuple[str, str]]:
    """'section 27A(5)(a)' -> [(regulation,27A),(clause,5),(subclause,a)]."""
    lvl = LEVEL_MAP.get(_norm_unit(unit) if unit else "", None)
    parts = [p for p in re.findall(r"[0-9IVXLC]+[A-Za-z]?|[a-z]+", tref or "") if p]
    if not parts:
        return []
    chain = [(lvl or "regulation", norm_ref(parts[0]))]
    base = DESCEND.index(chain[0][0]) if chain[0][0] in DESCEND else 0
    for i, p in enumerate(parts[1:], 1):
        li = min(base + i, len(DESCEND) - 1)
        chain.append((DESCEND[li], norm_ref(p)))
    return chain


def scope_locators(*texts) -> list[tuple[str, str]]:
    locs = []
    for t in texts:
        if not t:
            continue
        for m in LOCATOR.finditer(t):
            lvl = LEVEL_MAP.get(_norm_unit(m.group("unit")))
            r = norm_ref(m.group("ref"))
            if lvl and r:
                locs.append((lvl, r))
    return locs


def anchor_edge(edge, provisions: list[dict], index: dict) -> tuple[dict | None, bool]:
    """Match a typed edge to a segmented provision via locator chains.
    index: (level, ref) -> [provision]. Returns (provision|None, ambiguous)."""
    chain = decompose_ref(edge.unit, edge.target_ref or edge.anchor or "")
    scope = scope_locators(edge.scope, edge.path)

    if chain:
        head_lvl, head_ref = chain[-1]
        cands = index.get((head_lvl, head_ref), [])
        if not cands:  # level uncertain: same ref at any level
            cands = [p for (lvl, r), ps in index.items() if r == head_ref for p in ps]
        context = chain[:-1] + scope
    elif scope:
        head_lvl, head_ref = scope[-1]         # word-level: anchor to deepest scope unit
        cands = index.get((head_lvl, head_ref), [])
        context = scope[:-1]
    else:
        return None, False

    if not cands:
        return None, False

    def _overlap(p):
        return len(set(context) & set(path_locators(p["path"])))

    if len(cands) == 1:
        p = cands[0]
        # a ghost reconstructed under one scope must not absorb events aimed
        # at the same ref under a different scope — force a fresh ghost
        if p.get("ghost") and context and not _overlap(p):
            return None, False
        if context and not _overlap(p):
            up = _scope_fallback(context, index)
            if up:
                return up
        return p, False

    scored = []
    for p in cands:
        ppath = set(path_locators(p["path"]))
        scored.append((len(set(context) & ppath), -len(p["path"]), p))
    scored.sort(key=lambda t: (-t[0], t[1]))
    if scored[0][0] == 0 and context:
        # no candidate lies under the edge's stated scope: anchoring to an
        # arbitrary same-ref sibling is a mis-anchor, so land the event on
        # the scope unit itself (e.g. the section row) instead
        up = _scope_fallback(context, index)
        if up:
            return up
        return None, False
    if len(scored) > 1 and scored[0][0] == scored[1][0]:
        return scored[0][2], True
    return scored[0][2], False


def _scope_fallback(context, index):
    for lvl, r in reversed(context):
        up = index.get((lvl, r), [])
        if up:
            return up[0], len(up) > 1
    return None


def prov_index(provisions: list[dict]) -> dict:
    idx = {}
    for p in provisions:
        idx.setdefault((p["level"], norm_ref(p["ref"])), []).append(p)
    return idx


def edge_detail(edge) -> str:
    if edge.level == "words":
        old = "; ".join(f"\u201c{o}\u201d" for o in (edge.old or []))
        if edge.action == "substitute":
            return f"{old} \u2192 \u201c{edge.new}\u201d" if edge.new else f"{old} substituted"
        if edge.action == "omit":
            return f"{old} omitted"
        if edge.action == "insert":
            return f"\u201c{edge.new}\u201d inserted" + (f" after {edge.anchor}" if edge.anchor else "")
    if edge.action == "renumber":
        return "renumbered"
    return (edge.source_line or "")[:120]


# ================= builder =================

def build(corpus: dict, only_slugs: set | None = None) -> tuple[dict, dict, Counter]:
    stats = Counter()
    lineages: dict[str, dict] = {}          # principal slug -> {provisions, amendments}
    maint: dict[str, list] = defaultdict(list)  # amendment slug -> notes
    seg_cache: dict[str, list] = {}

    amendments = [(s, d) for s, d in corpus.items() if is_amendment(s, d)]
    amendments.sort(key=lambda t: t[1].get("dateIssued") or "")
    stats["amendment_docs"] = len(amendments)

    for aslug, adoc in amendments:
        text = full_text(aslug)
        if not text:
            stats["amendment_no_text"] += 1
            continue
        res = parse_amendment(text)
        edges, unparsed = res["edges"], res["unparsed"]
        stats["edges_total"] += len(edges)
        stats["unparsed_lines"] += len(unparsed)

        # -- resolve principal --
        pslug, score = (None, 0.0)
        if res["principal"]:
            pslug, score = resolve_principal(res["principal"], corpus)
        if not pslug:
            own = re.sub(r"\((?:first|second|third|fourth|fifth|sixth|\d+(?:st|nd|rd|th))?\s*amendment\)?", "",
                         adoc.get("title", ""), flags=re.I)
            own = re.sub(r"\bamendments?\b", "", own, flags=re.I)
            pslug, score = resolve_principal(own, {k: v for k, v in corpus.items()
                                                   if k != aslug and not is_amendment(k, v)})
        if not pslug or pslug == aslug:
            stats["principal_unresolved"] += 1
            maint[aslug].append(f"principal unresolved (best score {score:.2f}) — "
                                f"parsed title: {res['principal'] or '—'}")
            continue
        if only_slugs and pslug not in only_slugs:
            continue
        stats["principal_resolved"] += 1

        # -- segment principal (cached); fall back to a consolidated variant's
        #    text when the resolved slug itself has no extracted text --
        if pslug not in seg_cache:
            ptext, ptext_from = full_text(pslug), pslug
            if not ptext:
                from provision_engine import _title_tokens
                want = _title_tokens(corpus[pslug].get("title", ""))
                for vs, vd in corpus.items():
                    if vs in (pslug, aslug) or is_amendment(vs, vd):
                        continue
                    if want and want <= _title_tokens(vd.get("title", "")):
                        vt = full_text(vs)
                        if vt:
                            ptext, ptext_from = vt, vs
                            break
            segs = segment_provisions(ptext, doc_id=pslug) if ptext else []
            # Dedup by legal identity rather than path id: a consolidated act
            # yields the same section from the arrangement-of-sections, the
            # body, and bilingual double-captures, each under a different
            # stack path. Section numbers are unique act-wide; clause refs are
            # unique within their parent section. Keep the first occurrence
            # (the TOC comes first and carries the clean heading).
            seen_keys = set()
            deduped = []
            for p in segs:
                if p["level"] in ("regulation", "schedule", "part", "chapter"):
                    key = (p["level"], norm_ref(p["ref"]))
                else:
                    parent = next((c for c in reversed(p["path"].split("/")[:-1])
                                   if c.startswith("reg-")), "")
                    key = (p["level"], norm_ref(p["ref"]), parent)
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                deduped.append(p)
            segs = deduped
            seg_cache[pslug] = {"provs": segs, "from": ptext_from if ptext else None}
            if not segs:
                stats["principal_no_text_or_flat"] += 1
        provisions = seg_cache[pslug]["provs"]
        pidx = prov_index(provisions)

        lin = lineages.setdefault(pslug, {"provisions": provisions, "amendments": [],
                                          "textFrom": seg_cache[pslug]["from"]})
        adate = adoc.get("dateIssued")
        applied = unresolved = 0

        for e in edges:
            if e.confidence == "low":
                unresolved += 1
                maint[aslug].append(f"low confidence, not applied: {e.source_line[:120]}")
                continue
            action = e.action if e.level != "words" else WORD_LEVEL_ACTION
            verified = e.confidence == "high"

            if e.action == "insert" and e.level == "unit":
                # new provision: introducedBy = this amendment instrument
                anchor_p, _ = anchor_edge(e, provisions, pidx) if (e.anchor or e.target_ref) else (None, False)
                ref = e.new_ref or e.target_ref or (e.anchor and f"after {e.anchor}") or "?"
                lvl = LEVEL_MAP.get(_norm_unit(e.unit) if e.unit else "clause", "clause")
                base_path = anchor_p["path"] if anchor_p else ""
                path_ref = re.sub(r"[^0-9A-Za-z]", "", norm_ref(str(ref))) or "NEW"
                path = (re.sub(r"[^/]+$", "", base_path) if base_path else "") + \
                       f"{lvl[:3]}-{path_ref}"
                newp = {"id": f"{pslug}#{path}", "level": lvl,
                        "ref": (e.new_ref or e.target_ref or ref), "heading": "",
                        "path": path,
                        "introducedBy": aslug, "introducedOn": adate,
                        "events": [{"action": "insert", "doc": aslug, "date": adate,
                                    "detail": edge_detail(e), "verified": verified}]}
                if not any(p["id"] == newp["id"] for p in provisions):
                    provisions.append(newp)
                    pidx.setdefault((newp["level"], norm_ref(newp["ref"])), []).append(newp)
                applied += 1
                stats["events_insert"] += 1
                continue

            target, ambiguous = anchor_edge(e, provisions, pidx)
            if not target and e.level == "unit" and (e.target_ref or e.anchor) \
                    and e.action in ("omit", "substitute"):
                # ghost provision: the amendment itself is evidence the clause
                # existed even if the segmented (often consolidated) text no
                # longer carries it
                chain = decompose_ref(e.unit, e.target_ref or e.anchor)
                if chain:
                    lvl, ref = chain[-1]
                    # display ref without the parsed parentheses — the frontend
                    # adds level-appropriate wrapping itself
                    disp = re.sub(r"^\(([^()]+)\)$", r"\1", (e.target_ref or e.anchor).strip())
                    # prefix the amendment's scope locators so clause (b) of
                    # regulation 7 and clause (b) of section 42D stay distinct
                    scope = [loc for loc in scope_locators(e.scope, e.path)
                             if loc not in chain]
                    full_chain = scope + chain
                    path = "/".join(f"{l[:3]}-{r}" for l, r in full_chain)
                    target = {"id": f"{pslug}#{path}", "level": lvl, "ref": disp,
                              "heading": "", "path": path, "ghost": True}
                    provisions.append(target)
                    pidx.setdefault((lvl, norm_ref(ref)), []).append(target)
                    stats["ghost_provisions"] += 1
            if not target:
                unresolved += 1
                maint[aslug].append(f"unanchored ({e.action} {e.unit or ''} "
                                    f"{e.target_ref or e.anchor or ''}): {e.source_line[:110]}")
                continue
            target.setdefault("events", [])
            ev = {"action": action, "doc": aslug, "date": adate,
                  "detail": edge_detail(e),
                  "verified": verified and not ambiguous}
            if any(x["action"] == ev["action"] and x["doc"] == ev["doc"]
                   and x["detail"] == ev["detail"] for x in target["events"]):
                stats["events_deduped"] += 1
                continue
            target["events"].append(ev)
            applied += 1
            stats[f"events_{action}"] += 1
            if ambiguous:
                stats["ambiguous_anchors"] += 1

        lin["amendments"].append({"doc": aslug, "date": adate, "edges": len(edges),
                                  "applied": applied, "unresolved": unresolved,
                                  "unparsed": len(unparsed)})
        for u in unparsed[:10]:
            maint[aslug].append(f"unparsed operative line: {u[:130]}")

    # trim payload: drop text bodies, keep heading/path/events; skip lineages
    # where nothing anchored and no amendments recorded
    out = {}
    for pslug, lin in lineages.items():
        provs = []
        for p in lin["provisions"]:
            provs.append({k: p.get(k) for k in
                          ("id", "level", "ref", "heading", "path", "ghost",
                           "introducedBy", "introducedOn", "events") if p.get(k) is not None})
        cutoffs = sorted({e["date"] for p in provs for e in (p.get("events") or []) if e.get("date")} |
                         {p["introducedOn"] for p in provs if p.get("introducedOn")})
        out[pslug] = {"provisions": provs, "amendments": lin["amendments"],
                      "cutoffs": cutoffs, "textFrom": lin.get("textFrom")}
        stats["lineages"] += 1
        stats["provisions_shipped"] += len(provs)
    return out, dict(maint), stats


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--slugs", nargs="*", help="restrict to these principal slugs")
    ap.add_argument("--report", action="store_true")
    args = ap.parse_args()

    corpus = load_corpus()
    out, maint, stats = build(corpus, set(args.slugs) if args.slugs else None)

    js = ("/* Auto-generated by build_provisions.py — do not edit by hand.\n"
          f"   Generated {datetime.now(timezone.utc).isoformat()} · "
          f"{stats['lineages']} lineages · {stats['provisions_shipped']} provisions. */\n"
          "window.IRDAI_PROVISIONS = "
          + json.dumps(out, ensure_ascii=False, separators=(",", ":"))
          + ";\nwindow.IRDAI_PROV_MAINT = "
          + json.dumps(maint, ensure_ascii=False, separators=(",", ":"))
          + ";\n")
    OUT_JS.write_text(js, encoding="utf-8")
    print(f"wrote {OUT_JS} ({OUT_JS.stat().st_size/1024:.0f} KB)")

    if args.report or True:
        for k in sorted(stats):
            print(f"  {k:28s} {stats[k]}")
        if maint:
            n = sum(len(v) for v in maint.values())
            print(f"  maintenance notes            {n} across {len(maint)} instruments")


if __name__ == "__main__":
    main()
