"""Frontiers Media (DOI prefix 10.3389) — frontiersin.org.

Frontiers is fully open access. PDF download is trivial. Supplementary
material download is HARD because Frontiers' 2024 SPA rewrite hides the
actual file URLs behind client-side JS state; direct HEAD/GET on every
plausible /files/Articles/{id}/... path we tried returned 404 (tested
2026-07-11). The Nuxt-serialised token blob in the article HTML contains
the supp URLs but they're compressed via index-references into a lookup
table that changes shape per page.

What we CAN do reliably:

  * Article HTML: https://www.frontiersin.org/articles/{DOI}/full
    (transparently redirects to the journal-scoped canonical URL)
  * PDF:          https://www.frontiersin.org/articles/{DOI}/pdf
    (returns application/pdf directly — confirmed 2026-07-11)
  * JATS XML:     https://public-pages-files-2025.frontiersin.org
                  /journals/{JOURNAL_SLUG}/articles/{DOI}/xml
    (returns full JATS XML including <supplementary-material> blocks with
     xlink:href attributes listing the supp filenames verbatim, e.g.
     "Table_1.DOCX", "Image_1.pdf", "DataSheet_1.docx")

So `probe_supp` uses the JATS XML to enumerate supp file counts — that's
authoritative for "does this article HAVE supp material?" and stable
across the SPA rewrite. `fetch_supp` best-effort-tries a handful of
historic file-path patterns; if all fail it records the discovered
filenames in the attempts log so downstream can see that supp EXISTS but
the direct-download path is unknown (PMC-OA fallback then covers most
Frontiers articles anyway).

Journal-slug discovery: the /articles/{DOI}/full endpoint issues a 302 to
the canonical `/journals/{JOURNAL_SLUG}/articles/{DOI}/full` URL. We
capture the slug from the redirect chain.
"""
from __future__ import annotations

import csv
import re
import time
from pathlib import Path
from urllib.parse import unquote

import requests

from .base import Publisher, PublisherResult, SuppFile


