"""Collector: sitting governors -> Mandate (+ seeds Person identity). No network.

Source of truth: **the TSE result the platform already ingested** — the
``consulta_cand`` row for the previous general election with ``DS_SIT_TOT_TURNO =
ELEITO`` at ``CD_CARGO = 3``. Winning that election is what confers the office
(CF art. 28; the diplomação that follows merely certifies it), so the result *is* the
constitutive record of the mandate, not a proxy for one.

Why derive rather than scrape the state, when every other roster collector fetches:

* **The state publishes no roster.** ``sc.gov.br`` names the governor in prose on a
  page built for humans; ``dados.sc.gov.br`` (CKAN, 112 datasets) has budget,
  spending and legislation, and nothing that says who holds the office. Scraping a
  name out of a headline would be a weaker claim from a more fragile source.
* **Identity would get worse, not better.** A scraped page yields a display name. The
  TSE row yields the **CPF**, which is what lets `resolution.deterministic` match the
  incumbent to their 2026 candidacy at `cpf_exact` / `auto_strong` — the strongest
  tier the pipeline has, and the one the badge is gated on. ALESC's name-only rosters
  are the standing example of what the alternative costs.
* **It generalizes.** Nothing here is Santa Catarina-specific. Widening
  `RESUMO_TARGET_UFS` seeds every state's governor from the same national file.

🚨 This reads rows the TSE collector wrote; it does **not** re-read the TSE. Run
``collect tse-candidates`` for the previous general election first, or this returns
``empty`` — which is the correct outcome for "the source is not in base yet", not an
error. The ledger entry records the derivation and names the election it came from.

🚨 `Mandate.data_fim` stays ``NULL`` for a term still running, even though its end
date is known and constitutional. See :func:`resumo.cargos.executive_term`.
"""

from __future__ import annotations

import datetime as dt
import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from resumo import cargos
from resumo.config import get_settings
from resumo.db.models import Candidacy, House, Mandate, Person
from resumo.ingestion.base import Collector, CollectorResult
from resumo.ingestion.ledger import record_ingestion
from resumo.util import clean, normalize_name, valid_cpf

logger = logging.getLogger("resumo.ingestion.executivo")

# TSE's outcome vocabulary for a majoritarian race. The proportional variants
# ("ELEITO POR QP", "ELEITO POR MÉDIA") cannot occur at CD_CARGO 3 and are left out
# rather than accepted defensively — matching them here would silently admit a row
# from the wrong office if the cargo filter were ever loosened.
ELECTED = "ELEITO"

# What the office is called in `Mandate.situacao`, mirroring how the legislative
# collectors record an exercised seat (the ficha's `em_exercicio` test reads the
# prefix "exerc").
SITUACAO_EM_EXERCICIO = "Exercício"


def _winners(session: Session, *, year: int, ufs: tuple[str, ...]) -> list[Candidacy]:
    stmt = select(Candidacy).where(
        Candidacy.ano_eleicao == year,
        Candidacy.cd_cargo == cargos.GOVERNADOR,
        Candidacy.ds_sit_tot_turno == ELECTED,
    )
    if ufs:
        stmt = stmt.where(Candidacy.sg_uf.in_(ufs))
    # One row per candidacy (SQ_CANDIDATO is the PK) and DS_SIT_TOT_TURNO carries the
    # FINAL outcome, so a runoff winner appears exactly once — no per-turno dedup.
    return list(session.execute(stmt.order_by(Candidacy.sg_uf)).scalars())


def _get_or_create_person(session: Session, cand: Candidacy) -> Person:
    """Reuse the CPF-bearing person if one exists; otherwise seed from the TSE row.

    CPF is the join key, and `Person.cpf` is unique — a governor who previously sat in
    the Câmara or Senado already has a person row, and creating a second one would
    split their history in two right where the pipeline needs it whole. Jorginho Mello
    is exactly that case: a senator (2019-2022) before the governorship.
    """
    cpf = valid_cpf(cand.cpf_raw)
    person = None
    if cpf:
        person = session.execute(select(Person).where(Person.cpf == cpf)).scalars().first()
    if person is None:
        person = Person(cpf=cpf)
        session.add(person)
    # The TSE row is the better source for all of these than whatever seeded the
    # person earlier, but never overwrite a known value with a blank one.
    person.nome_civil = clean(cand.nome_candidato) or person.nome_civil
    person.nome_normalizado = (
        normalize_name(cand.nome_candidato) or cand.nome_normalizado or person.nome_normalizado
    )
    person.data_nascimento = cand.data_nascimento or person.data_nascimento
    person.titulo_eleitoral = clean(cand.titulo_raw) or person.titulo_eleitoral
    session.flush()
    return person


