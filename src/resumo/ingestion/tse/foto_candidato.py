"""Collector: TSE foto_cand zips -> CandidatePhoto.

The photo the candidate filed with the Justiça Eleitoral at registration — the same
image DivulgaCandContas shows. Published per UF, one zip per state per election, and
(unlike every other bulk product) NOT under `odsele`: see `ckan.fotos_url`.

Like `proposta_governo`, the zip has no machine-readable manifest, so the file ->
candidate mapping is the digit run in the member path matched against a known
SQ_CANDIDATO. Here that fragility is cheap on both sides: an unmatched photo is
simply not published, and a mismatch is impossible to reach silently because the
only digit runs that resolve are candidacies we already hold.

Orphans are the NORM, not a warning sign: a UF's bundle carries every candidate in
the state, while this install is scoped to four offices.
"""

from __future__ import annotations

import logging
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from resumo.config import get_settings
from resumo.db.models import Candidacy, CandidatePhoto
from resumo.ingestion.base import Collector, CollectorResult
from resumo.ingestion.http import download_to_tempfile
from resumo.ingestion.ledger import (
    already_ingested,
    content_hash,
    record_ingestion,
    scoped_key,
    upsert,
)
from resumo.ingestion.tse import ckan, parsing

logger = logging.getLogger("resumo.ingestion.tse")

_MEDIA_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}


def _media_type(member: str) -> str:
    return _MEDIA_TYPES.get(Path(member).suffix.lower(), "image/jpeg")


class FotoCandidatoCollector(Collector):
    name = "tse_foto_candidato"

    def run(
        self,
        session: Session,
        *,
        source: Path | str | None = None,
        year: int | None = None,
        uf: str | None = None,
        **_,
    ) -> CollectorResult:
        """Ingest one UF's photo zip, or every configured UF when `uf` is omitted."""
        settings = get_settings()
        year = year or settings.election_year

        if source is None and not uf:
            targets = settings.uf_list
            if not targets:
                raise ValueError(
                    "foto_cand is published per UF: pass --uf, or set RESUMO_TARGET_UFS"
                )
            if len(targets) > 1:
                total, details = 0, []
                for one in targets:
                    r = self.run(session, year=year, uf=one)
                    total += r.row_count
                    details.append(f"{one}:{r.status}({r.row_count})")
                return CollectorResult(self.name, "ingested", total, " ".join(details))
            uf = targets[0]

        return self._run_one(session, source=source, year=year, uf=uf)

    def _run_one(
        self,
        session: Session,
        *,
        source: Path | str | None,
        year: int,
        uf: str | None,
    ) -> CollectorResult:
        settings = get_settings()
        tmp: Path | None = None
        if source is not None:
            data_path: Path | str = source
            digest = content_hash(Path(source).read_bytes())
            source_url = str(source)
        else:
            # CKAN first, template as fallback — this product has already moved once
            # between cycles. The needle carries the UF, so a catalog answer can only
            # ever be this state's bundle or no answer at all; and even a wrong zip
            # could not mis-attribute a face, since a photo is only kept when its
            # digit run IS a candidacy we hold.
            source_url = ckan.resolve_resource_url(
                f"candidatos-{year}",
                f"foto_cand{year}_{(uf or '').upper()}",
                fallback=ckan.fotos_url(year, uf or ""),
            )
            tmp, digest = download_to_tempfile(source_url)
            data_path = tmp

        # The bundle is read against whatever candidacies are in base, and that set is
        # governed by the configured scope. Widening the scope must re-run this even
        # though the zip's bytes never changed — otherwise the newly in-scope
        # candidates stay faceless until TSE happens to republish the file.
        ledger_url = scoped_key(
            source_url,
            uf=(uf or "").upper(),
            cargo=",".join(map(str, sorted(settings.cargo_set))),
        )

        try:
            if already_ingested(session, ledger_url, digest):
                return CollectorResult(self.name, "skipped", 0, "unchanged (hash match)")

            known = {sq for (sq,) in session.execute(select(Candidacy.sq_candidato))}
            storage = settings.storage_path() / "foto" / str(year)
            storage.mkdir(parents=True, exist_ok=True)

            rows: list[dict] = []
            orphans = 0
            empty = 0
            for member in parsing.list_image_members(data_path):
                sq = parsing.match_sq(member, known)
                if not sq:
                    orphans += 1
                    continue
                image = parsing.read_member(data_path, member)
                if not image:
                    # A zero-byte member would publish a broken image tag under
                    # someone's name; count it and move on.
                    empty += 1
                    continue
                ihash = content_hash(image)
                out = storage / f"{sq}_{ihash[:8]}{Path(member).suffix.lower()}"
                out.write_bytes(image)
                rows.append(
                    {
                        "sq_candidato": sq,
                        "source": "tse_bulk_foto",
                        "storage_path": str(out),
                        "original_filename": Path(member).name,
                        "media_type": _media_type(member),
                        "content_hash": ihash,
                    }
                )

            n = upsert(session, CandidatePhoto, rows, index_elements=["sq_candidato"])
            record_ingestion(
                session,
                collector_name=self.name,
                source_url=ledger_url,
                digest=digest,
                row_count=n,
            )
            detail = f"uf={uf or '—'}"
            if orphans:
                detail += f" · {orphans} fotos sem candidatura no escopo"
            if empty:
                detail += f" · {empty} arquivo(s) vazio(s) ignorado(s)"
            return CollectorResult(self.name, "ingested", n, detail)
        finally:
            if tmp is not None:
                tmp.unlink(missing_ok=True)
