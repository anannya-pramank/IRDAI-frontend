"""Follow Orders/Notices detail pages and, for every candidate file link,
dump: anchor text, decoded filename, token-overlap with the row title, and the
ancestor container trail. Goal: decide whether 'filename overlaps title' is a
safe discriminator for the real doc vs boilerplate (the stray 'Forms' link),
or whether a DOM container is cleaner."""
import re
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin, unquote_plus, urlsplit

BASE = "https://irdai.gov.in"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

# (label, detail path, the row title as seen in the listing)
DETAILS = [
    ("Orders", "/web/guest/document-detail?documentId=379542",
     "Committee for development of a model for sharing the motor vehicle database"),
    ("Notices", "/web/guest/document-detail?documentId=505536",
     "Appointment of Election Officer"),
]


def toks(s: str) -> set:
    return {w for w in re.findall(r"[a-z0-9]+", s.lower()) if len(w) >= 3}


def filename_of(href: str) -> str:
    """Human-readable filename segment from a Liferay /documents/ URL."""
    path = urlsplit(href).path
    segs = [unquote_plus(s) for s in path.split("/") if s]
    for s in segs:
        if s.lower().endswith((".pdf", ".doc", ".docx")):
            return s
    # else the longest mostly-alpha segment (skip numeric folder ids / uuids)
    cands = [s for s in segs if re.search(r"[A-Za-z]", s)
             and not re.fullmatch(r"[0-9a-f-]{16,}", s)]
    return max(cands, key=len) if cands else (segs[-1] if segs else "")


def container_trail(a, levels: int = 4) -> str:
    trail = []
    node = a.parent
    for _ in range(levels):
        if node is None or node.name is None:
            break
        ident = node.get("id")
        cls = " ".join(node.get("class", [])[:2])
        tag = node.name
        trail.append(tag + (f"#{ident}" if ident else "") + (f".{cls}" if cls else ""))
        node = node.parent
    return " < ".join(trail)


def probe(label: str, path: str, title: str) -> None:
    url = urljoin(BASE, path)
    print(f"\n{'='*70}\n### {label}: {title!r}\n    {url}")
    try:
        r = requests.get(url, headers={"User-Agent": UA}, timeout=40, allow_redirects=True)
    except Exception as e:
        print(f"    fetch error: {e}"); return
    print(f"    HTTP {r.status_code} | bytes: {len(r.text)}")
    soup = BeautifulSoup(r.text, "html.parser")
    t_title = toks(title)

    for a in soup.select("a[href]"):
        href = a["href"]
        low = href.lower()
        if not ("download=true" in low or "/documents/" in low
                or low.endswith((".pdf", ".doc", ".docx"))):
            continue
        full = urljoin(url, href)
        fname = filename_of(full)
        overlap = len(t_title & toks(fname)) / max(len(t_title), 1)
        dl = "download=true" in low
        print(f"\n    anchor_text : {a.get_text(strip=True)[:50]!r}")
        print(f"    filename    : {fname[:70]!r}")
        print(f"    title_match : {overlap:.0%}   download=true: {dl}")
        print(f"    container   : {container_trail(a)}")


def main() -> None:
    for label, path, title in DETAILS:
        probe(label, path, title)


if __name__ == "__main__":
    main()
