"""Compare the candidate Orders/Notices listing paths side by side, to find
which one is actually populated. Mirrors irdai_watcher's fetch + parse so the
result reflects exactly what the watcher would see."""
import requests
from bs4 import BeautifulSoup

NS = "_com_irdai_document_media_IRDAIDocumentMediaPortlet_"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

PAGES = {
    "Orders  (/orders)":  "https://irdai.gov.in/orders",
    "Orders  (/orders1)": "https://irdai.gov.in/orders1",
    "Notices (/notices)":  "https://irdai.gov.in/notices",
    "Notices (/notices1)": "https://irdai.gov.in/notices1",
    "Circulars (control)": "https://irdai.gov.in/circulars",
}


def probe(label: str, url: str) -> None:
    print(f"\n### {label}  ->  {url}")
    try:
        r = requests.get(url, params={NS + "delta": "20", NS + "cur": "1"},
                         headers={"User-Agent": UA}, timeout=40, allow_redirects=True)
    except Exception as e:
        print(f"    fetch error: {e}")
        return
    print(f"    HTTP {r.status_code} | final URL: {r.url} | bytes: {len(r.text)}")
    soup = BeautifulSoup(r.text, "html.parser")
    table = soup.select_one("table.table")
    if not table:
        body = soup.get_text(" ", strip=True).lower()
        hint = next((m for m in ("no record", "no data", "not found", "no result")
                     if m in body), None)
        print("    NO table.table found."
              + (f" Page says '{hint}'." if hint else " (JS-rendered / different markup?)"))
        return
    trs = table.select("tbody tr")
    multicol = sum(1 for tr in trs if len(tr.find_all("td")) >= 4)
    detail_links = len(table.select("a[href*='document-detail']"))
    print(f"    tbody rows: {len(trs)} | multi-col doc rows: {multicol} "
          f"| detail-page links: {detail_links}")
    for n, tr in enumerate(trs[:3]):
        tds = tr.find_all("td")
        cells = [td.get_text(strip=True)[:40] for td in tds]
        print(f"      row {n}: {len(tds)} cols -> {cells}")


def main() -> None:
    for label, url in PAGES.items():
        probe(label, url)


if __name__ == "__main__":
    main()
