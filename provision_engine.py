#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
provision_engine.py — prototype for clause-level traceability (IRDAI wiki).

Two passes:

  segment_provisions(text)   Principal-regulation text -> list of provision
                             records (ref, heading, hierarchy path, text span).
                             Each provision's "introducedBy" is its parent doc.

  parse_amendment(text)      Amendment-instrument text -> list of typed edges:
                             {action: substitute|insert|omit|renumber,
                              level: unit|words, unit, target_ref, scope,
                              old, new, confidence}
                             plus the resolved principal-regulation title.

Grammar covers the closed set of Indian gazette drafting formulas:
  - "For <unit> X [of <path>], the following ... shall be substituted"
  - "for the words/figures \"X\", the words \"Y\" shall be substituted/inserted"
  - "the words 'X' shall be substituted with/as 'Y'"        (inverted form)
  - "[the] heading \"X\" shall be substituted as \"Y\""
  - "after <unit> X [of <path>], the following ... shall be inserted"
  - "the following ... shall be inserted after/before <anchor> [as <ref>]"
  - "<unit> X [of <path>] shall be omitted"
  - "the words/figures \"X\" shall be omitted"
  - "Throughout <scope>, for the words \"X\", the words \"Y\" shall be substituted"
  - "shall be renumbered"                                    (flag only)

Confidence: high  = explicit unit+ref (or global word-sub) with inline path
            medium= unit+ref, scope inherited from an "In <path>:" header
            low   = operative verb matched but operands only partially parsed