class FrontiersPublisher(Publisher):
    doi_prefix = "10.3389"
    name = "frontiers"

    BROWSER_UA = (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    )

    # Supp files are declared inside <supplementary-material .../> or
    # <supplementary-material ...>...</supplementary-material> blocks in
    # the JATS XML. We match on the tag itself (self-closed or paired)
    # and then extract each block's xlink:href attribute.
    #
    # Filename conventions observed in the wild (2026-07-11):
    #   * Underscored:   "Table_1.DOCX", "Image_1.pdf",
    #                    "DataSheet_1.docx", "Presentation_1.pdf",
    #                    "Video_1.mp4"
    #   * Non-underscored (newer): "DataSheet1.ZIP", "Table1.docx"
    #
    # Restricting to <supplementary-material> parents avoids false
    # positives from figure images (e.g. fmolb-10-1327893-g001.tif) that
    # otherwise match a naive xlink:href-anywhere regex.
    _JATS_SUPP_TAG_RE = re.compile(
        r'<supplementary-material\b[^>]*/>'
        r'|<supplementary-material\b[^>]*?>.*?</supplementary-material>',
        re.DOTALL,
    )
    _JATS_HREF_RE = re.compile(r'xlink:href="([^"]+)"')

    def _jats_supp_files(self, xml: str) -> set[str]:
        """Return the set of xlink:href filenames declared inside any
        <supplementary-material> block in the JATS XML.

        Filenames are taken verbatim from the xlink:href attribute of the
        first matching URL-like token inside each block. Self-closed
        <supplementary-material .../> tags (Frontiers' current convention
        for single-file supp) are also handled.
        """
        out: set[str] = set()
        for m in self._JATS_SUPP_TAG_RE.finditer(xml):
            block = m.group(0)
            # Take the FIRST href in the block (Frontiers puts the file
            # ref in the outermost <supplementary-material xlink:href="...">
            # attr for single-file supp and in an inner <media> child for
            # multi-part supp — both cases handled by "first href").
            href = self._JATS_HREF_RE.search(block)
            if not href:
                continue
            val = href.group(1)
            # Skip URLs (references, licenses) — supp filenames are bare.
            if val.startswith(("http://", "https://", "//")):
                continue
            # Skip pure integer / anchor refs.
            if "." not in val:
                continue
            out.add(val)
        return out

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
        raise RuntimeError(f"frontiers.http_get gave up: {last_exc}")

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

    def _article_url(self, doi: str) -> str:
        return f"https://www.frontiersin.org/articles/{doi}/full"

    def article_html_url(
        self, session: requests.Session, doi: str
    ) -> str | None:
        """Frontiers /articles/{DOI}/full redirects to the canonical
        journal-scoped URL. Return the pre-redirect URL — the caller does
        allow_redirects=True so it lands on the article regardless.
        """
        return self._article_url(doi)

    def _pdf_url(self, doi: str) -> str:
        return f"https://www.frontiersin.org/articles/{doi}/pdf"

    def _journal_slug(self, session: requests.Session, doi: str) -> str | None:
        """Discover the journal URL slug via the /articles/{DOI}/full redirect.

        The redirect target is /journals/{JOURNAL_SLUG}/articles/{DOI}/full,
        which we parse for the slug. Needed for the JATS XML endpoint.
        """
        try:
            r = self._http_get(session, self._article_url(doi))
        except Exception:
            return None
        m = re.search(r"/journals/([^/]+)/articles/", r.url)
        return m.group(1) if m else None

    def _jats_xml_url(self, journal: str, doi: str) -> str:
        return (
            "https://public-pages-files-2025.frontiersin.org/journals/"
            f"{journal}/articles/{doi}/xml"
        )

    def probe_reachable(self, session: requests.Session, doi: str) -> bool:
        """Peek /articles/{DOI}/pdf for %PDF magic bytes.

        Uses a local streamed-GET instead of `_peek_pdf` because Frontiers'
        Cloudfront edge sometimes fragments the initial TCP packet into a
        3-byte first chunk (b"%PD"), which `_peek_pdf`'s single-chunk-check
        rejects. We accumulate up to 4 bytes across chunks before deciding.
        """
        url = self._pdf_url(doi)
        try:
            r = self._http_get(session, url, stream=True)
        except Exception:
            return False
        try:
            if r.status_code != 200:
                return False
            head = b""
            for chunk in r.iter_content(chunk_size=8):
                head += chunk
                if len(head) >= 4:
                    break
            return head.startswith(b"%PDF")
        finally:
            try:
                r.close()
            except Exception:
                pass

    def probe_supp(self, session: requests.Session, doi: str) -> tuple[bool, int]:
        """Fetch JATS XML, count unique xlink:href supp filenames.

        Uses the public-pages-files-2025 CDN which serves the article's
        original JATS-format XML. Supplementary-material elements list
        their child files' filenames as xlink:href attributes; we count
        the unique filenames matching a supp-file naming pattern
        (Table_N / Image_N / DataSheet_N / Presentation_N / etc.).

        This is stable across the 2024 SPA rewrite — the JATS endpoint
        has not been touched — and is authoritative for "does this
        article have supp material declared?" even when the SPA-rendered
        HTML doesn't expose the URLs.
        """
        journal = self._journal_slug(session, doi)
        if not journal:
            return (False, 0)
        try:
            r = self._http_get(session, self._jats_xml_url(journal, doi))
        except Exception:
            return (False, 0)
        if r.status_code != 200:
            return (False, 0)
        n = len(self._jats_supp_files(r.text))
        return (n > 0, n)

    def fetch_pdf(
        self, session: requests.Session, doi: str, out_dir: Path
    ) -> PublisherResult:
        result = PublisherResult()
        url = self._pdf_url(doi)
        result.attempts.append(f"frontiers_pdf:{url}")
        try:
            n = self._download(session, url, out_dir / "paper.pdf")
        except Exception as e:
            result.attempts.append(f"frontiers_pdf:fail:{e}")
            return result
        if n < 8192:
            (out_dir / "paper.pdf").unlink(missing_ok=True)
            result.attempts.append(f"frontiers_pdf:fail:size={n}")
            return result
        try:
            with (out_dir / "paper.pdf").open("rb") as fh:
                head = fh.read(8)
        except OSError:
            head = b""
        if not head.startswith(b"%PDF"):
            (out_dir / "paper.pdf").unlink(missing_ok=True)
            result.attempts.append(f"frontiers_pdf:fail:not_pdf:first8={head!r}")
            return result
        result.pdf_path = out_dir / "paper.pdf"
        result.pdf_url = url
        return result

    def _candidate_supp_urls(
        self, journal: str, doi: str, filename: str
    ) -> list[str]:
        """Historic / plausible Frontiers direct-file URL patterns.

        Frontiers' post-2024 SPA hides the true supp URLs. Every URL scheme
        we tried in 2026-07-11 investigation returned 404 for the current
        corpus samples (334070/fcimb-08-00281). We keep the patterns here
        so future re-runs can be re-tried cheaply — if Frontiers restores
        a stable file endpoint, these will start hitting.

        The article_id (numeric) is embedded in HTML file paths but we do
        NOT have it here; the caller (fetch_supp) discovers it if needed.
        """
        # Placeholder — the file naming is opaque; without article_id +
        # journal-stub context (fcimb-08-00281 style) we can't compose a
        # useful direct URL. Return empty; fetch_supp will still log the
        # filenames it discovered so callers can see "supp exists but
        # direct-fetch unsupported".
        return []

    def fetch_supp(
        self, session: requests.Session, doi: str, out_dir: Path
    ) -> PublisherResult:
        """Best-effort supp fetch.

        As of 2026-07-11 Frontiers' post-SPA-rewrite direct file URLs are
        not publicly known / not derivable from the JATS XML alone (the
        real download URLs live in a Nuxt-serialised token blob that
        indirects through an integer table). We enumerate the supp
        filenames from JATS so probe_supp gives an accurate count, but
        the actual download step almost always fails for post-rewrite
        articles.

        For rows the pmc_oa fallback covers this is fine — the corpus
        already has 69/79 Frontiers papers supp-satisfied via PMC-OA.
        For the residual, we log the discovered filenames in the attempts
        list so downstream can distinguish "no supp exists" from
        "supp exists but publisher direct-fetch failed".
        """
        result = PublisherResult()
        journal = self._journal_slug(session, doi)
        if not journal:
            result.attempts.append(
                f"frontiers_supp:no_journal_slug_for_doi={doi}"
            )
            return result

        # JATS XML → supp filename enumeration
        jats_url = self._jats_xml_url(journal, doi)
        result.attempts.append(f"frontiers_jats:{jats_url}")
        try:
            r = self._http_get(session, jats_url)
            r.raise_for_status()
        except Exception as e:
            result.attempts.append(f"frontiers_jats:fail:{e}")
            return result
        filenames = sorted(self._jats_supp_files(r.text))
        if not filenames:
            result.attempts.append("frontiers_supp:no_files_in_jats")
            return result

        result.attempts.append(
            f"frontiers_supp:jats_declared_files={sorted(filenames)}"
        )

        # For each declared filename, try our (currently empty) list of
        # historic direct URL patterns. If any works, download; otherwise
        # log as unreachable.
        supp_dir = out_dir / "supp"
        supp_dir.mkdir(parents=True, exist_ok=True)
        manifest_rows: list[dict[str, str]] = []
        for name in sorted(filenames):
            dest = supp_dir / name
            if dest.exists() and dest.stat().st_size > 0:
                # Prior fetch (via another source) landed this file —
                # register it in this plugin's result but don't re-download.
                result.attempts.append(
                    f"frontiers_supp:skip_existing:{name}"
                )
                sf = SuppFile(
                    url="",
                    filename=name,
                    label=None,
                    bytes_written=dest.stat().st_size,
                )
                result.supp_files.append(sf)
                manifest_rows.append(
                    {
                        "filename": name,
                        "url": "",
                        "label": "",
                        "bytes": str(dest.stat().st_size),
                    }
                )
                continue
            candidates = self._candidate_supp_urls(journal, doi, name)
            landed = False
            for url in candidates:
                try:
                    n = self._download(session, url, dest)
                except Exception as e:
                    result.attempts.append(
                        f"frontiers_supp:try_fail:{url}:{e}"
                    )
                    continue
                if n < 32:
                    dest.unlink(missing_ok=True)
                    result.attempts.append(
                        f"frontiers_supp:try_fail:{url}:size={n}"
                    )
                    continue
                # Sniff magic bytes if extension is guessable
                landed = True
                sf = SuppFile(
                    url=url,
                    filename=name,
                    label=None,
                    bytes_written=n,
                )
                result.supp_files.append(sf)
                manifest_rows.append(
                    {
                        "filename": name,
                        "url": url,
                        "label": "",
                        "bytes": str(n),
                    }
                )
                break
            if not landed:
                result.attempts.append(
                    f"frontiers_supp:no_direct_url_for:{name}"
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