def _upsert_mandate(session: Session, cand: Candidacy, *, posse: dt.date) -> Mandate:
    # `house_member_id` is the winning SQ_CANDIDATO: the executive publishes no member
    # id of its own, and the TSE sequence is the only stable, official handle for the
    # act that created this mandate. It also makes the row traceable straight back to
    # the candidacy it derives from.
    existing = session.execute(
        select(Mandate).where(
            Mandate.house == House.EXECUTIVO,
            Mandate.house_member_id == cand.sq_candidato,
            Mandate.id_legislatura == posse.year,
        )
    ).scalar_one_or_none()
    person = _get_or_create_person(session, cand)
    mandate = existing or Mandate(
        house=House.EXECUTIVO,
        house_member_id=cand.sq_candidato,
        # Not a legislature: the year the term began. See `Mandate`.
        id_legislatura=posse.year,
    )
    mandate.person_id = person.id
    # The name the ficha prints. Nome de urna, because that is how a governor is
    # publicly known and how the e-Legis acts refer to the office-holder.
    mandate.nome_parlamentar = clean(cand.nome_urna) or clean(cand.nome_candidato)
    # 🚨 The party the mandate was WON under, which is not necessarily today's. The
    # TSE row is a 2022 fact and is labelled as such wherever it surfaces; party
    # switching mid-term is common and this column must not be read as current.
    mandate.sigla_partido = clean(cand.sg_partido)
    mandate.sigla_uf = clean(cand.sg_uf)
    mandate.condicao_eleitoral = "Titular"
    mandate.situacao = SITUACAO_EM_EXERCICIO
    mandate.data_inicio = posse
    # 🚨 Left NULL on purpose while the term runs — `data_fim IS NULL` is what marks a
    # mandate active, and the whole point of this collector is that the seat is held.
    mandate.data_fim = None
    if existing is None:
        session.add(mandate)
    session.flush()
    return mandate


class GovernadoresCollector(Collector):
    name = "executivo_governadores"

    def run(
        self,
        session: Session,
        *,
        year: int | None = None,
        ufs: list[str] | None = None,
        **_,
    ) -> CollectorResult:
        settings = get_settings()
        # `year` is the election that SEATED the incumbents — 2022 for the 2026 cycle.
        # Callers pass the election year they mean; the default derives it so the
        # re-pointing seam stays a single env var.
        election = year or cargos.previous_general_election(settings.election_year)
        scope = tuple(u.strip().upper() for u in ufs) if ufs is not None else settings.uf_list

        winners = _winners(session, year=election, ufs=scope)
        if not winners:
            where = ", ".join(scope) if scope else "todas as UFs"
            return CollectorResult(
                self.name,
                "empty",
                0,
                f"nenhum governador ELEITO em {election} para {where} — rode "
                f"`collect tse-candidates --year {election}` antes",
            )

        posse, fim = cargos.executive_term(election)
        for cand in winners:
            _upsert_mandate(session, cand, posse=posse)
            logger.info(
                "executivo_governadores: %s (%s/%s) mandato %s-%s",
                cand.nome_urna, cand.sg_uf, cand.sg_partido, posse.year, fim.year,
            )

        record_ingestion(
            session,
            collector_name=self.name,
            # Derived, and the ledger says so rather than claiming a URL was fetched:
            # the artifact behind this is the TSE candidacy file another collector
            # already recorded with its real source_url and content hash.
            source_url=f"derived:candidacy?ano={election}&cd_cargo={cargos.GOVERNADOR}&sit=ELEITO",
            digest=f"count={len(winners)}",
            row_count=len(winners),
        )
        return CollectorResult(
            self.name, "ingested", len(winners), f"eleição {election} · mandato {posse}-{fim}"
        )
