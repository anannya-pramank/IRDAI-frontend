"""Figure out IRDAI's real pagination. The watcher's cur/delta query params
don't advance past page 1, so backfill never went beyond ~20/category. This
inspects: whether delta is honoured, whether cur=2 differs from cur=1, what the
page's own pagination controls point at, and the true total count."""
import re
import requests
from bs4 import BeautifulSoup

NS = "_com_irdai_document_media_IRDAIDocumentMediaPortlet_"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
URL = "https://irdai.gov.in/circulars"  # a bucket with many entries

sess = requests.Session()
sess.headers.update({"User-Agent": UA})


def fetch(cur=1, delta=20, extra=None):
    params = {NS + "delta": str(delta), NS + "cur": str(cur)}
    if extra:
        params.update(extra)
    return sess.get(URL, params=params, timeout=40, allow_redirects=True)


def row_ids(html):
    soup = BeautifulSoup(html, "html.parser")
    ids = []
    for cb in soup.select("table.table tbody input.checkSingle"):
        ids.append(cb.get("value"))
    if not ids:  # fall back to documentId in links
        for a in soup.select("table.table a[href*='documentId=']"):
            m = re.search(r"documentId=(\d+)", a["href"])
            if m:
                ids.append(m.group(1))
    return ids


def n_rows(html):
    return len(BeautifulSoup(html, "html.parser").select("table.table tbody tr"))


print("=== A/B: does cur advance? (delta=20) ===")
a = fetch(cur=1).text
b = fetch(cur=2).text
ida, idb = row_ids(a), row_ids(b)
print(f"  cur=1: {n_rows(a)} rows, first ids {ida[:3]}")
print(f"  cur=2: {n_rows(b)} rows, first ids {idb[:3]}")
print(f"  -> cur=2 {'DIFFERENT (pagination works!)' if ida[:3] != idb[:3] else 'SAME as cur=1 (cur ignored)'}")

print("\n=== C/D: is delta honoured? ===")
print(f"  delta=5  -> {n_rows(fetch(delta=5).text)} rows")
print(f"  delta=75 -> {n_rows(fetch(delta=75).text)} rows")

print("\n=== E: pagination controls in the page-1 HTML ===")
soup = BeautifulSoup(a, "html.parser")
seen = set()
for el in soup.select("[class*='pagination'], [class*='page-iterator'], "
                      "[class*='lfr-pagination'], ul.pagination, nav"):
    cls = " ".join(el.get("class", []))
    if cls and cls not in seen:
        seen.add(cls)
        print(f"  container class: {cls!r}")
anchors = []
for a_tag in soup.select("a[href]"):
    href = a_tag["href"]
    if "cur=" in href or "delta=" in href or "resetCur" in href or "page=" in href:
        anchors.append((a_tag.get_text(strip=True)[:12], href))
print(f"  anchors carrying cur/delta/page: {len(anchors)}")
for text, href in anchors[:8]:
    # show only the query part, trimmed
    q = href.split("?", 1)[1][:200] if "?" in href else href[:200]
    print(f"    [{text!r}] ...?{q}")

# any onclick / data-href pagination (JS-driven)?
js = soup.select("[onclick*='cur'], [data-href*='cur'], [data-uri*='cur']")
print(f"  JS-driven pagination elements: {len(js)}")
for el in js[:4]:
    print(f"    {el.name}: {(el.get('onclick') or el.get('data-href') or el.get('data-uri'))[:160]}")

print("\n=== F: total-count text ===")
text = soup.get_text(" ", strip=True)
for pat in [r"of\s+([\d,]+)\s+(?:entries|results|items)",
            r"\bof\s+([\d,]+)\b", r"Total[:\s]+([\d,]+)"]:
    m = re.search(pat, text, re.I)
    if m:
        print(f"  matched {pat!r}: total ~ {m.group(1)}")
        break
else:
    print("  no obvious total-count string found")
