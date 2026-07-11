"""Cell Press family — a subset of Elsevier (10.1016) that lives on cell.com.

Cell Press hosts its own Cloudflare-fronted platform (`www.cell.com` and
for Cellular & Molecular Gastroenterology & Hepatology
`www.cmghjournal.org`) that gates our cluster IP with stochastic 200/403.
Repeated hits from the same session tend to escalate to 403; warm-session
mitigation helps but is unreliable.

VALUE OF THIS PLUGIN, per the 2026-07-10 Cell-family deep-dive:
  1. Routes Cell-family DOIs (which are Elsevier 10.1016 but NOT
     ScienceDirect-hosted) to a dedicated code path instead of getting
     lumped into the generic Elsevier ScienceDirect bucket.
  2. When a fulltext HTML fetch succeeds, enumerates supp URLs at
     `www.cell.com/cms/{DOI}/attachment/{uuid}/mmc{N}.{ext}` for the
     local-agent Playwright rescue queue.
  3. Provides an article-HTML source for `probe_reads_from_article_html`
     so DDBJ / ArrayExpress / BioProject accessions embedded in the paper
     can be mined even when the PDF is not fetchable from cluster.

DOI dispatch: 10.1016 subprefixes for Cell Press journals identified in
the CRC-microbiome corpus + broader Cell Press catalog.
"""
from __future__ import annotations

import csv as _csv
import re
import time
from pathlib import Path
from urllib.parse import unquote, urljoin

import requests
from bs4 import BeautifulSoup

from .base import Publisher, PublisherResult, SuppFile


# Elsevier DOI suffixes that identify Cell Press journals.
# Structure: 10.1016/j.{suffix}.YYYY.MM.NNN
CELL_PRESS_SUFFIXES = frozenset(
    (
        # Flagships
        "cell",      # Cell
        "ccell",     # Cancer Cell
        "chom",      # Cell Host & Microbe
        "cmet",      # Cell Metabolism
        "celrep",    # Cell Reports
        "xcrm",      # Cell Reports Medicine
        "xgen",      # Cell Genomics
        "stem",      # Cell Stem Cell
        "molcel",    # Molecular Cell
        "immuni",    # Immunity
        "cub",       # Current Biology
        # Adjacent / affiliated (Elsevier journals hosted on cell.com or its subdomains)
        "jcmgh",     # Cellular and Molecular Gastroenterology and Hepatology (cmghjournal.org)
        "devcel",    # Developmental Cell
        "neuron",    # Neuron
        "med",       # Med (Cell Press)
        "chembiol",  # Cell Chemical Biology
        "xinn",      # The Innovation (adjacent)
    )
)


