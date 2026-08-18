"""ALESC (Assembleia Legislativa de Santa Catarina) collectors.

ALESC publishes **no API**. Everything here is scraped or read from bulk CSV across
three unrelated hosts (see :mod:`resumo.ingestion.alesc.client`), so every module in
this package is written to degrade gracefully rather than raise when the upstream
markup drifts.
"""

from __future__ import annotations

from resumo.ingestion.alesc.client import AlescBlackoutError, AlescClient
from resumo.ingestion.alesc.parsing import AlescParseError

__all__ = ["AlescBlackoutError", "AlescClient", "AlescParseError"]
