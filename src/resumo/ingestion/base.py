"""Collector base class.

Each collector: fetch artifact(s) -> hash -> ledger check -> parse -> idempotent
upsert -> record provenance. Subclasses implement `run`.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass

from sqlalchemy.orm import Session

logger = logging.getLogger("resumo.ingestion")


@dataclass
class CollectorResult:
    collector: str
    status: str  # "ingested" | "skipped" | "empty" | "error"
    row_count: int = 0
    detail: str | None = None

    def __str__(self) -> str:
        suffix = f" — {self.detail}" if self.detail else ""
        return f"[{self.collector}] {self.status}: {self.row_count} rows{suffix}"


class Collector(ABC):
    name: str = "collector"

    @abstractmethod
    def run(self, session: Session, **kwargs) -> CollectorResult:
        """Fetch, normalize and upsert. Must be safe to re-run (idempotent)."""
        raise NotImplementedError