class CellPressPublisher(Publisher):
    # We intentionally leave `doi_prefix` empty and override matches() —
    # Cell Press papers share Elsevier's 10.1016 prefix so a plain
    # prefix match would swallow the entire Elsevier ScienceDirect bucket.
    doi_prefix = ""
    name = "cell_press"

    BROWSER_UA = (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    )

    _SUFFIX_RE = re.compile(r"^10\.1016/j\.([a-z]+)\.\d{4}\.")

    def matches(self, doi: str) -> bool:
        m = self._SUFFIX_RE.match(doi)
        if m is None:
            return False
        return m.group(1) in CELL_PRESS_SUFFIXES

    def _article_url(self, doi: str) -> str:
        # DOI resolver redirects to cell.com/{slug}/fulltext/{PII} (or the
        # cmghjournal.org equivalent). Simpler than mapping every journal
        # slug manually and works consistently.
        return f"https://doi.org/{doi}"

    def article_html_url(
        self, session: requests.Session, doi: str
    ) -> str | None:
        """Return the DOI resolver URL after warming cookies.

        Cloudflare on cell.com is stochastic — even the warm session may
        403. Callers should treat a 4xx here as best-effort-failed rather
        than definitive missing content.
        """
        self._warm_session(session)
        return self._article_url(doi)

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
                    timeout=60,
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
        raise RuntimeError(f"cell_press.http_get gave up: {last_exc}")

    def _warm_session(self, session: requests.Session) -> None:
        """Two innocuous GETs on the cell.com root before article fetches."""
        for url in ("https://www.cell.com/",):
            try:
                r = self._http_get(session, url)
                r.close()
            except Exception:
                pass

    # ---------------------- probe hooks --------------------------------------

    def probe_reachable(self, session: requests.Session, doi: str) -> bool:
        """Fetch the DOI-resolved article page + look for the showPdf link.

        Because cell.com's Cloudflare is stochastic, this succeeds
        maybe 50% of the time from cluster. That's the reality; failures
        raise transient RuntimeError so the caller's regression guard
        preserves the prior state.
        """
        self._warm_session(session)
        r = self._http_get(session, self._article_url(doi))
        if r.status_code != 200:
            return False
        # If the fulltext HTML has a `showPdf` or `pdfExtended` link, the
        # PDF is retrievable (subject to Cloudflare's mood).
        return bool(
            re.search(
                r"(showPdf\?pii=|pdfExtended/)", r.text, flags=re.IGNORECASE
            )
        )

    _MMC_RE = re.compile(
        r'(?:https?://www\.(?:cell|cmghjournal)\.com)?/cms/'
        r'[^"\'\s<>]+/attachment/[^"\'\s<>]+/mmc\d+\.[A-Za-z0-9]+',
        re.IGNORECASE,
    )

    def probe_supp(self, session: requests.Session, doi: str) -> tuple[bool, int]:
        """Count `mmc*` supp URLs in the article HTML."""
        self._warm_session(session)
        r = self._http_get(session, self._article_url(doi))
        if r.status_code != 200:
            return (False, 0)
        hits = set(self._MMC_RE.findall(r.text))
        return (len(hits) > 0, len(hits))

    # ---------------------- fetch entry points -------------------------------

    def fetch_pdf(
        self, session: requests.Session, doi: str, out_dir: Path
    ) -> PublisherResult:
        """Best-effort; cell.com's Cloudflare is stochastic, so this is
        expected to succeed only sometimes from cluster. Local Playwright
        rescue handles the rest."""
        result = PublisherResult()
        self._warm_session(session)
        try:
            landing = self._http_get(session, self._article_url(doi))
        except Exception as e:
            result.attempts.append(f"cell_press_landing:fail:{e}")
            return result
        if landing.status_code != 200:
            result.attempts.append(
                f"cell_press_landing:HTTP {landing.status_code}"
            )
            return result

        # Try to find a showPdf link on the landing.
        m = re.search(
            r'href=["\']([^"\']*showPdf\?pii=[^"\'&]+[^"\']*)["\']',
            landing.text,
            flags=re.IGNORECASE,
        )
        if not m:
            result.attempts.append("cell_press_pdf:no_showPdf_link")
            return result

        pdf_url_rel = m.group(1)
        pdf_url = urljoin(landing.url, pdf_url_rel)
        result.attempts.append(f"cell_press_pdf:{pdf_url}")

        dest = out_dir / "paper.pdf"
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            with self._http_get(session, pdf_url, stream=True) as r:
                r.raise_for_status()
                tmp = dest.with_suffix(dest.suffix + ".part")
                n = 0
                with tmp.open("wb") as fh:
                    for chunk in r.iter_content(chunk_size=64 * 1024):
                        if chunk:
                            fh.write(chunk)
                            n += len(chunk)
                tmp.rename(dest)
        except Exception as e:
            result.attempts.append(f"cell_press_pdf:fail:{e}")
            return result

        if dest.stat().st_size < 8192:
            dest.unlink(missing_ok=True)
            result.attempts.append("cell_press_pdf:fail:too_small")
            return result
        try:
            with dest.open("rb") as fh:
                head = fh.read(8)
        except OSError:
            head = b""
        if not head.startswith(b"%PDF"):
            dest.unlink(missing_ok=True)
            result.attempts.append(f"cell_press_pdf:fail:not_pdf:first8={head!r}")
            return result

        result.pdf_path = dest
        result.pdf_url = pdf_url
        return result

    def fetch_supp(
        self, session: requests.Session, doi: str, out_dir: Path
    ) -> PublisherResult:
        """Enumerate mmc supp URLs; write a Playwright-pending manifest.

        Attempts to actually download too (in case Cloudflare cooperates),
        but the local agent's Playwright pass is the reliable path.
        """
        result = PublisherResult()
        self._warm_session(session)
        try:
            landing = self._http_get(session, self._article_url(doi))
        except Exception as e:
            result.attempts.append(f"cell_press_supp_landing:fail:{e}")
            return result
        if landing.status_code != 200:
            result.attempts.append(
                f"cell_press_supp_landing:HTTP {landing.status_code}"
            )
            return result

        # Enumerate mmc URLs (some come back schemeless).
        supp_urls: list[str] = []
        for hit in set(self._MMC_RE.findall(landing.text)):
            if hit.startswith("//"):
                supp_urls.append("https:" + hit)
            elif hit.startswith("/"):
                supp_urls.append(urljoin(landing.url, hit))
            elif hit.startswith("http"):
                supp_urls.append(hit)
            else:
                supp_urls.append(urljoin(landing.url, "/" + hit))
        # De-dup preserving order.
        supp_urls = list(dict.fromkeys(supp_urls))

        if not supp_urls:
            result.attempts.append("cell_press_supp:no_mmc_links")
            return result

        # Write the Playwright-pending manifest.
        supp_dir = out_dir / "supp"
        supp_dir.mkdir(parents=True, exist_ok=True)
        with (supp_dir / "manifest_pending_playwright.tsv").open(
            "w", newline=""
        ) as fh:
            w = _csv.writer(fh, delimiter="\t")
            w.writerow(["url", "reason"])
            for url in supp_urls:
                w.writerow([url, "cell_press_cloudflare_gated"])
        result.attempts.append(
            f"cell_press_supp:pending_playwright:n={len(supp_urls)}"
        )

        # Opportunistic direct download attempts (may partially succeed).
        for url in supp_urls:
            name = unquote(url.rsplit("/", 1)[-1])
            dest = supp_dir / name
            if dest.exists() and dest.stat().st_size > 0:
                continue
            try:
                with self._http_get(session, url, stream=True) as r:
                    if r.status_code != 200:
                        result.attempts.append(
                            f"cell_press_supp_dl:HTTP {r.status_code}:{url}"
                        )
                        continue
                    tmp = dest.with_suffix(dest.suffix + ".part")
                    n = 0
                    with tmp.open("wb") as fh:
                        for chunk in r.iter_content(chunk_size=64 * 1024):
                            if chunk:
                                fh.write(chunk)
                                n += len(chunk)
                    if n < 512:
                        tmp.unlink(missing_ok=True)
                        continue
                    tmp.rename(dest)
                    result.supp_files.append(
                        SuppFile(
                            url=url, filename=name, label=None,
                            bytes_written=n,
                        )
                    )
            except Exception as e:
                result.attempts.append(
                    f"cell_press_supp_dl:fail:{url}:{e}"
                )
        return result
