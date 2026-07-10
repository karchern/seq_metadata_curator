"""Nature Publishing Group (DOI prefix 10.1038)."""
from __future__ import annotations

import csv
import re
import time
from pathlib import Path
from urllib.parse import unquote, urljoin

import requests
from bs4 import BeautifulSoup

from .base import Publisher, PublisherResult, SuppFile


class NaturePublisher(Publisher):
    doi_prefix = "10.1038"
    name = "nature"

    BROWSER_UA = (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    )

    def _article_url(self, doi: str) -> str:
        return f"https://www.nature.com/articles/{self.article_slug(doi)}"

    def _pdf_url(self, doi: str) -> str:
        return f"{self._article_url(doi)}.pdf"

    def _http_get(
        self, session: requests.Session, url: str, *, stream: bool = False
    ) -> requests.Response:
        delay = 1.0
        last_exc: Exception | None = None
        for _ in range(4):
            try:
                r = session.get(
                    url,
                    stream=stream,
                    timeout=90,
                    allow_redirects=True,
                    headers={"User-Agent": self.BROWSER_UA},
                )
                if r.status_code in (429, 500, 502, 503, 504):
                    last_exc = RuntimeError(f"HTTP {r.status_code} at {url}")
                    time.sleep(delay)
                    delay *= 2
                    continue
                return r
            except (requests.ConnectionError, requests.Timeout) as e:
                last_exc = e
                time.sleep(delay)
                delay *= 2
        raise RuntimeError(f"nature.http_get gave up: {last_exc}")

    def _download(
        self, session: requests.Session, url: str, dest: Path
    ) -> int:
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(dest.suffix + ".part")
        n = 0
        with self._http_get(session, url, stream=True) as r:
            r.raise_for_status()
            with tmp.open("wb") as fh:
                for chunk in r.iter_content(chunk_size=64 * 1024):
                    if chunk:
                        fh.write(chunk)
                        n += len(chunk)
        tmp.rename(dest)
        return n

    def probe_reachable(self, session: requests.Session, doi: str) -> bool:
        """Cheap check: HEAD/peek nature.com/articles/{slug}.pdf, look for %PDF."""
        return self._peek_pdf(session, self._pdf_url(doi))

    _ESM_URL_RE = re.compile(
        r'https?://static-content\.springer\.com/[^"\'\s<>]+/MediaObjects/[^"\'\s<>]+'
    )

    def probe_supp(self, session: requests.Session, doi: str) -> tuple[bool, int]:
        """Fetch article HTML, count ESM URLs. Cheap (one GET, no downloads)."""
        try:
            r = self._http_get(session, self._article_url(doi))
        except Exception:
            return (False, 0)
        if r.status_code != 200:
            return (False, 0)
        n = len(set(self._ESM_URL_RE.findall(r.text)))
        return (n > 0, n)

    def fetch_pdf(
        self, session: requests.Session, doi: str, out_dir: Path
    ) -> PublisherResult:
        result = PublisherResult()
        url = self._pdf_url(doi)
        result.attempts.append(f"nature_pdf:{url}")
        try:
            n = self._download(session, url, out_dir / "paper.pdf")
        except Exception as e:
            result.attempts.append(f"nature_pdf:fail:{e}")
            return result
        if n < 8192:
            (out_dir / "paper.pdf").unlink(missing_ok=True)
            result.attempts.append(f"nature_pdf:fail:size={n}")
            return result
        # Nature can serve HTML paywall/error pages under .pdf URLs (esp.
        # for author-manuscript-only or withdrawn articles). Sniff magic
        # bytes — same defence Springer + BMJ use.
        try:
            with (out_dir / "paper.pdf").open("rb") as fh:
                head = fh.read(8)
        except OSError:
            head = b""
        if not head.startswith(b"%PDF"):
            (out_dir / "paper.pdf").unlink(missing_ok=True)
            result.attempts.append(f"nature_pdf:fail:not_pdf:first8={head!r}")
            return result
        result.pdf_path = out_dir / "paper.pdf"
        result.pdf_url = url
        return result

    def fetch_supp(
        self, session: requests.Session, doi: str, out_dir: Path
    ) -> PublisherResult:
        """Parse the article HTML for MediaObjects supp links and download all.

        Nature supp files land at:
            https://static-content.springer.com/esm/art%3A{doi-encoded}
                /MediaObjects/{opaque-slug}_ESM.{ext}
        The article HTML links to each with a human label like
        'Supplementary Data 1' nearby — we harvest that label so downstream
        code can correlate filename → semantic role.
        """
        result = PublisherResult()
        art_url = self._article_url(doi)
        result.attempts.append(f"nature_article_html:{art_url}")
        try:
            r = self._http_get(session, art_url)
            r.raise_for_status()
        except Exception as e:
            result.attempts.append(f"nature_article_html:fail:{e}")
            return result

        html = r.text
        # 1) Collect every static-content.springer.com URL that points into
        #    MediaObjects. Deduplicate; preserve encounter order.
        url_re = re.compile(
            r'https?://static-content\.springer\.com/[^"\'\s<>]+/MediaObjects/[^"\'\s<>]+'
        )
        seen: dict[str, None] = {}
        for u in url_re.findall(html):
            if u not in seen:
                seen[u] = None
        urls = list(seen.keys())

        # 2) Harvest labels from the anchor context. Nature's DOM shape:
        #    <a href="{esm-url}" ...>Supplementary Data 1</a>
        soup = BeautifulSoup(html, "html.parser")
        label_by_url: dict[str, str] = {}
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if href.startswith("//"):
                href = "https:" + href
            elif href.startswith("/"):
                href = urljoin(art_url, href)
            if href in seen:
                label = a.get_text(strip=True)
                if label and href not in label_by_url:
                    label_by_url[href] = label

        # 3) Download.
        supp_dir = out_dir / "supp"
        supp_dir.mkdir(parents=True, exist_ok=True)
        manifest_rows: list[dict[str, str]] = []
        for url in urls:
            name = unquote(url.rsplit("/", 1)[-1])
            dest = supp_dir / name
            if dest.exists() and dest.stat().st_size > 0:
                result.attempts.append(f"nature_supp:skip_existing:{name}")
                bytes_written = dest.stat().st_size
            else:
                try:
                    bytes_written = self._download(session, url, dest)
                except Exception as e:
                    result.attempts.append(f"nature_supp:fail:{url}:{e}")
                    continue
            sf = SuppFile(
                url=url,
                filename=name,
                label=label_by_url.get(url),
                bytes_written=bytes_written,
            )
            result.supp_files.append(sf)
            manifest_rows.append(
                {
                    "filename": name,
                    "url": url,
                    "label": sf.label or "",
                    "bytes": str(bytes_written),
                }
            )

        if manifest_rows:
            with (supp_dir / "manifest.tsv").open("w", newline="") as fh:
                w = csv.DictWriter(
                    fh,
                    fieldnames=["filename", "url", "label", "bytes"],
                    delimiter="\t",
                )
                w.writeheader()
                w.writerows(manifest_rows)

        return result
