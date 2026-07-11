"""BioMed Central / BMC (DOI prefix 10.1186) — biomedcentral.com family.

BMC is Springer Nature's fully-open-access family. Individual journals live
on subdomains (bmcbioinformatics.biomedcentral.com,
bmccancer.biomedcentral.com, gutpathogens.biomedcentral.com, ...) but every
article has a DOI in the 10.1186 prefix and follows a completely uniform
URL scheme once you know which subdomain to hit.

CrossRef `resource.primary.URL` gives the exact subdomain per DOI, so we
follow it rather than trying to derive it heuristically from the DOI suffix
(bmc journal-suffix mappings are lengthy and evolve).

  Article HTML: {subdomain}/articles/{DOI}
  PDF:          {subdomain}/counter/pdf/{DOI}.pdf   (confirmed 2026-07-11)
  Supp files:   https://static-content.springer.com/esm/art%3A{doi-encoded}
                /MediaObjects/{opaque-slug}_MOESM{N}_ESM.{ext}

The supp-file CDN is the exact same static-content.springer.com/esm/ scheme
that Nature (10.1038) and Springer (10.1007) use — the plugin reuses the
Nature ESM URL regex verbatim.
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


class BMCPublisher(Publisher):
    doi_prefix = "10.1186"
    name = "bmc"

    BROWSER_UA = (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    )

    _ESM_URL_RE = re.compile(
        r'https?://static-content\.springer\.com/[^"\'\s<>]+/MediaObjects/[^"\'\s<>]+'
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
        raise RuntimeError(f"bmc.http_get gave up: {last_exc}")

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

    def _crossref_primary_url(self, session: requests.Session, doi: str) -> str | None:
        """Ask CrossRef for the publisher landing URL. Cached-cheap.

        BMC's per-journal subdomain is baked into resource.primary.URL and is
        the only reliable way to derive it — the mapping is
        journal-slug-suffix → subdomain and BMC keeps adding new journals.
        """
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

    def article_html_url(
        self, session: requests.Session, doi: str
    ) -> str | None:
        """CrossRef primary URL is the BMC article HTML URL. Delegate
        directly — same lookup as fetch_supp / probe_supp use.
        """
        return self._article_url(session, doi)

    def _article_url(self, session: requests.Session, doi: str) -> str | None:
        """Return {subdomain}/articles/{DOI} — via CrossRef primary URL.

        If CrossRef doesn't answer, fall back to a generic
        `www.biomedcentral.com/articles/{doi}` guess, which some rows resolve
        anyway thanks to Springer Nature's DOI-based routing.
        """
        prim = self._crossref_primary_url(session, doi)
        if prim:
            return prim
        return f"https://www.biomedcentral.com/articles/{doi}"

    def _pdf_url_from_article_url(self, article_url: str, doi: str) -> str:
        """Convert an article landing URL to the PDF URL.

        BMC PDF path is `{subdomain}/counter/pdf/{DOI}.pdf`.
        """
        # strip the /articles/{DOI} tail and append /counter/pdf/{DOI}.pdf
        m = re.match(r"^(https?://[^/]+)", article_url)
        base = m.group(1) if m else article_url
        return f"{base}/counter/pdf/{doi}.pdf"

    def probe_reachable(self, session: requests.Session, doi: str) -> bool:
        """Peek {subdomain}/counter/pdf/{DOI}.pdf for %PDF magic bytes."""
        art_url = self._article_url(session, doi)
        if not art_url:
            return False
        pdf_url = self._pdf_url_from_article_url(art_url, doi)
        return self._peek_pdf(session, pdf_url)

    def probe_supp(self, session: requests.Session, doi: str) -> tuple[bool, int]:
        """Fetch article HTML, count ESM URLs on static-content.springer.com."""
        art_url = self._article_url(session, doi)
        if not art_url:
            return (False, 0)
        r = self._http_get(session, art_url)
        if r.status_code != 200:
            return (False, 0)
        n = len(set(self._ESM_URL_RE.findall(r.text)))
        return (n > 0, n)

    def fetch_pdf(
        self, session: requests.Session, doi: str, out_dir: Path
    ) -> PublisherResult:
        result = PublisherResult()
        art_url = self._article_url(session, doi)
        if not art_url:
            result.attempts.append(f"bmc_pdf:no_article_url_for_doi={doi}")
            return result
        url = self._pdf_url_from_article_url(art_url, doi)
        result.attempts.append(f"bmc_pdf:{url}")
        try:
            n = self._download(session, url, out_dir / "paper.pdf")
        except Exception as e:
            result.attempts.append(f"bmc_pdf:fail:{e}")
            return result
        if n < 8192:
            (out_dir / "paper.pdf").unlink(missing_ok=True)
            result.attempts.append(f"bmc_pdf:fail:size={n}")
            return result
        try:
            with (out_dir / "paper.pdf").open("rb") as fh:
                head = fh.read(8)
        except OSError:
            head = b""
        if not head.startswith(b"%PDF"):
            (out_dir / "paper.pdf").unlink(missing_ok=True)
            result.attempts.append(f"bmc_pdf:fail:not_pdf:first8={head!r}")
            return result
        result.pdf_path = out_dir / "paper.pdf"
        result.pdf_url = url
        return result

    def fetch_supp(
        self, session: requests.Session, doi: str, out_dir: Path
    ) -> PublisherResult:
        """Parse article HTML for ESM/MediaObjects supp URLs and download all.

        BMC's supp files are named `{opaque-slug}_MOESM{N}_ESM.{ext}` and
        served from the same static-content.springer.com/esm/ CDN as
        Nature + Springer.
        """
        result = PublisherResult()
        art_url = self._article_url(session, doi)
        if not art_url:
            result.attempts.append(f"bmc_article_html:no_url_for_doi={doi}")
            return result
        result.attempts.append(f"bmc_article_html:{art_url}")
        try:
            r = self._http_get(session, art_url)
            r.raise_for_status()
        except Exception as e:
            result.attempts.append(f"bmc_article_html:fail:{e}")
            return result

        html = r.text
        # Restrict to ESM (supp) files only — MediaObjects also contains
        # figure PNGs served from media.springernature.com which are NOT supp.
        # Nature's regex hits static-content.springer.com specifically, which
        # BMC uses only for supp — matches our needs.
        seen: dict[str, None] = {}
        for u in self._ESM_URL_RE.findall(html):
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
                result.attempts.append(f"bmc_supp:skip_existing:{name}")
                bytes_written = dest.stat().st_size
            else:
                try:
                    bytes_written = self._download(session, url, dest)
                except Exception as e:
                    result.attempts.append(f"bmc_supp:fail:{url}:{e}")
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
