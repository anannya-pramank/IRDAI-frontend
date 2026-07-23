"""Check whether IRDAI's /orders and /notices Legal-menu listings actually
contain document rows, or are genuinely empty. Mirrors irdai_watcher's fetch
+ parse so the result reflects exactly what the watcher sees."""
import requests
from bs4 import BeautifulSoup

NS = "_com_irdai_document_media_IRDAIDocumentMediaPortlet_"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

PAGES = {
    "Orders": "https://irdai.gov.in/orders",
    "Notices": "https://irdai.gov.in/notices",
    # sanity control: a bucket we KNOW has rows, to prove the probe works
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
        # look for an explicit "no records" style message
        body = soup.get_text(" ", strip=True).lower()
        hint = next((m for m in ("no record", "no data", "not found", "no result")
                     if m in body), None)
        print(f"    NO table.table found."
              + (f" Page says '{hint}'." if hint else " (JS-rendered or different markup?)"))
        return
    trs = table.select("tbody tr")
    print(f"    table.table found | tbody rows: {len(trs)}")
    for n, tr in enumerate(trs[:2]):
        tds = tr.find_all("td")
        cells = [td.get_text(strip=True)[:45] for td in tds]
        print(f"      row {n}: {len(tds)} cols -> {cells}")


def main() -> None:
    for label, url in PAGES.items():
        probe(label, url)


if __name__ == "__main__":
    main()
