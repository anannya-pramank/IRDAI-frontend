"""Follow the Orders/Notices single-row detail-page links and see how the
actual PDF(s) are exposed there — so we know whether the watcher can scrape
a real download URL to hand to the loader for chunking/embedding."""
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin

BASE = "https://irdai.gov.in"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

# documentId values pulled from the earlier probe_deep output
DETAILS = {
    "Orders / motor-vehicle-database committee": "/web/guest/document-detail?documentId=379542",
    "Notices / Appointment of Election Officer": "/web/guest/document-detail?documentId=505536",
}


def probe(label: str, path: str) -> None:
    url = urljoin(BASE, path)
    print(f"\n### {label}\n    {url}")
    try:
        r = requests.get(url, headers={"User-Agent": UA}, timeout=40, allow_redirects=True)
    except Exception as e:
        print(f"    fetch error: {e}"); return
    print(f"    HTTP {r.status_code} | final URL: {r.url} | bytes: {len(r.text)}")
    soup = BeautifulSoup(r.text, "html.parser")

    # every anchor whose href points at a document/download
    hits = []
    for a in soup.select("a[href]"):
        href = a["href"]
        low = href.lower()
        if "download=true" in low or "/documents/" in low or low.endswith(
                (".pdf", ".doc", ".docx")):
            hits.append((a.get_text(strip=True)[:50], urljoin(url, href)))
    print(f"    candidate file links: {len(hits)}")
    for text, href in hits[:8]:
        print(f"      - {text!r} -> {href[:110]}")

    # also surface any obvious title / date on the page for metadata
    h = soup.select_one("h1, h2, .portlet-title, .document-title")
    if h:
        print(f"    heading on page: {h.get_text(strip=True)[:80]}")


def main() -> None:
    for label, path in DETAILS.items():
        probe(label, path)


if __name__ == "__main__":
    main()
