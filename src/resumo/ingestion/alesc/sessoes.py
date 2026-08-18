"""Crawling the e-Legis plenary-session index (shared by votações and presença).

Source of truth: ``{alesc_elegis_base}/sessoes-plenarias?page=N`` — 610 sessions over
61 pages, newest first, earliest **02/02/2023**.

🚨 **e-Legis has no data before Feb 2023.** There is no earlier ALESC roll-call or
attendance history anywhere — not on this host and not on the institutional site.

🚨 Session hashes are **opaque hashids** (``57x9N``, ``zJo1K``): not sequential, not
guessable, so the index must actually be crawled to enumerate them. A full crawl is
~1,220 requests, which is why every caller takes `limit` and a date window and why
partial runs are the normal mode of operation.
"""

from __future__ import annotations

import datetime as dt
import itertools
import logging
from collections.abc import Iterator

from resumo.ingestion.alesc.client import AlescClient
from resumo.ingestion.alesc.parsing import (
    SessionRef,
    next_page_url,
    parse_result_total,
    parse_session_index,
)
from resumo.util import parse_date

logger = logging.getLogger("resumo.ingestion.alesc")

INDEX_PATH = "/sessoes-plenarias"


def iter_sessions(
    client: AlescClient,
    *,
    data_inicio: str | dt.date | None = None,
    data_fim: str | dt.date | None = None,
    limit: int | None = None,
    max_pages: int | None = 100,
    section: str | None = None,
) -> Iterator[SessionRef]:
    """Yield sessions newest-first, filtered by date window and available `section`.

    Because the index is sorted descending, hitting a session older than `data_inicio`
    ends the crawl instead of paging through the remaining 60 pages.
    """
    start = data_inicio if isinstance(data_inicio, dt.date) else parse_date(data_inicio)
    end = data_fim if isinstance(data_fim, dt.date) else parse_date(data_fim)

    yielded = 0
    path: str | None = INDEX_PATH
    # `None` means "no page cap", matching `limit=None` everywhere else in the
    # collectors; the crawl still ends when the index runs out of `rel=next` links
    # or a session predates `data_inicio`.
    page_cap = itertools.count() if max_pages is None else range(max_pages)
    for page in page_cap:
        if path is None:
            break
        markup = client.get_elegis(path)
        if page == 0:
            total = parse_result_total(markup)
            if total is not None:
                logger.info("ALESC: %s sessions in the e-Legis index", total)
        sessions = parse_session_index(markup)
        if not sessions:
            return
        for ref in sessions:
            if start and ref.data and ref.data < start:
                return
            if end and ref.data and ref.data > end:
                continue
            if section and not ref.has(section):
                continue
            yield ref
            yielded += 1
            if limit and yielded >= limit:
                return
        path = next_page_url(markup)
