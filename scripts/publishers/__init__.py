"""Publisher-specific supp-material + PDF handlers.

Selection is DOI-prefix-driven. To add support for a new publisher:

    1. Add a module (e.g. `elsevier.py`) subclassing `Publisher`.
    2. Register it in `_REGISTRY` below.
    3. Test on a known paper from that publisher.

The framework is intentionally thin — each publisher's HTML is idiosyncratic
enough that per-publisher code is unavoidable, but the interface is uniform
so `fetch_paper.py` doesn't need to know publisher details.
"""
from __future__ import annotations

from typing import Optional

from .base import Publisher
from .bmc import BMCPublisher
from .bmj import BMJPublisher
from .cell_press import CellPressPublisher
from .frontiers import FrontiersPublisher
from .mdpi import MDPIPublisher
from .nature import NaturePublisher
from .nature_legacy import LegacyNaturePublisher
from .science_aaas import ScienceAAASPublisher
from .springer import SpringerPublisher

# Ordered dispatch — first match wins. Add new publishers here.
# Note: LegacyNaturePublisher must precede NaturePublisher — it narrows on
# dotted-suffix legacy DOIs (e.g. 10.1038/onc.2017.314) and delegates URL
# construction to a slug-transformed override.
# Similarly CellPressPublisher must precede any future generic Elsevier
# handler — it narrows on `10.1016/j.{cell-suffix}.*` (celrep/chom/xgen/…)
# via a custom matches() override.
# BMCPublisher / FrontiersPublisher / MDPIPublisher own unique DOI
# prefixes (10.1186, 10.3389, 10.3390 respectively) so ordering vs the
# rest doesn't matter, but we keep entries lexicographic by name within
# the "unique-prefix" cluster for readability.
_REGISTRY: list[Publisher] = [
    LegacyNaturePublisher(),
    NaturePublisher(),
    SpringerPublisher(),
    BMCPublisher(),
    BMJPublisher(),
    FrontiersPublisher(),
    MDPIPublisher(),
    ScienceAAASPublisher(),
    CellPressPublisher(),
]


def get_publisher(doi: Optional[str]) -> Optional[Publisher]:
    if not doi:
        return None
    for pub in _REGISTRY:
        if pub.matches(doi):
            return pub
    return None


__all__ = ["Publisher", "get_publisher"]
