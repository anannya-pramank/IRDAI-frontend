"""Deeper look:
(1) dump the single Orders/Notices row's HTML — is it a real, linkable doc?
(2) per-column link/download flags for a Circulars row — which column holds
    the English title + PDF, so we can confirm the parser tags titles right."""
import requests
from bs4 import BeautifulSoup

NS = "_com_irdai_document_media_IRDAIDocumentMediaPortlet_"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")


def fetch(url: str) -> str:
    return requests.get(url, params={NS + "delta": "20", NS + "cur": "1"},
                        headers={"User-Agent": UA}, timeout=40,
                        allow_redirects=True).text


def dump_row_html(label: str, url: str) -> None:
    print(f"\n### {label} — raw HTML of the first row")
    tr = BeautifulSoup(fetch(url), "html.parser").select_one("table.table tbody tr")
    if not tr:
        print("    (no row)"); return
    html = " ".join(tr.decode().split())
    print("   ", html[:1400])


def dump_cols(label: str, url: str) -> None:
    print(f"\n### {label} — per-column link/download flags (row 0)")
    tr = BeautifulSoup(fetch(url), "html.parser").select_one("table.table tbody tr")
    if not tr:
        print("    (no row)"); return
    for i, td in enumerate(tr.find_all("td")):
        link = bool(td.select_one("a[href]"))
        dl = bool(td.select_one("a[href*='download=true']"))
        print(f"    td[{i}]: link={link} dl={dl} | {td.get_text(strip=True)[:55]}")


dump_row_html("Orders", "https://irdai.gov.in/orders")
dump_row_html("Notices", "https://irdai.gov.in/notices")
dump_cols("Circulars", "https://irdai.gov.in/circulars")