Anything with an operative verb that no rule matches is reported as UNPARSED.
"""

from __future__ import annotations
import re
from dataclasses import dataclass, field, asdict

Q = r"[\"\u201c\u201d'\u2018\u2019]"          # straight + curly quotes
QTEXT = rf"{Q}(?P<%s>[^\"\u201c\u201d\u2018\u2019]+?){Q}"

UNIT = (r"(?:sub[\s-]?regulations?|sub[\s-]?rules?|sub[\s-]?clauses?|"
        r"sub[\s-]?sections?|regulations?|rules?|clauses?|sections?|"
        r"notes?|provisos?|parts?|schedules?|chapters?|forms?|annexures?)")
# 7 | 7(12)(i) | 27A | IV | 2(4) | IIA | 1(3) | (2) | IV(A) | XIII
REF = r"(?:\(?(?:[0-9IVXLC]+[A-Za-z]?|[a-z])\)?(?:\s*\([0-9a-zA-Zivx]+\))*)"

WORDY = (r"(?:words?\s+and\s+figures?|word\s+and\s+figures?|"
         r"figures?\s+and\s+letters?|words?|word|figures?|phrases?|letters?)")

def _q(name):  # named quoted capture
    return QTEXT % name

FLAGS = re.I

# ---------- operative-line detector (denominator for hit rate) ----------
OPERATIVE = re.compile(r"shall be (?:substituted|inserted|omitted|renumbered|deleted)", FLAGS)

# ---------- scope headers ----------
# "6. In Part II of the Schedule I of principal Regulations:"  /  "12. In the clause 2 of Part II of the Schedule III of principal Regulations"
SCOPE_HDR = re.compile(
    rf"^(?:\(?\d+\)?[\.\)]?\s*|[a-z][\.\)]\s*|[ivx]+\.\s*)?In\s+(?:the\s+)?(?P<path>(?:{UNIT})\s*\(?{REF}\)?.*?)\s*[:,\u2013\u2014-]*\s*$",
    FLAGS)
SCHEDULE_HDR = re.compile(r"^SCHEDULE\s*[-\u2013\u2014]?\s*(?P<sch>[IVXLC]+A?)\b", FLAGS)
# "3. Chapter III of principal Regulations: For Regulation 7, ..."
INLINE_SCOPE = re.compile(rf"^(?:\d+[A-Za-z]?[\.\)]\s*)?(?P<path>(?:{UNIT})\s+{REF}(?:\s+of\s+(?:the\s+)?(?:{UNIT})\s*[-\s]?{REF})*\s+of\s+(?:the\s+)?principal\s+Regulations?)\s*:\s*(?P<rest>.+)$", FLAGS)

# ---------- rules, tried in order ----------
RULES = []
def rule(name, pattern, builder):
    RULES.append((name, re.compile(pattern, FLAGS), builder))

PATH = rf"(?P<path>(?:(?:{UNIT})\s*[-\s]?\(?{REF}\)?(?:\s+(?:of|to)\s+(?:the\s+)?)?)+(?:of\s+)?(?:the\s+)?(?:principal\s+Regulations?|Regulations?)?)"

# renumber (flag before anything else)
rule("renumber",
     r"shall be renumbered",
     lambda m, s: dict(action="renumber", level="unit", confidence="low"))

# global / scoped word substitution: "Throughout <scope>, for the words "X" [, "X2" ...], the word(s) "Y" shall be substituted"
rule("global_word_sub",
     rf"Throughout\s+(?:the\s+)?(?P<scope>[^,]+?),?\s*for\s+the\s+{WORDY}\s+(?P<olds>{Q}[^\"\u201c\u201d\u2018\u2019]+{Q}(?:\s*,\s*{Q}[^\"\u201c\u201d\u2018\u2019]+{Q})*(?:\s*and\s+{Q}[^\"\u201c\u201d\u2018\u2019]+{Q})?)\s*,?\s*the\s+{WORDY}\s+{_q('new')}\s+shall\s+be\s+substituted",
     lambda m, s: dict(action="substitute", level="words", scope=m.group("scope").strip(),
                       old=re.findall(rf"{Q}([^\"\u201c\u201d\u2018\u2019]+?){Q}", m.group("olds")),
                       new=m.group("new"), confidence="high", is_global=True))

# forward word substitution/insertion: "[In X,] for the words "A", the words "B" shall be substituted/inserted"
rule("word_sub_fwd",
     rf"for\s+the\s+{WORDY}\s+{_q('old')}\s*,?\s*(?P<wherever>wherever\s+(?:it|they)\s+occurs?\s*,?\s*)?the\s+{WORDY}\s+{_q('new')}\s+shall\s+be\s+(?:substituted|inserted)",
     lambda m, s: dict(action="substitute", level="words", old=[m.group("old")],
                       new=m.group("new"), confidence=None,
                       is_global=bool(m.group("wherever"))))

# paired substitution: 'the words "A", "B" and "C" shall be substituted with "D", "E" and "F" respectively'
rule("word_sub_paired",
     rf"the\s+{WORDY}\s+(?P<olds>{Q}[^\"\u201c\u201d\u2018\u2019]+{Q}(?:\s*(?:,|or|and)\s*{Q}[^\"\u201c\u201d\u2018\u2019]+{Q})*)\s+shall\s+be\s+substituted\s+(?:with|as|by)\s+(?:the\s+{WORDY}\s*,?\s*)?(?P<news>{Q}[^\"\u201c\u201d\u2018\u2019]+{Q}(?:\s*(?:,|or|and)\s*{Q}[^\"\u201c\u201d\u2018\u2019]+{Q})*)\s+respectively",
     lambda m, s: dict(action="substitute", level="words", paired=True,
                       old=re.findall(rf"{Q}([^\"\u201c\u201d\u2018\u2019]+?){Q}", m.group("olds")),
                       new=re.findall(rf"{Q}([^\"\u201c\u201d\u2018\u2019]+?){Q}", m.group("news")),
                       confidence=None))

# inverted word substitution: "the words 'X' shall be substituted with/as 'Y'"
rule("word_sub_inv",
     rf"the\s+{WORDY}\s*,?\s*(?P<olds>{Q}[^\"\u201c\u201d\u2018\u2019]+{Q}(?:\s*(?:,|or|and)\s*{Q}[^\"\u201c\u201d\u2018\u2019]+{Q})*)\s+shall\s+be\s+substituted\s+(?:with|as|by)\s+(?:the\s+{WORDY}\s*,?\s*)?{_q('new')}",
     lambda m, s: dict(action="substitute", level="words",
                       old=re.findall(rf"{Q}([^\"\u201c\u201d\u2018\u2019]+?){Q}", m.group("olds")),
                       new=m.group("new"), confidence=None))

# heading substitution: 'The heading "X" shall be substituted as "Y"' / 'the heading shall be substituted as "Y"'
rule("heading_sub",
     rf"(?:(?P<hu>Chapter|Part|Schedule|Regulation)\s*[\u2013\u2014-]?\s*(?P<hr>{REF})\s+)?heading\s+(?:{_q('old')}\s+)?shall\s+be\s+substituted\s*,?\s+(?:as|with)(?:\s+under)?\s*[:\u2013\u2014-]*\s*(?:{_q('new')}|(?P<new_plain>[A-Z][^.\n]{{4,110}}))",
     lambda m, s: dict(action="substitute", level="unit", unit="heading",
                       target_ref=(f"{m.group('hu')} {m.group('hr')}" if m.group('hu') else None),
                       old=[m.group("old")] if m.group("old") else None,
                       new=m.group("new") or (m.group("new_plain") or "").strip(),
                       confidence="medium" if m.group("new_plain") else None))

# word insertion after anchor words: 'after the words "X", the words "Y" shall be inserted'
rule("word_insert_after",
     rf"after\s+the\s+{WORDY}\s+{_q('anchor')}\s*,?\s*the\s+{WORDY}\s+{_q('new')}\s+shall\s+be\s+inserted",
     lambda m, s: dict(action="insert", level="words", anchor=m.group("anchor"),
                       new=m.group("new"), confidence=None))

# word/figure omission: 'the word and figures "X" shall be omitted' (possibly a list)
rule("word_omit",
     rf"the\s+{WORDY}\s+(?P<olds>{Q}[^\"\u201c\u201d\u2018\u2019]+{Q}(?:\s*,\s*{Q}[^\"\u201c\u201d\u2018\u2019]+{Q})*(?:\s*and\s+{Q}[^\"\u201c\u201d\u2018\u2019]+{Q})?)\s+shall\s+be\s+omitted",
     lambda m, s: dict(action="omit", level="words",
                       old=re.findall(rf"{Q}([^\"\u201c\u201d\u2018\u2019]+?){Q}", m.group("olds")),
                       confidence=None))

# unit substitution: "For [the existing] <unit> X [of <path>], the following ... shall be substituted"
rule("unit_sub",
     rf"For\s+(?:the\s+existing\s+)?(?P<unit>{UNIT})\s+(?P<ref>{REF})\s*(?:of\s+(?P<path>[^,]+?))?\s*,?\s*the\s+(?:following|existing)\b.*?shall\s+be\s+substituted",
     lambda m, s: dict(action="substitute", level="unit", unit=_norm_unit(m.group("unit")),
                       target_ref=m.group("ref").strip(), path=(m.group("path") or "").strip() or None,
                       confidence=None))

# unit-first substitution: "<unit> X [and Y] [of <unit> Z][,] shall be substituted[, namely / as under]"
rule("unit_sub_first",
     rf"(?P<unit>{UNIT})\s+(?P<refs>{REF}(?:\s*(?:,|and)\s*{REF})*)\s*(?:of\s+(?P<path>(?:{UNIT})\s*\(?{REF}\)?))?\s*,?\s*shall\s+be\s+substituted",
     lambda m, s: dict(action="substitute", level="unit", unit=_norm_unit(m.group("unit")),
                       target_ref=m.group("refs").strip(),
                       path=(m.group("path") or "").strip() or None, confidence=None))

# note substituted for note N: "the following note shall be substituted for note VII"
rule("note_sub_for",
     rf"the\s+following\s+(?P<unit>note)s?\s+shall\s+be\s+substituted\s+for\s+note\s+(?P<ref>{REF})",
     lambda m, s: dict(action="substitute", level="unit", unit="note",
                       target_ref=m.group("ref").strip(), confidence=None))

# insert after unit: "After <unit>? X [of <path>], the following ... shall be inserted"
rule("insert_after",
     rf"After\s+(?:the\s+)?(?:(?P<unit>{UNIT})\s+)?(?P<ref>{REF})\s*(?:of\s+(?P<path>[^,]+?))?\s*,?\s*the\s+following\b.*?shall\s+be\s+inserted",
     lambda m, s: dict(action="insert", level="unit", unit=_norm_unit(m.group("unit") or "clause"),
                       anchor=m.group("ref").strip(), position="after",
                       path=(m.group("path") or "").strip() or None, confidence=None))

# "After <unit> X, new <unit> A and <unit> B shall be inserted"
rule("insert_new_units",
     rf"After\s+(?:the\s+)?(?P<aunit>{UNIT})\s+(?P<anchor>{REF})\s*,?\s*new\s+(?P<news>(?:{UNIT})\s+{REF}(?:\s+and\s+(?:{UNIT})\s+{REF})*)\s+shall\s+be\s+inserted",
     lambda m, s: dict(action="insert", level="unit", unit=_norm_unit(m.group("aunit")),
                       anchor=m.group("anchor").strip(), position="after",
                       new_ref=m.group("news").strip(), confidence=None))

# insert inverted: "the following <unit>? shall be inserted after <anchor> [as <ref>]"
rule("insert_after_inv",
     rf"the\s+following(?:\s+(?P<unit>{UNIT}|section))?s?\s+shall\s+be\s+inserted\s+after\s+(?:the\s+)?(?:(?P<aunit>{UNIT})\s+)?(?P<anchor>{REF})(?:\s+as\s+(?:{UNIT})\s+(?P<newref>{REF}))?",
     lambda m, s: dict(action="insert", level="unit", unit=_norm_unit(m.group("unit") or m.group("aunit") or "clause"),
                       anchor=m.group("anchor").strip(), position="after",
                       new_ref=(m.group("newref") or "").strip() or None, confidence=None))

# insert before: "<content> shall be inserted before Clause N" / "Before Clause 1, the following shall be inserted"
rule("insert_before",
     rf"(?:shall\s+be\s+inserted\s+before\s+(?:the\s+)?(?P<unit>{UNIT})\s+(?P<ref>{REF})|Before\s+(?:the\s+)?(?P<unit2>{UNIT})\s+(?P<ref2>{REF})\s*,?\s*the\s+following\b.*?shall\s+be\s+inserted)",
     lambda m, s: dict(action="insert", level="unit",
                       unit=_norm_unit(m.group("unit") or m.group("unit2")),
                       anchor=(m.group("ref") or m.group("ref2")).strip(),
                       position="before", confidence=None))

# unit omission: "[The] <unit> X [of <path>] shall be omitted"
rule("unit_omit",
     rf"(?:The\s+)?(?P<unit>{UNIT})\s+(?P<ref>{REF})\s*(?:of\s+(?P<path>[^,]+?))?\s+shall\s+be\s+omitted",
     lambda m, s: dict(action="omit", level="unit", unit=_norm_unit(m.group("unit")),
                       target_ref=m.group("ref").strip(),
                       path=(m.group("path") or "").strip() or None, confidence=None))


# defective-drafting fallback: '"X" ... "Y" shall be substituted' with the "for" missing
rule("word_sub_defective",
     rf"{_q('old')}\s*,?\s*(?:the\s+{WORDY}\s+)?{_q('new')}\s+shall\s+be\s+substituted",
     lambda m, s: dict(action="substitute", level="words", old=[m.group("old")],
                       new=m.group("new"), confidence="low"))


def _norm_unit(u: str) -> str:
    u = re.sub(r"[\s-]+", "-", u.strip().lower()).rstrip("s") if u else u
    return {"sub-regulation": "sub-regulation", "sub-clause": "sub-clause",
            "sub-rule": "sub-rule", "sub-section": "sub-section"}.get(u, u)


# "In clause (e) of regulation 2, ..." / "In the sub-clause (1) ..." / "In sub clause 5(iii), ..."  -> inline location prefix
LOC_PREFIX = re.compile(
    rf"^(?:[0-9a-z]+[\.\)]\s*|\([0-9a-z]+\)\s*|[ivx]+\.\s*)*In\s+(?:the\s+)?(?P<loc>(?:{UNIT})\s*\(?{REF}\)?(?:\s+of\s+(?:the\s+)?(?:{UNIT})\s*[-\s]?\(?{REF}\)?)*)",
    FLAGS)

PRINCIPAL_RE = re.compile(
    r"(?:amend(?:ments?\s+to)?|to\s+amend)\s+(?:the\s+)?(?P<title>(?:Insurance\s+Regulatory|IRDAI?|IRDA).*?(?:Regulations?|Rules?)\s*,?\s*(?:19|20)\d\d)",
    FLAGS)


@dataclass
class Edge:
    action: str
    level: str
    unit: str | None = None
    target_ref: str | None = None
    anchor: str | None = None
    position: str | None = None
    new_ref: str | None = None
    old: list | None = None
    new: str | None = None
    scope: str | None = None
    path: str | None = None
    confidence: str = "medium"
    source_line: str = ""
    is_global: bool = False
    paired: bool = False
    exception: str | None = None


def parse_amendment(text: str) -> dict:
    """Parse an amendment instrument's operative text into typed edges."""
    principal = None
    m = PRINCIPAL_RE.search(text)
    if m:
        principal = re.sub(r"\s+", " ", m.group("title")).strip()

    edges, unparsed = [], []
    schedule_scope, item_scope = None, None
    pending_ws = False  # inside a quoted replacement block after "namely:"

    for raw in text.splitlines():
        line = re.sub(r"\s+", " ", raw).strip()
        if not line:
            continue

        sh = SCHEDULE_HDR.match(line)
        if sh:
            schedule_scope, item_scope = f"Schedule {sh.group('sch')}", None
            continue

        operative = bool(OPERATIVE.search(line))

        # numbered top-level item resets the item scope — but only when the
        # line is itself an instruction or scope header; numbered headings
        # inside quoted replacement bodies must not clobber the scope stack.
        if re.match(r"^\d+[\.\)]\s", line) and (operative or SCOPE_HDR.match(line)):
            item_scope = None

        # "In <path>:" header (no operative verb on the line) sets item scope
        if not operative:
            hm = SCOPE_HDR.match(line)
            if hm:
                item_scope = re.sub(r"\s+", " ", hm.group("path")).strip(" ,;:-\u2013\u2014")
            continue

        # inline scope prefix: "N. <Unit X of principal Regulations>: <instruction>"
        im = INLINE_SCOPE.match(line)
        inline_path = None
        if im:
            inline_path = im.group("path").strip()
            line_body = im.group("rest")
        else:
            line_body = line
            # scope headers that ALSO carry the instruction ("12. In the clause 2 ... a) in the sub-clause (1) for the word ...")
            hm = SCOPE_HDR.match(line)
            if hm and OPERATIVE.search(line):
                item_scope = None  # path is inline; LOC_PREFIX below will catch it

        loc = None
        lm = LOC_PREFIX.match(line_body)
        if lm:
            loc = re.sub(r"\s+", " ", lm.group("loc")).strip()

        # collect matches across ALL rules; earlier rules win on span overlap,
        # so multi-instruction lines ("X shall be omitted. After Y ... inserted")
        # yield one edge per instruction.
        candidates = []
        for prio, (name, pat, build) in enumerate(RULES):
            for m in pat.finditer(line_body):
                candidates.append((prio, m.start(), m.end(), m, build))
        candidates.sort(key=lambda c: (c[0], c[1]))
        taken, matched = [], []
        for prio, a, b, m, build in candidates:
            if any(not (b <= ta or a >= tb) for ta, tb in taken):
                continue
            taken.append((a, b))
            d = build(m, line_body)
            d.setdefault("scope", None)
            e = Edge(**{k: v for k, v in d.items() if k in Edge.__dataclass_fields__})
            e.source_line = line[:140]
            e.path = e.path or inline_path
            e.scope = e.scope or loc or item_scope or schedule_scope
            if e.confidence is None:
                if e.path or (e.level == "words" and (loc or e.scope)):
                    e.confidence = "high"
                elif e.scope:
                    e.confidence = "medium"
                else:
                    e.confidence = "low"
            xm = re.search(r"(except\s+where\b[^.\n]*|unless\s+the\s+context\s+otherwise\s+requires[^.\n]*|save\s+as\s+otherwise\b[^.\n]*)",
                           line_body, re.I)
            if xm:
                e.exception = xm.group(1).strip()
                if e.confidence == "high":
                    e.confidence = "medium"
            # expand multi-ref unit targets and paired word lists into atomic edges
            expanded = []
            if e.level == "unit" and e.target_ref and re.search(r"\b(?:and|,)\b", e.target_ref):
                for r in re.split(r"\s*(?:,|and)\s*", e.target_ref):
                    if r.strip():
                        e2 = Edge(**{**asdict(e), "target_ref": r.strip()})
                        expanded.append(e2)
            elif e.paired and isinstance(e.new, list):
                if len(e.old or []) == len(e.new):
                    for o, n in zip(e.old, e.new):
                        expanded.append(Edge(**{**asdict(e), "old": [o], "new": n, "paired": False}))
                else:
                    e.confidence = "low"  # unequal pairing — flag, don't guess
                    expanded.append(e)
            else:
                expanded.append(e)
            for e2 in expanded:
                matched.append((a, e2))
                a += 0  # keep source ordering stable
        matched = [e for _, e in sorted(matched, key=lambda t: t[0])]

        if matched:
            edges.extend(matched)
        elif operative:
            unparsed.append(line[:160])

    return {"principal": principal, "edges": edges, "unparsed": unparsed}


