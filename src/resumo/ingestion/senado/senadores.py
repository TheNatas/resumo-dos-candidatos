"""Collector: Senado /senador/lista/legislatura/{leg} (+ detail, + mandatos)
-> Mandate, and seeds Person identity.

🚨 THE SENADO PUBLISHES NO CPF — ANYWHERE.
Verified against the full 157-path OpenAPI surface: no senator resource exposes a
CPF (every `cpf`/`cpfCnpj` field in this API belongs to a *supplier*, e.g. CEAPS).
So `Person.cpf` stays None for senators, and the deterministic cpf_exact match that
carries Câmara deputies is simply unavailable here: senator ↔ candidacy resolution
MUST go down the probabilistic path (nome_normalizado + data de nascimento + UF).
That is why this collector works so hard for `DataNascimento` — it is the only
discriminating attribute we get besides the name, and it costs one request per
senator (`/senador/{codigo}`), the only place it is published.

Roster source is the *legislatura* list, not `/senador/lista/atual`: "atual" omits
titulares who are currently licensed, and those are precisely the incumbents most
likely to be seeking re-election — driving off it would silently drop them.
"""

from __future__ import annotations

import datetime as dt
import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from resumo.config import get_settings
from resumo.db.models import House, Mandate, Person
from resumo.ingestion.base import Collector, CollectorResult
from resumo.ingestion.http import throttle
from resumo.ingestion.ledger import record_ingestion
from resumo.ingestion.senado.client import SenadoClient, _as_list, dig
from resumo.resolution.identity import resolve_person
from resumo.util import clean, parse_date, parse_int

logger = logging.getLogger("resumo.ingestion.senado")


def _parse_lista_legislatura(payload: dict) -> list[dict]:
    """`/senador/lista/legislatura/{leg}` -> Parlamentar[].

    Envelope AND shape differ from the "atual" list (which nests a *singular*
    `Mandato`), so the two lists deliberately do not share a parser.
    """
    return _as_list(dig(payload, "ListaParlamentarLegislatura", "Parlamentares", "Parlamentar"))


def _record_uf(parlamentar: dict) -> str | None:
    """The UF a roster record belongs to.

    Prefers `IdentificacaoParlamentar.UfParlamentar`, which is only present for
    senators in exercise, and falls back to the UF carried on the mandate — the
    only place it appears for licensed / former titulares.
    """
    uf = clean(dig(parlamentar, "IdentificacaoParlamentar", "UfParlamentar"))
    if uf:
        return uf.upper()
    for mandato in _as_list(dig(parlamentar, "Mandatos", "Mandato")):
        if isinstance(mandato, dict) and (uf := clean(mandato.get("UfParlamentar"))):
            return uf.upper()
    return None


def _parse_lista_atual(payload: dict, uf_scope: tuple[str, ...]) -> set[str]:
    """`/senador/lista/atual` -> {CodigoParlamentar} currently in exercise.

    🚨 The `uf` query param is SILENTLY IGNORED by this endpoint: it answers 200 with
    all 81 senators no matter what you pass. The filter below is the only one that
    exists — never assume the server applied one.
    """
    codes: set[str] = set()
    for p in _as_list(dig(payload, "ListaParlamentarEmExercicio", "Parlamentares", "Parlamentar")):
        ident = p.get("IdentificacaoParlamentar") or {}
        uf = clean(ident.get("UfParlamentar"))
        if uf_scope and (uf or "").upper() not in uf_scope:
            continue
        codigo = clean(ident.get("CodigoParlamentar"))
        if codigo:
            codes.add(codigo)
    return codes


