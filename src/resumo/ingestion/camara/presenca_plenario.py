"""Collector: relatório oficial de presença em plenário da Câmara -> AttendanceSummary.

🚨 **Esta é a única fonte oficial brasileira que publica dias faltados prontos.** A
Mesa da Câmara divulga, por deputado e por ano, seis números já reconciliados
(inclusive com as justificativas aceitas *depois* da sessão), separando ausência
justificada de não justificada e restringindo o total ao período de exercício do
mandato. Nada disso é derivável da API de dados abertos:
``/eventos/{id}/deputados`` devolve **apenas quem compareceu**, sem tipo de evento e
sem noção de ausência — foi o que fez a ficha exibir 100% para todo deputado federal.

Duas linhas por ano são gravadas, uma por régua, porque a fonte publica as duas e
elas **não batem de propósito**:

* ``DIA``    — dias com sessão deliberativa, presença, ausência justificada e não
  justificada. É a régua que separa os dois tipos de ausência.
* ``SESSAO`` — sessões deliberativas com Ordem do Dia iniciada e ausências não
  justificadas nelas. A fonte não publica ausência justificada nesta régua, então
  ela fica nula em vez de ser importada da outra.

Converter uma na outra seria inventar: um deputado ausente na extraordinária nº 277 e
presente na nº 278 do mesmo dia conta como ausência de sessão **e** como dia com
presença. Ver :mod:`resumo.attendance`.

Custo: uma página HTML por deputado × ano (para SC, ~16 deputados × 4 anos = 64
requisições; nacional seriam ~2.052), com o throttle padrão entre elas.
"""

from __future__ import annotations

import logging

import httpx
from sqlalchemy.orm import Session

from resumo import attendance as att
from resumo.config import get_settings
from resumo.db.models import AttendanceSummary, AttendanceUnit, House
from resumo.ingestion.base import Collector, CollectorResult
from resumo.ingestion.camara.client import CamaraClient
from resumo.ingestion.camara.common import mandate_map
from resumo.ingestion.camara.parsing import (
    CamaraParseError,
    PresencaPlenario,
    parse_presenca_plenario,
)
from resumo.ingestion.ledger import content_hash, record_ingestion, upsert

logger = logging.getLogger("resumo.ingestion.camara")

DERIVATION = "camara_presenca_plenario_oficial"
PATH = "deputados/{member_id}/presenca-plenario/{ano}"


def default_years(election_year: int) -> list[int]:
    """Os quatro anos da legislatura que termina na eleição corrente.

    Uma legislatura dura quatro sessões legislativas e a última coincide com o ano da
    eleição (57ª = 2023–2026 para a eleição de 2026), então o intervalo sai do ano de
    eleição configurado em vez de uma tabela de legislaturas que teria de ser mantida
    à mão. Anos fora do exercício do parlamentar respondem "não há dados" e são
    simplesmente pulados, de modo que um intervalo largo demais é inofensivo.
    """
    return list(range(election_year - 3, election_year + 1))


def _summary_rows(presenca: PresencaPlenario) -> list[att.SummaryRow]:
    rows: list[att.SummaryRow] = []
    if presenca.dias_total is not None:
        rows.append(
            att.SummaryRow(
                ano=presenca.ano,
                unidade=AttendanceUnit.DIA,
                total=presenca.dias_total,
                presenca=presenca.dias_presenca_efetiva or 0,
                ausencia_justificada=presenca.dias_ausencia_justificada,
                ausencia_nao_justificada=presenca.dias_ausencia_nao_justificada,
            )
        )
    if presenca.sessoes_total is not None:
        rows.append(
            att.SummaryRow(
                ano=presenca.ano,
                unidade=AttendanceUnit.SESSAO,
                total=presenca.sessoes_total,
                presenca=presenca.sessoes_presenca or 0,
                # A Mesa não publica ausência justificada por sessão — só por dia.
                # Puxar o número da outra régua misturaria as duas contagens.
                ausencia_justificada=None,
                ausencia_nao_justificada=presenca.sessoes_ausencia_nao_justificada,
            )
        )
    return rows


class PresencaPlenarioCollector(Collector):
    name = "camara_presenca_plenario"

    def run(
        self,
        session: Session,
        *,
        anos: list[int] | None = None,
        id_legislatura: int | None = None,
        client: CamaraClient | None = None,
        limit: int | None = None,
        **_,
    ) -> CollectorResult:
        settings = get_settings()
        leg = id_legislatura or settings.id_legislatura
        years = sorted(set(anos or default_years(settings.election_year)))

        mandates = mandate_map(session, leg)
        if not mandates:
            return CollectorResult(
                self.name, "empty", 0,
                f"no CAMARA mandates for legislatura {leg} — run camara-deputados first",
            )
        members = sorted(mandates)[:limit] if limit else sorted(mandates)

        owns = client is None
        client = client or CamaraClient()
        try:
            total = 0
            pages = 0
            sem_dados = 0
            falhas = 0
            digests: list[str] = []
            for member_id in members:
                for ano in years:
                    path = PATH.format(member_id=member_id, ano=ano)
                    url = f"{settings.camara_portal_base.rstrip('/')}/{path}"
                    try:
                        markup = client.get_portal_html(path)
                    except httpx.HTTPError as exc:
                        # O portal responde 500 (não 404) para id inexistente, e uma
                        # página instável não pode derrubar a coleta inteira: registra
                        # e segue para o próximo deputado/ano.
                        falhas += 1
                        logger.warning("%s: %s falhou (%s) — pulado", self.name, url, exc)
                        continue
                    pages += 1
                    digests.append(content_hash(markup))
                    try:
                        presenca = parse_presenca_plenario(markup, ano=ano)
                    except CamaraParseError as exc:
                        falhas += 1
                        logger.error("%s: %s — %s", self.name, url, exc)
                        continue
                    if presenca is None:
                        # "Não há dados disponíveis para o ano de X": o deputado não
                        # estava em exercício. NÃO é zero falta — nada é gravado, para
                        # que a ficha não afirme presença perfeita num ano inexistente.
                        sem_dados += 1
                        continue
                    rows = [
                        row.as_row(
                            mandate_id=mandates[member_id],
                            house=House.CAMARA,
                            house_member_id=member_id,
                            metrica=att.CAMARA_PLENARIO,
                            derivation=DERIVATION,
                            source_url=url,
                        )
                        for row in _summary_rows(presenca)
                    ]
                    total += upsert(
                        session,
                        AttendanceSummary,
                        rows,
                        index_elements=["mandate_id", "ano", "ambito", "unidade"],
                    )

            record_ingestion(
                session,
                collector_name=self.name,
                source_url=(
                    f"{settings.camara_portal_base.rstrip('/')}/deputados/*/presenca-plenario/"
                    f"{{{years[0]}..{years[-1]}}}" if years else settings.camara_portal_base
                ),
                # Hash sobre os hashes das páginas lidas, não `count=N`: uma recoleta
                # em que nada mudou no portal produz o mesmo dígito, e o ledger mostra
                # isso. `count=N` seria idêntico mesmo com os números trocados.
                digest=content_hash("\n".join(sorted(digests))),
                row_count=total,
            )
            detail = f"{len(members)} deputados × {len(years)} anos · {pages} páginas"
            if sem_dados:
                detail += f" · {sem_dados} ano(s) sem dados (fora do exercício)"
            if falhas:
                detail += f" · {falhas} falha(s)"
            return CollectorResult(self.name, "ingested" if total else "empty", total, detail)
        finally:
            if owns:
                client.close()