# ================= SEGMENTER =================

SEG_PATTERNS = [
    ("schedule", re.compile(r"^SCHEDULE\s*[-\u2013\u2014]?\s*([IVXLC]+A?)\b[\s:\u2013\u2014-]*(.*)$", re.I)),
    ("part",     re.compile(r"^(?:\*\*)?Part\s+([IVXLC]+)\s*(?:\(([A-Z])\))?[\s:.\u2013\u2014-]*(.*)$", re.I)),
    ("chapter",  re.compile(r"^(?:\*\*)?Chapter\s+([IVXLC]+)\b[\s:.\u2013\u2014-]*(.*)$", re.I)),
    ("regulation", re.compile(r"^(?:\*\*)?(\d+[A-Z]?)\.\s+([A-Z\u201c\"].{3,120})$")),
    ("clause",   re.compile(r"^\((\d+[A-Z]?)\)\s+(.*)$")),
    ("subclause", re.compile(r"^\(([a-z]|[ivx]+)\)\s+(.*)$")),
]

def segment_provisions(text: str, doc_id: str = "doc") -> list[dict]:
    """Split principal-regulation text into hierarchical provision records."""
    provisions, stack = [], {}   # stack: level -> ref
    order = ["schedule", "part", "chapter", "regulation", "clause", "subclause"]
    buf_target = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        hit = None
        for level, pat in SEG_PATTERNS:
            m = pat.match(line)
            if m:
                hit = (level, m)
                break
        if hit:
            level, m = hit
            ref = m.group(1)
            heading = (m.groups()[-1] or "").strip().strip("*")
            # clear deeper levels
            for l in order[order.index(level) + 1:]:
                stack.pop(l, None)
            if level in ("schedule",):
                stack.pop("regulation", None); stack.pop("chapter", None)
            stack[level] = ref
            path = "/".join(f"{l[:3]}-{stack[l]}" for l in order if l in stack)
            rec = {"id": f"{doc_id}#{path}", "level": level, "ref": ref,
                   "heading": heading[:120], "path": path, "text": ""}
            provisions.append(rec)
            buf_target = rec
        elif buf_target is not None:
            buf_target["text"] += (" " if buf_target["text"] else "") + line
    return provisions