def _mandate_window(mandatos: list[dict], leg: int) -> tuple[dt.date | None, dt.date | None]:
    """(data_inicio, data_fim) from the exercícios of the mandate covering `leg`.

    A senator's term spans two legislaturas, so a Mandato is matched when `leg` is
    either its first or its second. `data_fim` is None while any exercício is still
    open — an in-progress term must not look terminated (blocking reads that as
    "mandate_active").
    """
    inicios: list[dt.date] = []
    fins: list[dt.date] = []
    open_ended = False
    for m in mandatos:
        legs = {
            parse_int(dig(m, "PrimeiraLegislaturaDoMandato", "NumeroLegislatura")),
            parse_int(dig(m, "SegundaLegislaturaDoMandato", "NumeroLegislatura")),
        }
        if legs != {None} and leg not in legs:
            continue
        for ex in _as_list(dig(m, "Exercicios", "Exercicio")):
            inicio = parse_date(ex.get("DataInicio"))
            fim = parse_date(ex.get("DataFim"))
            if inicio:
                inicios.append(inicio)
            if fim:
                fins.append(fim)
            else:
                open_ended = True
    return (min(inicios) if inicios else None, None if open_ended or not fins else max(fins))


def _participacao(parlamentar: dict, leg: int) -> str | None:
    """Titular/Suplente for the mandate covering `leg` (legislatura-list shape)."""
    for m in _as_list(dig(parlamentar, "Mandatos", "Mandato")):
        legs = {
            parse_int(dig(m, "PrimeiraLegislaturaDoMandato", "NumeroLegislatura")),
            parse_int(dig(m, "SegundaLegislaturaDoMandato", "NumeroLegislatura")),
        }
        if legs == {None} or leg in legs:
            return clean(m.get("DescricaoParticipacao"))
    return None


def _get_or_create_person(session: Session, member_id: str, ident: dict, basicos: dict) -> Person:
    """Person for a senator — seeded through the shared resolver, always CPF-less.

    `resolve_person` bridges a CPF-less house on (nome_normalizado + data de
    nascimento), which is exactly the pair this collector fetches the detail
    endpoint for. Passing no CPF is deliberate, not an omission: the Senado has none
    to give, and the resolver only ever enriches, so a senator who is already known
    from the Câmara keeps that CPF instead of having it blanked.
    """
    nome_civil = clean(ident.get("NomeCompletoParlamentar")) or clean(
        ident.get("NomeParlamentar")
    )
    dob = parse_date(basicos.get("DataNascimento"))

    if dob is None:
        # Without a DOB the resolver has no bridge and would mint a fresh Person on
        # every pull. Fall back to whoever the mandate we wrote last time points at,
        # so re-runs stay idempotent even for a senator with no published birth date.
        prior = session.execute(
            select(Person)
            .join(Mandate, Mandate.person_id == Person.id)
            .where(Mandate.house == House.SENADO, Mandate.house_member_id == member_id)
            .limit(1)
        ).scalar_one_or_none()
        if prior is not None:
            return prior

    return resolve_person(
        session,
        nome_civil=nome_civil,
        dob=dob,
        # Naturalidade = birth state. UfParlamentar is the state *represented* and
        # belongs on the Mandate, never on the Person.
        uf_nascimento=basicos.get("UfNaturalidade"),
    )


def _upsert_mandate(
    session: Session,
    member_id: str,
    leg: int,
    ident: dict,
    person: Person,
    *,
    condicao: str | None,
    situacao: str | None,
    data_inicio: dt.date | None,
    data_fim: dt.date | None,
    sigla_uf: str | None,
) -> None:
    existing = session.execute(
        select(Mandate).where(
            Mandate.house == House.SENADO,
            Mandate.house_member_id == member_id,
            Mandate.id_legislatura == leg,
        )
    ).scalar_one_or_none()
    mandate = existing or Mandate(
        house=House.SENADO, house_member_id=member_id, id_legislatura=leg
    )
    mandate.person_id = person.id
    mandate.nome_parlamentar = clean(ident.get("NomeParlamentar")) or mandate.nome_parlamentar
    mandate.sigla_partido = clean(ident.get("SiglaPartidoParlamentar"))
    # NOT `ident["UfParlamentar"]`: that is null for anyone out of exercise, and a
    # null here would silently remove the senator from resolution/blocking (which
    # blocks on UF) and from the emendas author bridge. `_record_uf` falls back to
    # the mandate block, where the UF is always present.
    mandate.sigla_uf = sigla_uf or mandate.sigla_uf
    mandate.condicao_eleitoral = condicao or mandate.condicao_eleitoral
    mandate.situacao = situacao or mandate.situacao
    mandate.data_inicio = data_inicio or mandate.data_inicio
    mandate.data_fim = data_fim
    if existing is None:
        session.add(mandate)
    session.flush()


