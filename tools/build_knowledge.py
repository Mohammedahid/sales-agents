"""Build knowledge/<brand>.md from a website + PDF profile(s).

Usage (on the server):
  docker compose run --rm app python tools/build_knowledge.py seen https://seen.example.com /srv/knowledge/seen-profile.pdf
  docker compose run --rm app python tools/build_knowledge.py nvolve https://nvolve.example.com /srv/knowledge/nvolve-profile.pdf

Afterwards open knowledge/<brand>.md, clean it up and add prices/packages by hand.
"""
import re
import sys
from collections import deque
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup
from pypdf import PdfReader

MAX_PAGES = 25
OUT_DIR = Path(__file__).resolve().parent.parent / "knowledge"


def crawl(start: str) -> list[tuple[str, str]]:
    host = urlparse(start).netloc
    seen, queue, pages = set(), deque([start]), []
    with httpx.Client(timeout=20, follow_redirects=True,
                      headers={"User-Agent": "Mozilla/5.0 knowledge-builder"}) as http:
        while queue and len(pages) < MAX_PAGES:
            url = queue.popleft().split("#")[0]
            if url in seen:
                continue
            seen.add(url)
            try:
                r = http.get(url)
            except Exception as e:
                print("skip", url, e)
                continue
            if "text/html" not in r.headers.get("content-type", ""):
                continue
            soup = BeautifulSoup(r.text, "html.parser")
            for tag in soup(["script", "style", "noscript", "svg", "nav", "footer"]):
                tag.decompose()
            title = soup.title.get_text(strip=True) if soup.title else url
            text = re.sub(r"\n{3,}", "\n\n", soup.get_text("\n", strip=True))
            if len(text) > 100:
                pages.append((f"{title} ({url})", text))
                print("ok  ", url, len(text))
            for a in soup.find_all("a", href=True):
                nxt = urljoin(url, a["href"])
                if urlparse(nxt).netloc == host and not re.search(r"\.(jpg|png|pdf|zip|mp4)$", nxt, re.I):
                    queue.append(nxt)
    return pages


def read_pdf(path: str) -> tuple[str, str]:
    reader = PdfReader(path)
    text = "\n".join((p.extract_text() or "") for p in reader.pages)
    return (f"البروفايل ({Path(path).name})", text)


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)
    brand, sources = sys.argv[1], sys.argv[2:]
    sections = []
    for src in sources:
        if src.startswith("http"):
            sections += crawl(src)
        elif src.lower().endswith(".pdf"):
            sections.append(read_pdf(src))
    out = OUT_DIR / f"{brand}-website.md"  # main summary stays in <brand>.md
    body = "\n\n".join(f"### {t}\n{x}" for t, x in sections)
    header = (
        "## ملخص سريع (عدّله بإيدك)\n"
        "- المنتجات/الخدمات:\n- الأسعار والباقات:\n- مدة التنفيذ/التسليم:\n"
        "- المدن اللي بنخدمها:\n- أسئلة متكررة:\n\n## محتوى من الموقع والبروفايل\n"
    )
    out.write_text(header + body, encoding="utf-8")
    print(f"\nwritten {out} ({len(body)} chars). راجع الملف وعدّل الملخص فوق.")


if __name__ == "__main__":
    main()
