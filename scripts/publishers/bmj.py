"""BMJ Publishing Group (DOI prefix 10.1136) — HighWire-hosted journals.

BMJ articles live at journal-specific subdomains of bmj.com (e.g. gut.bmj.com,
jitc.bmj.com, bmjopen.bmj.com). The DOI resolver redirects to the article
landing page, which for open-access articles carries a `citation_pdf_url`
meta tag pointing directly at the article PDF.

Paywalled BMJ articles return the same URL pattern but serve an HTML paywall
page under the `.full.pdf` endpoint. We detect this by sniffing the response
content-type + magic bytes and fail cleanly so upstream fallbacks (Unpaywall,
DOI landing scrape) still get a shot.

No universal supp URL pattern — supp files on BMJ HighWire journals link out
to a per-article `supplementary-material.html` page with journal-specific
paths. Not attempted here: the fetch_supp method is a best-effort scan of the
landing page for `.pdf`/`.docx`/`.xlsx` links under a `/supplementary/` or
similar path.
"""
from __future__ import annotations

import csv
import re
import time
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from .base import Publisher, PublisherResult, SuppFile


class BMJPublisher(Publisher):
    doi_prefix = "10.1136"
    name = "bmj"

    BROWSER_UA = (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    )

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
        raise RuntimeError(f"bmj.http_get gave up: {last_exc}")

    def _resolve_landing(
        self, session: requests.Session, doi: str
    ) -> tuple[str, str]:
        """Follow doi.org → the article landing page. Returns (final_url, html)."""
        r = self._http_get(session, f"https://doi.org/{doi}")
        r.raise_for_status()
        return r.url, r.text

    def _find_pdf_url(self, landing_url: str, html: str) -> str | None:
        """Extract citation_pdf_url meta tag; fall back to `.full.pdf` guess."""
        m = re.search(
            r'<meta\s+name="citation_pdf_url"\s+content="([^"]+)"',
            html,
            flags=re.IGNORECASE,
        )
        if m:
            return m.group(1)
        # BMJ HighWire convention: content/<jrnl>/<vol>/<iss>/<pg>.full.pdf
        # Landing typically ends at /content/<vol>/<iss>/<pg>.
        pu = urlparse(landing_url)
        if pu.path.endswith("/"):
            return None
        return urljoin(landing_url, pu.path + ".full.pdf")

    def _looks_like_pdf(self, dest: Path) -> bool:
        try:
            with dest.open("rb") as fh:
                head = fh.read(8)
        except OSError:
            return False
        return head.startswith(b"%PDF")

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
        """Cheap check: resolve DOI → parse citation_pdf_url → peek that URL.

        BMJ's paywalled papers also return 200 on the pdf endpoint but with
        HTML body, so we sniff %PDF magic bytes. This costs 2 HTTP calls but
        is the only reliable BMJ probe (Cloudflare-style Content-Type lying).
        """
        try:
            landing_url, html = self._resolve_landing(session, doi)
        except Exception:
            return False
        pdf_url = self._find_pdf_url(landing_url, html)
        if not pdf_url:
            return False
        return self._peek_pdf(session, pdf_url)

    def fetch_pdf(
        self, session: requests.Session, doi: str, out_dir: Path
    ) -> PublisherResult:
        result = PublisherResult()
        try:
            landing_url, html = self._resolve_landing(session, doi)
        except Exception as e:
            result.attempts.append(f"bmj_landing:fail:{e}")
            return result
        result.attempts.append(f"bmj_landing:{landing_url}")

        pdf_url = self._find_pdf_url(landing_url, html)
        if not pdf_url:
            result.attempts.append("bmj_pdf:no_citation_pdf_url_meta")
            return result

        result.attempts.append(f"bmj_pdf:{pdf_url}")
        dest = out_dir / "paper.pdf"
        try:
            n = self._download(session, pdf_url, dest)
        except Exception as e:
            result.attempts.append(f"bmj_pdf:fail:{e}")
            return result

        if n < 8192 or not self._looks_like_pdf(dest):
            # BMJ returns 200 with HTML paywall body under .full.pdf when the
            # article isn't OA. Delete + report as failure so the fallback
            # chain (Unpaywall, DOI landing scrape) can proceed.
            dest.unlink(missing_ok=True)
            result.attempts.append(
                f"bmj_pdf:fail:not_pdf_or_too_small:n={n}"
            )
            return result

        result.pdf_path = dest
        result.pdf_url = pdf_url
        return result

    def fetch_supp(
        self, session: requests.Session, doi: str, out_dir: Path
    ) -> PublisherResult:
        """Best-effort scan of the article landing page for supp files.

        HighWire BMJ supplementary material typically links to /supplementary/
        paths and/or a dedicated supplementary-material.html page. This scan
        looks for direct .pdf/.docx/.xlsx/.zip references anywhere within
        those paths on the landing HTML. If none found, returns empty.
        """
        result = PublisherResult()
        try:
            landing_url, html = self._resolve_landing(session, doi)
        except Exception as e:
            result.attempts.append(f"bmj_supp_landing:fail:{e}")
            return result
        result.attempts.append(f"bmj_supp_landing:{landing_url}")

        soup = BeautifulSoup(html, "html.parser")
        candidates: dict[str, str] = {}  # url -> label
        supp_ext = re.compile(r"\.(pdf|docx?|xlsx?|zip|txt|csv|tsv)$", re.IGNORECASE)
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if href.startswith("//"):
                absu = "https:" + href
            elif href.startswith("/"):
                absu = urljoin(landing_url, href)
            elif href.startswith("http"):
                absu = href
            else:
                continue
            # Only follow BMJ-hosted supp material paths to avoid downloading
            # references etc.
            path = urlparse(absu).path.lower()
            if not any(
                marker in path
                for marker in (
                    "/supplementary/",
                    "/supplemental/",
                    "supplementary-material",
                    "/highwire/filestream/",
                )
            ):
                continue
            if not supp_ext.search(path):
                continue
            label = a.get_text(strip=True) or None
            candidates.setdefault(absu, label or "")

        if not candidates:
            result.attempts.append("bmj_supp:no_candidates")
            return result

        supp_dir = out_dir / "supp"
        supp_dir.mkdir(parents=True, exist_ok=True)
        manifest_rows: list[dict[str, str]] = []
        for url, label in candidates.items():
            name = unquote(url.rsplit("/", 1)[-1]) or "supp_file"
            dest = supp_dir / name
            if dest.exists() and dest.stat().st_size > 0:
                bytes_written = dest.stat().st_size
                result.attempts.append(f"bmj_supp:skip_existing:{name}")
            else:
                try:
                    bytes_written = self._download(session, url, dest)
                except Exception as e:
                    result.attempts.append(f"bmj_supp:fail:{url}:{e}")
                    continue
            sf = SuppFile(
                url=url, filename=name, label=label or None, bytes_written=bytes_written
            )
            result.supp_files.append(sf)
            manifest_rows.append(
                {"filename": name, "url": url, "label": label, "bytes": str(bytes_written)}
            )

        if manifest_rows:
            with (supp_dir / "manifest.tsv").open("w", newline="") as fh:
                w = csv.DictWriter(
                    fh, fieldnames=["filename", "url", "label", "bytes"], delimiter="\t"
                )
                w.writeheader()
                w.writerows(manifest_rows)
        return result
