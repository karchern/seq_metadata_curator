"""Base class for publisher-specific PDF + supp-material handlers."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import requests


@dataclass
class SuppFile:
    """A downloaded supplementary file plus what we know about it."""
    url: str
    filename: str
    label: Optional[str] = None       # e.g. "Supplementary Data 1"
    bytes_written: int = 0


@dataclass
class PublisherResult:
    """What a Publisher.fetch_* call reports back to the orchestrator."""
    pdf_path: Optional[Path] = None
    pdf_url: Optional[str] = None
    supp_files: list[SuppFile] = field(default_factory=list)
    attempts: list[str] = field(default_factory=list)


class Publisher(ABC):
    """Abstract publisher. Concrete subclasses implement matches() +
    fetch_pdf() + fetch_supp() against a specific publisher's HTML."""

    #: DOI prefix (e.g. "10.1038"); used by default matches().
    doi_prefix: str = ""
    #: Human-readable name for logging.
    name: str = "unknown"

    def matches(self, doi: str) -> bool:
        return bool(self.doi_prefix) and doi.startswith(self.doi_prefix + "/")

    def article_slug(self, doi: str) -> str:
        """Article ID = DOI suffix after prefix/."""
        return doi.split("/", 1)[1] if "/" in doi else doi

    @abstractmethod
    def fetch_pdf(
        self, session: requests.Session, doi: str, out_dir: Path
    ) -> PublisherResult:
        """Fetch the publisher's official PDF. Returns a result whose
        pdf_path is set on success and None on failure."""

    @abstractmethod
    def fetch_supp(
        self, session: requests.Session, doi: str, out_dir: Path
    ) -> PublisherResult:
        """Fetch every supplementary file the publisher exposes. Returns a
        result listing what was downloaded."""

    # -------------------- probe hooks (used by probe_coverage.py) -------------
    def probe_reachable(
        self, session: requests.Session, doi: str
    ) -> bool:
        """Cheap check: would fetch_pdf plausibly succeed for this DOI?

        Default returns False so unimplemented publishers don't get counted
        as accessible. Subclasses SHOULD override with a lightweight check
        (partial GET + %PDF magic-byte sniff is the canonical pattern —
        see _peek_pdf).
        """
        return False

    def probe_supp(
        self, session: requests.Session, doi: str
    ) -> tuple[bool, int]:
        """Cheap check: does this publisher expose supp files for this DOI?

        Return (True, N) if we can enumerate N > 0 supp URLs WITHOUT
        downloading them; (False, 0) otherwise. Publisher-specific: the
        default is (False, 0) so unimplemented publishers don't lie about
        supp availability.
        """
        return (False, 0)

    _BROWSER_UA = (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    )

    @classmethod
    def _peek_pdf(cls, session: requests.Session, url: str, timeout: int = 20) -> bool:
        """Return True iff URL responds 200 and its first bytes are `%PDF`.

        Streams only 8 bytes then closes the connection — cheap enough to
        run across the whole corpus without hammering publisher endpoints.

        Network exceptions (ConnectionError / Timeout) are RE-RAISED so
        the caller's regression guard can distinguish transient failure
        from a definitive negative. Non-200 HTTP responses return False
        because those ARE definitive (publisher said "not here").
        """
        r = session.get(
            url,
            stream=True,
            timeout=timeout,
            allow_redirects=True,
            headers={"User-Agent": cls._BROWSER_UA},
        )
        try:
            if r.status_code != 200:
                return False
            for chunk in r.iter_content(chunk_size=8):
                return chunk.startswith(b"%PDF")
            return False
        finally:
            try:
                r.close()
            except Exception:
                pass
