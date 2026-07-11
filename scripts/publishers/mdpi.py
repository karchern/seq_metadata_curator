"""MDPI (DOI prefix 10.3390) — mdpi.com.

MDPI is fully open access. From a public browser their URL scheme is:

  Article HTML: https://www.mdpi.com/{PII}/htm   or ...  (redirect target)
  PDF:          https://www.mdpi.com/{PII}/pdf
  Supp files:   https://www.mdpi.com/article/{PII}/s{N}  (per-file redirect)
                — usually resolves to /article_deploy/... path.
  Supp bundle:  https://www.mdpi.com/{PII}/s1?filename=...

`{PII}` is derivable from the DOI via CrossRef `resource.primary.URL`;
CrossRef returns e.g. `https://www.mdpi.com/2072-6694/13/6/1341` for DOI
`10.3390/cancers13061341` (PII = `2072-6694/13/6/1341`).

CLUSTER-IP GOTCHA (confirmed 2026-07-11):

  MDPI's frontend is behind Akamai / edgesuite.net which blocks the EMBL
  compute-node public IP range with HTTP 403 "Access Denied" — regardless
  of User-Agent, Referer, or Chromium-realistic client-hint headers. This
  matches the Cloudflare-gated-publisher pattern that `local_agent_scripts/`
  was created for on the laptop side. Local-agent laptop workflow is the
  intended fallback for MDPI supp fetch.

The plugin still exists so:

  1. Publisher dispatch correctly routes 10.3390 DOIs to MDPI (rather than
     leaving them unhandled, which would suppress diagnostics).
  2. probe_reachable / probe_supp cleanly report False + reason=blocked,
     preventing the coverage refresh from crediting MDPI PDF/supp that
     we can't actually retrieve.
  3. If a future path around the block appears (proxy, alternate CDN,
     login-agent) we plug it in here rather than rebuilding dispatch.
"""
from __future__ import annotations

import csv
import re
import time
from pathlib import Path
from urllib.parse import unquote

import requests
from bs4 import BeautifulSoup

from .base import Publisher, PublisherResult, SuppFile