# ================= PRINCIPAL RESOLUTION =================

_STOP = {"of", "the", "and", "for", "india", "indian"}
_ABBREV = [
    (re.compile(r"\birdai?\b|\birda\b", re.I), "insurance regulatory development authority"),
    (re.compile(r"\breg\b\.?", re.I), "regulations"),
    (re.compile(r"\bregulation\b", re.I), "regulations"),
]

def _title_tokens(t: str) -> set:
    t = re.sub(r"[^A-Za-z0-9]+", " ", t or "").lower()
    for pat, exp in _ABBREV:
        t = pat.sub(exp, t)
    return {w for w in t.split() if w not in _STOP}


def resolve_principal(principal_title: str, corpus: dict, threshold: float = 0.7):
    """Match a parsed principal-regulation title to a corpus slug.

    Exact-ish containment on normalized tokens first; then Jaccard on
    content tokens (abbreviations expanded, stopwords dropped). Returns
    (slug, score) or (None, best_score) for the pending-relations log.
    """
    pt = _title_tokens(principal_title)
    if not pt:
        return None, 0.0
    best, best_score = None, 0.0
    for sid, d in corpus.items():
        ct = _title_tokens(d.get("title", ""))
        if not ct:
            continue
        score = len(pt & ct) / len(pt | ct)
        if pt <= ct or ct <= pt:
            score = max(score, 0.99)
        if score > best_score:
            best, best_score = sid, score
    return (best, best_score) if best_score >= threshold else (None, best_score)


if __name__ == "__main__":
    import json, sys
    for f in sys.argv[1:]:
        res = parse_amendment(open(f, encoding="utf-8").read())
        print(json.dumps({"file": f, "principal": res["principal"],
                          "edges": [asdict(e) for e in res["edges"]],
                          "unparsed": res["unparsed"]}, indent=1, ensure_ascii=False))