class SenadoresCollector(Collector):
    name = "senado_senadores"

    def run(
        self,
        session: Session,
        *,
        id_legislatura: int | None = None,
        client: SenadoClient | None = None,
        limit: int | None = None,
        ufs: list[str] | None = None,
        **_,
    ) -> CollectorResult:
        settings = get_settings()
        leg = id_legislatura or settings.id_legislatura
        uf_scope = tuple(u.upper() for u in ufs) if ufs is not None else settings.uf_list
        owns = client is None
        client = client or SenadoClient()
        try:
            # Unlike /senador/lista/atual, `uf` IS honoured server-side here.
            params = {"uf": uf_scope[0]} if len(uf_scope) == 1 else None
            listed = _parse_lista_legislatura(client.get(f"senador/lista/legislatura/{leg}", params))
            if uf_scope:
                # Still filter client-side: the param takes a single UF, and a
                # multi-UF scope (or a future server-side regression) must not leak.
                #
                # But `IdentificacaoParlamentar.UfParlamentar` is populated ONLY for
                # senators currently in exercise — for everyone else it is null, and
                # the UF lives in the mandate block instead. Filtering on the
                # identification field alone silently deletes exactly the people this
                # collector exists to find: titulares who are licensed or have left
                # office (e.g. Jorge Seif, Jorginho Mello in SC), who are prime
                # candidates for re-election. So: drop a record only when a UF is
                # actually known AND out of scope.
                listed = [p for p in listed if (_record_uf(p) or "") in uf_scope or not _record_uf(p)]
            if limit:
                listed = listed[:limit]

            throttle()
            em_exercicio = _parse_lista_atual(client.get("senador/lista/atual"), uf_scope)

            count = 0
            for item in listed:
                ident = item.get("IdentificacaoParlamentar") or {}
                member_id = clean(ident.get("CodigoParlamentar"))
                if not member_id:
                    continue

                throttle()
                detail = client.get(f"senador/{member_id}")
                d_ident = dig(detail, "DetalheParlamentar", "Parlamentar", "IdentificacaoParlamentar") or {}
                basicos = dig(detail, "DetalheParlamentar", "Parlamentar", "DadosBasicosParlamentar") or {}

                throttle()
                mandatos = _as_list(
                    dig(
                        client.get(f"senador/{member_id}/mandatos"),
                        "MandatoParlamentar",
                        "Parlamentar",
                        "Mandatos",
                        "Mandato",
                    )
                )
                data_inicio, data_fim = _mandate_window(mandatos, leg)

                person = _get_or_create_person(session, member_id, {**ident, **d_ident}, basicos)
                _upsert_mandate(
                    session,
                    member_id,
                    leg,
                    {**d_ident, **ident},  # list values win: they are legislatura-scoped
                    person,
                    condicao=_participacao(item, leg),
                    # "Exercício" is the Câmara vocabulary; reusing it keeps the
                    # public surface house-agnostic.
                    situacao="Exercício" if member_id in em_exercicio else "Fora de exercício",
                    data_inicio=data_inicio,
                    data_fim=data_fim,
                    sigla_uf=_record_uf(item),
                )
                count += 1

            record_ingestion(
                session,
                collector_name=self.name,
                source_url=(
                    f"{settings.senado_api_base}/senador/lista/legislatura/{leg}"
                    + (f"?uf={','.join(uf_scope)}" if uf_scope else "")
                ),
                digest=f"count={count}",
                row_count=count,
            )
            scope = f"legislatura {leg} · uf={','.join(uf_scope) or 'ALL'}"
            return CollectorResult(self.name, "ingested", count, scope)
        finally:
            if owns:
                client.close()