class MDPIPublisher(Publisher):
    doi_prefix = "10.3390"
    name = "mdpi"

    BROWSER_UA = (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    )

    # MDPI supp file URLs, when reachable, take this form.
    # e.g. /article_deploy/2072-6694/13/6/1341/1/cancers-13-01341-s001.pdf
    _SUPP_URL_RE = re.compile(
        r'href="(/article_deploy/[^"]+|https?://www\.mdpi\.com/article/[^"]+/s[0-9]+[^"]*)"'
    )

    def _http_get(
        self, session: requests.Session, url: str, *, stream: bool = False
    ) -> requests.Response:
        delay = 1.0
        last_exc: Exception | None = None
        for _ in range(3):
            try:
                r = session.get(
                    url,
                    stream=stream,
                    timeout=90,
                    allow_redirects=True,
                    headers={
                        "User-Agent": self.BROWSER_UA,
                        # A Referer sometimes helps against soft blocks (in
                        # practice MDPI's Akamai still 403s from the cluster,
                        # but no harm on other IPs).
                        "Referer": "https://scholar.google.com/",
                    },
                )
                if r.status_code in (429, 500, 502, 503, 504):
                    last_exc = RuntimeError(f"HTTP {r.status_code} at {url}")
                    try:
                        r.close()
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
        raise RuntimeError(f"mdpi.http_get gave up: {last_exc}")

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

    def _crossref_primary_url(
        self, session: requests.Session, doi: str
    ) -> str | None:
        try:
            r = self._http_get(
                session, f"https://api.crossref.org/works/{doi}"
            )
            if r.status_code != 200:
                return None
            j = r.json()
            return (
                j.get("message", {})
                 .get("resource", {})
                 .get("primary", {})
                 .get("URL")
            )
        except Exception:
            return None

    def _pii(self, session: requests.Session, doi: str) -> str | None:
        """Extract the PII suffix from CrossRef primary URL.

        e.g. for URL 'https://www.mdpi.com/2072-6694/13/6/1341'
             PII = '2072-6694/13/6/1341'
        """
        prim = self._crossref_primary_url(session, doi)
        if not prim:
            return None
        m = re.match(r"^https?://www\.mdpi\.com/([0-9]+-[0-9Xx]+/[0-9]+/[0-9]+/[0-9A-Za-z]+)", prim)
        return m.group(1) if m else None

    def _article_url(self, pii: str) -> str:
        return f"https://www.mdpi.com/{pii}"

    def _pdf_url(self, pii: str) -> str:
        return f"https://www.mdpi.com/{pii}/pdf"

    def probe_reachable(self, session: requests.Session, doi: str) -> bool:
        """Peek /{pii}/pdf for %PDF. Cluster-IP-blocked → returns False."""
        pii = self._pii(session, doi)
        if not pii:
            return False
        try:
            return self._peek_pdf(session, self._pdf_url(pii))
        except Exception:
            return False

    def probe_supp(self, session: requests.Session, doi: str) -> tuple[bool, int]:
        """Fetch article HTML, count supp URLs.

        On cluster IPs this consistently returns (False, 0) because Akamai
        403s the article HTML before we ever see the supp URLs. When the
        block is lifted (or on a non-blocked IP) it counts real supp URLs
        matching the /article_deploy/... or /article/{PII}/s{N} patterns.
        """
        pii = self._pii(session, doi)
        if not pii:
            return (False, 0)
        try:
            r = self._http_get(session, self._article_url(pii))
        except Exception:
            return (False, 0)
        if r.status_code != 200:
            return (False, 0)
        n = len(set(self._SUPP_URL_RE.findall(r.text)))
        return (n > 0, n)

    def fetch_pdf(
        self, session: requests.Session, doi: str, out_dir: Path
    ) -> PublisherResult:
        result = PublisherResult()
        pii = self._pii(session, doi)
        if not pii:
            result.attempts.append(f"mdpi_pdf:no_pii_for_doi={doi}")
            return result
        url = self._pdf_url(pii)
        result.attempts.append(f"mdpi_pdf:{url}")
        try:
            n = self._download(session, url, out_dir / "paper.pdf")
        except Exception as e:
            result.attempts.append(f"mdpi_pdf:fail:{e}")
            return result
        if n < 8192:
            (out_dir / "paper.pdf").unlink(missing_ok=True)
            result.attempts.append(f"mdpi_pdf:fail:size={n}")
            return result
        try:
            with (out_dir / "paper.pdf").open("rb") as fh:
                head = fh.read(8)
        except OSError:
            head = b""
        if not head.startswith(b"%PDF"):
            (out_dir / "paper.pdf").unlink(missing_ok=True)
            result.attempts.append(
                f"mdpi_pdf:fail:not_pdf:first8={head!r}"
            )
            return result
        result.pdf_path = out_dir / "paper.pdf"
        result.pdf_url = url
        return result

    def fetch_supp(
        self, session: requests.Session, doi: str, out_dir: Path
    ) -> PublisherResult:
        """Parse article HTML for supp URLs and download each.

        MDPI serves individual supp files at /article_deploy/{PII}/...
        or via /article/{PII}/s{N} shortlink. When Akamai 403s the article
        HTML we simply return an empty result with a diagnostic attempt
        entry so the caller can distinguish blocked-by-CDN from "no supp".
        """
        result = PublisherResult()
        pii = self._pii(session, doi)
        if not pii:
            result.attempts.append(f"mdpi_supp:no_pii_for_doi={doi}")
            return result
        art_url = self._article_url(pii)
        result.attempts.append(f"mdpi_article_html:{art_url}")
        try:
            r = self._http_get(session, art_url)
        except Exception as e:
            result.attempts.append(f"mdpi_article_html:fail:{e}")
            return result
        if r.status_code != 200:
            result.attempts.append(
                f"mdpi_article_html:status={r.status_code}"
            )
            return result

        html = r.text
        seen: dict[str, None] = {}
        for u in self._SUPP_URL_RE.findall(html):
            # Normalize to absolute URL
            if u.startswith("/"):
                u = "https://www.mdpi.com" + u
            if u not in seen:
                seen[u] = None
        urls = list(seen.keys())
        if not urls:
            result.attempts.append("mdpi_supp:no_urls_in_html")
            return result

        soup = BeautifulSoup(html, "html.parser")
        label_by_url: dict[str, str] = {}
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if href.startswith("/"):
                href = "https://www.mdpi.com" + href
            if href in seen:
                label = a.get_text(strip=True)
                if label and href not in label_by_url:
                    label_by_url[href] = label

        supp_dir = out_dir / "supp"
        supp_dir.mkdir(parents=True, exist_ok=True)
        manifest_rows: list[dict[str, str]] = []
        for url in urls:
            name = unquote(url.rsplit("/", 1)[-1].split("?", 1)[0])
            if not name or name == pii.rsplit("/", 1)[-1]:
                # If URL has no useful filename (e.g. .../s1 shortlink),
                # skip — we'd need to follow redirects and inspect
                # Content-Disposition to get the real name.
                # MDPI typically resolves /article/{PII}/s{N} to a
                # /article_deploy/... URL with the true filename in path;
                # we handle that by following redirects during _download,
                # but here we need a sensible dest name up front.
                name = f"supp_{len(manifest_rows) + 1}.bin"
            dest = supp_dir / name
            if dest.exists() and dest.stat().st_size > 0:
                result.attempts.append(f"mdpi_supp:skip_existing:{name}")
                bytes_written = dest.stat().st_size
            else:
                try:
                    bytes_written = self._download(session, url, dest)
                except Exception as e:
                    result.attempts.append(f"mdpi_supp:fail:{url}:{e}")
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
