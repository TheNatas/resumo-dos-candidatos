"""Collector: Senado /votacao -> Vote (nominal positions) + derived AttendanceRecord.

One request returns whole votações with their `votos[]` already embedded (always the
full 81-seat roster), so both the roll-call and the presence signal come out of the
same payload — no per-votação fan-out, no second endpoint.

Two properties of this source drive the whole module:

🚨 **Secret votações carry no individual position.** ~57% of legislatura 57 is
`votacaoSecreta == "S"`, and on those every senator's `siglaVotoParlamentar` is
literally the word "Votou". Only the aggregates (`totalVotosSim/Nao/Abstencao`) are
published. Storing "Votou" as a position would fabricate a roll-call that does not
exist, so secret votações produce **no Vote rows at all**. (Inversely, on nominal
votações the aggregate totals are null — the two shapes never overlap.)

* **Attendance is derived here, not fetched.** The Senado publishes *no* attendance
  endpoint whatsoever (verified: `/plenario/lista/presenca` -> 404), so presence is
  read off the vote codes. That measures presence at **voting sessions only**, never
  at all plenary sessions — `derivation` says so, and any "faltas" figure downstream
  must be presented with that caveat.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy.orm import Session

from resumo.config import get_settings
from resumo.db.models import AttendanceRecord, Vote
from resumo.ingestion.base import Collector, CollectorResult
from resumo.ingestion.ledger import record_ingestion, upsert
from resumo.ingestion.senado.client import SenadoClient
from resumo.ingestion.senado.common import mandate_map
from resumo.util import clean, parse_date

# On a secret votação every entry reads "Votou": the senator took part, but the
# position is not published. Never a Vote position; still a presence signal.
SECRET_BALLOT_CODE = "Votou"

# Codes that are attendance/status markers, NOT positions. `P-NRV` (presente, não
# registrou voto) and the leave codes must never land in `Vote.tipo_voto`; the real
# vocabulary is Sim / Não / Abstenção / Presidente (art. 51 RISF) / OB / P-OD
# (the last two being obstruction, which IS a deliberate position).
NON_VOTE_CODES = frozenset({"P-NRV", "AP", "NCom", "MIS", "LS", "LP", "LPA", "LAP", "NA", "L7"})

# Every derived presence row describes the same kind of event: a plenary sitting in
# which at least one roll-call happened. Never a full plenary agenda.
ATTENDANCE_EVENT_TYPE = "Sessão de votação"

# NCom = "não compareceu": absent, no justification published.
ABSENT_CODES = frozenset({"NCom"})
# Leave/mission codes: absent, but with a published reason.
JUSTIFIED_ABSENCE_CODES = frozenset({"AP", "MIS", "LS", "LP", "LPA", "LAP"})
# "Dispositivo não citado" — the Senado's own reference table does not say what these
# mean, so they are neither presence nor absence. Recorded as UNKNOWN (`presente`
# NULL) and excluded from the presence denominator, rather than guessed in either
# direction: counting them present inflates the record, counting them absent invents
# a falta. Both would be a public claim the source does not support.
UNKNOWN_CODES = frozenset({"NA", "L7"})


def _year_windows(data_inicio: str, data_fim: str) -> list[tuple[str, str]]:
    """Split [inicio, fim] into calendar-year windows.

    /votacao caps a query at a 1-year range (a wider one is a hard HTTP 400,
    RFC7807 `application/problem+json`) unless `codigoParlamentar` is supplied.
    Chunking by calendar year keeps the query roster-wide and always inside the cap;
    it also keeps each response — which has no pagination and can be several MB —
    to a bounded size.
    """
    inicio = parse_date(data_inicio) or dt.date.today()
    fim = parse_date(data_fim) or dt.date.today()
    windows = []
    for year in range(inicio.year, fim.year + 1):
        start = max(inicio, dt.date(year, 1, 1))
        end = min(fim, dt.date(year, 12, 31))
        if start <= end:
            windows.append((start.isoformat(), end.isoformat()))
    return windows


def _id_votacao(v: dict) -> str:
    """`codigoSessaoVotacao` is the votação's own id and is what /votacao keys on.
    A handful of older rows omit it, so fall back to the composite that is always
    present: the session plus the votação's sequence within it.

    Prefixed like `Proposition.proposition_id`: `Vote` is unique on
    (id_votacao, house_member_id) with no `house` column, and both houses publish
    bare numeric ids for votações AND for members — an unprefixed Senado id could
    therefore silently overwrite a Câmara roll-call row.
    """
    codigo = clean(v.get("codigoSessaoVotacao"))
    if codigo:
        return f"SF{codigo}"
    sessao = clean(v.get("codigoSessao")) or ""
    return f"SF{sessao}-{clean(v.get('sequencialVotacao')) or ''}"


def _attendance(code: str | None, descricao: str | None) -> tuple[bool | None, str | None]:
    """(presente, justificativa) for a vote code, where None means "unknown".

    Anything outside the absence and unknown sets counts as present — including
    "Votou" (took part in a secret ballot), P-NRV (in the room, did not register)
    and every real position. The absence set is deliberately narrow: an
    unrecognized code must not silently become a "falta" on a public track record.
    """
    if code in UNKNOWN_CODES:
        return None, descricao or code
    if code in JUSTIFIED_ABSENCE_CODES:
        return False, descricao or code
    if code in ABSENT_CODES:
        return False, None
    return True, None


class VotacoesCollector(Collector):
    name = "senado_votacoes"

    def run(
        self,
        session: Session,
        *,
        data_inicio: str,
        data_fim: str,
        id_legislatura: int | None = None,
        client: SenadoClient | None = None,
        limit: int | None = None,
        **_,
    ) -> CollectorResult:
        settings = get_settings()
        leg = id_legislatura or settings.id_legislatura
        # In a state-scoped install the mandate map holds only that state's senators;
        # every votação still lists all 81, so rows for members we do not track are
        # dropped rather than stored with a dangling mandate_id.
        scoped = bool(settings.uf_list)
        owns = client is None
        client = client or SenadoClient()
        try:
            mandates = mandate_map(session, leg)

            votacoes: list[dict] = []
            for start, end in _year_windows(data_inicio, data_fim):
                # Modern endpoint: bare JSON array, no envelope, AAAA-MM-DD params.
                payload = client.get("votacao", {"dataInicio": start, "dataFim": end})
                votacoes.extend(payload if isinstance(payload, list) else [])
            if limit:
                votacoes = votacoes[:limit]

            votes_total = 0
            attendance_total = 0
            secret = 0
            for v in votacoes:
                id_votacao = _id_votacao(v)
                data = parse_date(v.get("dataSessao"))
                is_secret = clean(v.get("votacaoSecreta")) == "S"
                secret += int(is_secret)
                # `idProcesso` is the modern /processo id — prefixed the same way
                # Proposition does, so the two join without a translation table.
                id_processo = clean(v.get("idProcesso"))
                id_proposicao = f"SF{id_processo}" if id_processo else None
                # One attendance row per *session*, keyed on codigoSessao: several
                # votações share a session, and presence is a property of the
                # session, not of each roll-call inside it. Same "SF" prefix, same
                # reason: AttendanceRecord is unique on (id_evento, house_member_id)
                # across every house.
                sessao = clean(v.get("codigoSessao"))
                id_evento = f"SF{sessao}" if sessao else id_votacao

                vote_rows = []
                attendance_rows = []
                for voto in v.get("votos") or []:
                    member_id = clean(voto.get("codigoParlamentar"))
                    if not member_id or (scoped and member_id not in mandates):
                        continue
                    code = clean(voto.get("siglaVotoParlamentar"))
                    descricao = clean(voto.get("descricaoVotoParlamentar"))

                    presente, justificativa = _attendance(code, descricao)
                    attendance_rows.append(
                        {
                            "mandate_id": mandates.get(member_id),
                            "house_member_id": member_id,
                            "id_evento": id_evento,
                            "data": data,
                            # The event is the session, so its type is fixed. Using
                            # the votação's own description here would make `tipo`
                            # flip-flop between the several votações of one session.
                            "tipo": ATTENDANCE_EVENT_TYPE,
                            "presente": presente,
                            "justificativa": justificativa,
                            "derivation": "senado_votacao_comparecimento",
                        }
                    )

                    # A secret votação publishes no position for anyone, and a
                    # non-vote code is a status, not a position — neither becomes a
                    # Vote row. "Votou" is rejected on its own too (belt and braces,
                    # in case the secrecy flag is ever missing from a payload).
                    if is_secret or not code or code in NON_VOTE_CODES:
                        continue
                    if code == SECRET_BALLOT_CODE:
                        continue
                    vote_rows.append(
                        {
                            "mandate_id": mandates.get(member_id),
                            "house_member_id": member_id,
                            "id_votacao": id_votacao,
                            "id_proposicao": id_proposicao,
                            "tipo_voto": code,
                            "data_votacao": data,
                            # The Senado publishes no party orientation (there is no
                            # equivalent of Câmara's /orientacoes), so fidelity to the
                            # party line is simply not computable from this source.
                            "orientacao_partido": None,
                        }
                    )

                votes_total += upsert(
                    session, Vote, vote_rows, index_elements=["id_votacao", "house_member_id"]
                )
                # Several votações share one session, so this key is rewritten within
                # a run: the session's LAST roll-call is what its presence row
                # reflects (a senator who left early counts as absent for it).
                attendance_total += upsert(
                    session,
                    AttendanceRecord,
                    attendance_rows,
                    index_elements=["id_evento", "house_member_id"],
                )

            record_ingestion(
                session,
                collector_name=self.name,
                source_url=f"{settings.senado_api_base}/votacao?{data_inicio}..{data_fim}",
                digest=f"votes={votes_total};presenca={attendance_total}",
                row_count=votes_total,
            )
            detail = (
                f"{len(votacoes)} votações ({secret} secretas) · "
                f"{attendance_total} presenças derivadas"
            )
            return CollectorResult(self.name, "ingested", votes_total, detail)
        finally:
            if owns:
                client.close()
