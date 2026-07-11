"""AAAS Science family (DOI prefix 10.1126) — Science, Sci Transl Med, Sci Immunol, etc.

Cluster IP is NOT network-blocked by Science's Cloudflare; only cold /
robotic requests get 403'd. Warming the session with two innocuous GETs
(root + article landing) before requesting the PDF endpoint delivers a
real PDF in 3/3 fresh-session trials per the 2026-07-10 deep-dive.

    warmup GET https://www.science.org/                     → 200 (cookies land)
    warmup GET https://www.science.org/doi/{DOI}            → 200 (landing HTML)
    fetch  GET https://www.science.org/doi/pdf/{DOI}        → 200 application/pdf

Supp files are hosted at
    https://www.science.org/doi/suppl/{DOI}/suppl_file/{filename}
but redirect through
    https://www.science.org/action/downloadSupplement?doi={DOI}&file={file}
which is subscriber-gated (403 even for hybrid-OA CC-BY articles). Local
Playwright rescue is the only route for supp. probe_supp reports the
count of supp files LISTED on the article page (informational only) and
fetch_supp is a no-op returning attempts-only so the pipeline records
"we tried".
"""
from __future__ import annotations

import re
import time
from pathlib import Path
from urllib.parse import unquote, urljoin

import requests
from bs4 import BeautifulSoup

from .base import Publisher, PublisherResult, SuppFile


class ScienceAAASPublisher(Publisher):
    doi_prefix = "10.1126"
    name = "science_aaas"

    BROWSER_UA = (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    )

    def _article_url(self, doi: str) -> str:
        return f"https://www.science.org/doi/{doi}"

    def article_html_url(
        self, session: requests.Session, doi: str
    ) -> str | None:
        """Return the article URL after warming the session with cookies.

        Cold requests to /doi/{DOI} tend to 403 under Cloudflare; two
        innocuous warmup GETs seed the cookies so the follow-up bare GET
        by the caller lands 200. See _warm_session docstring.
        """
        self._warm_session(session, doi)
        return self._article_url(doi)

    def _pdf_url(self, doi: str) -> str:
        return f"https://www.science.org/doi/pdf/{doi}"

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
        raise RuntimeError(f"science_aaas.http_get gave up: {last_exc}")

    def _warm_session(self, session: requests.Session, doi: str) -> None:
        """Two innocuous GETs to seed cookies before hitting the PDF endpoint.

        Cloudflare treats a cold /doi/pdf/ hit as bot-like and returns 403;
        after warming with `/` + `/doi/{doi}` the same PDF request returns
        real `application/pdf`.
        """
        try:
            r = self._http_get(session, "https://www.science.org/")
            r.close()
        except Exception:
            return  # non-fatal; the fetch will fail cleanly if the site is down
        try:
            r = self._http_get(session, self._article_url(doi))
            r.close()
        except Exception:
            return

    # ---------------------- probe hooks --------------------------------------

    def probe_reachable(self, session: requests.Session, doi: str) -> bool:
        """Warm the session then peek the PDF URL for %PDF magic bytes."""
        self._warm_session(session, doi)
        try:
            return self._peek_pdf(session, self._pdf_url(doi))
        except Exception:
            # Transient — raise so the caller's regression guard can react.
            raise

    def probe_supp(self, session: requests.Session, doi: str) -> tuple[bool, int]:
        """Count supp files LISTED on the article page. Because the actual
        download endpoint is subscriber-gated (see docstring), this is a
        LOWER BOUND on what exists but an UPPER BOUND on what fetch_supp
        can actually deliver from cluster IP. Local-agent Playwright rescue
        is the only path for the actual files.
        """
        self._warm_session(session, doi)
        try:
            r = self._http_get(session, self._article_url(doi))
        except Exception:
            raise
        if r.status_code != 200:
            return (False, 0)
        # Science article HTML links supp files via
        #   /doi/suppl/{DOI}/suppl_file/{filename}
        suppl_re = re.compile(
            re.escape(f"/doi/suppl/{doi}/suppl_file/")
            + r"[\w\.\-]+",
            re.IGNORECASE,
        )
        hits = set(suppl_re.findall(r.text))
        return (len(hits) > 0, len(hits))

    # ---------------------- fetch entry points -------------------------------

    def fetch_pdf(
        self, session: requests.Session, doi: str, out_dir: Path
    ) -> PublisherResult:
        result = PublisherResult()
        self._warm_session(session, doi)
        url = self._pdf_url(doi)
        result.attempts.append(f"science_aaas_pdf:{url}")
        dest = out_dir / "paper.pdf"
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            with self._http_get(session, url, stream=True) as r:
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
            result.attempts.append(f"science_aaas_pdf:fail:{e}")
            return result

        if dest.stat().st_size < 8192:
            dest.unlink(missing_ok=True)
            result.attempts.append("science_aaas_pdf:fail:too_small")
            return result
        try:
            with dest.open("rb") as fh:
                head = fh.read(8)
        except OSError:
            head = b""
        if not head.startswith(b"%PDF"):
            dest.unlink(missing_ok=True)
            result.attempts.append(
                f"science_aaas_pdf:fail:not_pdf:first8={head!r}"
            )
            return result
        result.pdf_path = dest
        result.pdf_url = url
        return result

    def fetch_supp(
        self, session: requests.Session, doi: str, out_dir: Path
    ) -> PublisherResult:
        """No-op with attempt logging — supp is subscriber-gated at
        Science's `/action/downloadSupplement` endpoint even for hybrid-OA
        CC-BY articles. Caller (fetch_paper.process_one) is responsible for
        queueing these PMIDs for local-agent Playwright rescue.
        """
        result = PublisherResult()
        # Enumerate supp URLs for the manifest (so the local agent knows
        # which files to fetch on its Playwright pass).
        self._warm_session(session, doi)
        try:
            r = self._http_get(session, self._article_url(doi))
        except Exception as e:
            result.attempts.append(f"science_aaas_supp_html:fail:{e}")
            return result

        if r.status_code != 200:
            result.attempts.append(
                f"science_aaas_supp_html:HTTP {r.status_code}"
            )
            return result

        suppl_re = re.compile(
            re.escape(f"/doi/suppl/{doi}/suppl_file/") + r"[\w\.\-]+",
            re.IGNORECASE,
        )
        supp_urls: list[str] = []
        for hit in set(suppl_re.findall(r.text)):
            supp_urls.append("https://www.science.org" + hit)

        result.attempts.append(
            f"science_aaas_supp:manifest_only:n={len(supp_urls)}:"
            f"reason=subscriber_gated_downloadSupplement_endpoint"
        )

        # Write a manifest describing what SHOULD be fetched by the local
        # agent's Playwright pass. This is the same shape as our other
        # publishers' supp/manifest.tsv but marked as unfulfilled.
        if supp_urls:
            supp_dir = out_dir / "supp"
            supp_dir.mkdir(parents=True, exist_ok=True)
            import csv as _csv
            with (supp_dir / "manifest_pending_playwright.tsv").open(
                "w", newline=""
            ) as fh:
                w = _csv.writer(fh, delimiter="\t")
                w.writerow(["url", "reason"])
                for url in supp_urls:
                    w.writerow([url, "subscriber_gated_from_cluster"])
        return result
