"""Legacy Nature DOI slug fixer (DOI prefix 10.1038, dotted suffixes).

Background
----------
The main NaturePublisher (nature.py) builds the article URL by taking the DOI
suffix verbatim as the URL slug:
    doi = "10.1038/s41467-020-19995-0" → slug = "s41467-020-19995-0"
    →  https://www.nature.com/articles/s41467-020-19995-0

For articles published under the pre-~2018 Nature branding scheme, the DOI
suffix contains DOTS (e.g. `onc.2017.314`) but the URL slug does NOT — Nature
strips them (`onc2017314`). The main module 404s on these.

Example from the CRC-microbiome corpus:
    PMID 28869607 → DOI 10.1038/onc.2017.314
    → https://www.nature.com/articles/onc.2017.314        HTTP 404
    → https://www.nature.com/articles/onc2017314          HTTP 200 (article)
    → https://www.nature.com/articles/onc2017314.pdf      HTTP 200 (%PDF-1.6)

This module handles ONLY the dotted-suffix legacy shape. Register it
BEFORE the main NaturePublisher in the registry so its match wins on the
narrow case; otherwise it delegates to the standard flow.
"""
from __future__ import annotations

from .nature import NaturePublisher


class LegacyNaturePublisher(NaturePublisher):
    name = "nature_legacy"

    # We match 10.1038 DOIs whose suffix contains a dot — those are the
    # dotted-slug legacy articles. Modern Nature suffixes like
    # `s41586-020-2179-y` contain no dots.
    def matches(self, doi: str) -> bool:
        if not doi.startswith("10.1038/"):
            return False
        suffix = doi.split("/", 1)[1]
        return "." in suffix

    def article_slug(self, doi: str) -> str:
        # Strip all dots from the DOI suffix to recover Nature's URL slug.
        return super().article_slug(doi).replace(".", "")
