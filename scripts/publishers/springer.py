"""Springer non-Nature (DOI prefix 10.1007) — link.springer.com.

Springer serves article PDFs at a fully deterministic URL:

    https://link.springer.com/content/pdf/{DOI}.pdf

Empirically (verified 2026-07-10 on the 17 PDF-NONE 10.1007 papers in this
corpus) this endpoint returns application/pdf with %PDF magic bytes for BOTH
open-access and subscription-only articles — the CDN does not gate on IP
entitlement the way ScienceDirect does. This is unusual but consistent.

Supplementary files use the same static-content.springer.com/esm/.../MediaObjects
CDN as Nature Publishing Group (10.1038), so the supp-fetch logic here mirrors
NaturePublisher.fetch_supp() near-verbatim — the only difference is the
article HTML lives at link.springer.com/article/{DOI} rather than
www.nature.com/articles/{slug}.
"""
from __future__ import annotations

import csv
import re
import time
from pathlib import Path
from urllib.parse import unquote, urljoin

import requests
from bs4 import BeautifulSoup

from .base import Publisher, PublisherResult, SuppFile


class SpringerPublisher(Publisher):
    doi_prefix = "10.1007"
    name = "springer"

    BROWSER_UA = (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    )

    def _article_url(self, doi: str) -> str:
        return f"https://link.springer.com/article/{doi}"

    def _chapter_url(self, doi: str) -> str:
        """Springer book chapters live at /chapter/{doi}, not /article/{doi}.

        DOI patterns like `10.1007/978-{ISBN-suffix}_{chapter_num}` and other
        book-chapter DOIs 404 on /article/ but 200 on /chapter/.
        """
        return f"https://link.springer.com/chapter/{doi}"

    def _pdf_url(self, doi: str) -> str:
        return f"https://link.springer.com/content/pdf/{doi}.pdf"

    def article_html_url(self, session: requests.Session, doi: str) -> str | None:
        """Return whichever of /article/{doi} or /chapter/{doi} responds 200.

        Callers (publisher supp probing / scraping) should use this rather
        than hardcoding /article/, so book chapters aren't silently dropped.
        """
        for url in (self._article_url(doi), self._chapter_url(doi)):
            try:
                r = self._http_get(session, url)
            except Exception:
                continue
            if r.status_code == 200:
                return url
        return None

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
                    try:
                        r.close()  # avoid socket/fd leak on retry
                    except Exception:
                        pass
                    time.sleep(delay)
                    delay *= 2
                    continue
                return r
            except (requests.ConnectionError, requests.Timeout) as e:
                last_exc = e
                time.sleep(delay)
                delay *= 2
        raise RuntimeError(f"springer.http_get gave up: {last_exc}")

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
        """Cheap check: peek link.springer.com/content/pdf/{doi}.pdf for %PDF."""
        return self._peek_pdf(session, self._pdf_url(doi))

    _ESM_URL_RE = re.compile(
        r'https?://static-content\.springer\.com/[^"\'\s<>]+/MediaObjects/[^"\'\s<>]+'
    )

    def probe_supp(self, session: requests.Session, doi: str) -> tuple[bool, int]:
        """Try /article/{doi} then /chapter/{doi}; count ESM URLs on whichever
        returns 200. Handles both regular articles and book chapters.

        We DO catch per-candidate exceptions here because 404 on /article/
        (book chapter) is expected + benign and we want to try /chapter/
        next. But if ALL candidates raise, we re-raise the final one so
        the caller's regression guard fires on genuine network failure.
        """
        html: str | None = None
        last_exc: Exception | None = None
        for candidate in (self._article_url(doi), self._chapter_url(doi)):
            try:
                r = self._http_get(session, candidate)
            except Exception as e:
                last_exc = e
                continue
            if r.status_code == 200:
                html = r.text
                break
        if html is None:
            if last_exc is not None:
                raise last_exc
            return (False, 0)
        n = len(set(self._ESM_URL_RE.findall(html)))
        return (n > 0, n)

    def fetch_pdf(
        self, session: requests.Session, doi: str, out_dir: Path
    ) -> PublisherResult:
        result = PublisherResult()
        url = self._pdf_url(doi)
        result.attempts.append(f"springer_pdf:{url}")
        try:
            n = self._download(session, url, out_dir / "paper.pdf")
        except Exception as e:
            result.attempts.append(f"springer_pdf:fail:{e}")
            return result
        if n < 8192:
            (out_dir / "paper.pdf").unlink(missing_ok=True)
            result.attempts.append(f"springer_pdf:fail:size={n}")
            return result
        # Guard against the CDN returning an HTML paywall page with a wrong
        # content-type by sniffing the magic bytes.
        try:
            with (out_dir / "paper.pdf").open("rb") as fh:
                head = fh.read(8)
        except OSError:
            head = b""
        if not head.startswith(b"%PDF"):
            (out_dir / "paper.pdf").unlink(missing_ok=True)
            result.attempts.append(f"springer_pdf:fail:not_pdf:first8={head!r}")
            return result
        result.pdf_path = out_dir / "paper.pdf"
        result.pdf_url = url
        return result

    def fetch_supp(
        self, session: requests.Session, doi: str, out_dir: Path
    ) -> PublisherResult:
        """Parse article HTML for static-content.springer.com MediaObjects links.

        Springer's non-Nature journals mirror Nature's supp-hosting scheme:
        supplementary files live at
            https://static-content.springer.com/esm/art%3A{doi-encoded}
                /MediaObjects/{opaque-slug}_ESM.{ext}
        and are linked with a human label (e.g. 'Supplementary Data 1') on
        the article landing page.

        Uses article_html_url() so book-chapter DOIs (which live at
        /chapter/{doi} rather than /article/{doi}) are also covered.
        """
        result = PublisherResult()
        art_url = self.article_html_url(session, doi)
        if art_url is None:
            result.attempts.append(
                f"springer_article_html:no_reachable_url_for_doi={doi}"
            )
            return result
        result.attempts.append(f"springer_article_html:{art_url}")
        try:
            r = self._http_get(session, art_url)
            r.raise_for_status()
        except Exception as e:
            result.attempts.append(f"springer_article_html:fail:{e}")
            return result

        html = r.text
        url_re = re.compile(
            r'https?://static-content\.springer\.com/[^"\'\s<>]+/MediaObjects/[^"\'\s<>]+'
        )
        seen: dict[str, None] = {}
        for u in url_re.findall(html):
            if u not in seen:
                seen[u] = None
        urls = list(seen.keys())

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

        supp_dir = out_dir / "supp"
        supp_dir.mkdir(parents=True, exist_ok=True)
        manifest_rows: list[dict[str, str]] = []
        for url in urls:
            name = unquote(url.rsplit("/", 1)[-1])
            dest = supp_dir / name
            if dest.exists() and dest.stat().st_size > 0:
                result.attempts.append(f"springer_supp:skip_existing:{name}")
                bytes_written = dest.stat().st_size
            else:
                try:
                    bytes_written = self._download(session, url, dest)
                except Exception as e:
                    result.attempts.append(f"springer_supp:fail:{url}:{e}")
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
