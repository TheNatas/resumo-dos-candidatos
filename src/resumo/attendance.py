"""Frequência parlamentar: o vocabulário de cada fonte, e a agregação das derivadas.

A regra que organiza este módulo, e que a ficha pública obedece: **exibir o número na
unidade em que a fonte o publica**. Não convertemos sessão em dia nem dia em sessão —
a conversão exigiria afirmar o que a fonte não diz. Duas sessões no mesmo dia não são
dois dias; um dia de sessão deliberativa sem votação nominal não é uma sessão em que
alguém esteve ausente. Cada :class:`~resumo.db.models.AttendanceSummary` carrega sua
:class:`~resumo.db.models.AttendanceUnit`, e o template usa o substantivo dela.

As três réguas, e por que são três:

===========  ==========================================  =========================
Casa         O que a fonte publica                       Unidade
===========  ==========================================  =========================
Câmara       relatório oficial de presença em plenário    ``DIA`` **e** ``SESSAO``
Senado       códigos de comparecimento das votações       ``SESSAO`` (derivada)
ALESC        folha de presença por sessão plenária        ``SESSAO``
===========  ==========================================  =========================

Só a Câmara publica *dias faltados* prontos. As outras duas são derivadas do que já
está em :class:`~resumo.db.models.AttendanceRecord`, e o rótulo diz isso.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass

from sqlalchemy import extract, func, select
from sqlalchemy.orm import Session

from resumo.db.models import AttendanceRecord, AttendanceUnit, House

# Âmbito do registro. Hoje só plenário é coletado nas três Casas; comissões existem
# na Câmara e na ALESC com vocabulário próprio (e, na ALESC, com reuniões inteiras
# marcadas "Ausente" por terem sido canceladas), então ficam de fora até serem
# coletadas com o cuidado que exigem — em vez de entrarem misturadas ao plenário.
AMBITO_PLENARIO = "plenario"

# Chaves de métrica. São gravadas no banco e aparecem na API pública, então mudá-las
# é uma quebra de contrato: cada uma nomeia a régua, não a Casa.
CAMARA_PLENARIO = "camara_ato_mesa_191_2017"
SENADO_VOTACAO_NOMINAL = "senado_sessao_votacao_nominal"
ALESC_SESSAO_PLENARIA = "alesc_sessao_plenaria"


@dataclass(frozen=True)
class Metric:
    """Como uma fonte conta presença, e o que precisa ser dito junto do número."""

    key: str
    house: House
    fonte: str
    derived: bool
    note: str
    # Unidade -> o que o denominador significa naquela unidade. A ficha imprime esse
    # texto ao lado do número, porque "104" e "100" no mesmo ano são coisas
    # diferentes (sessões deliberativas e dias com sessão) e um número sozinho mente.
    denominador: dict[AttendanceUnit, str]

    @property
    def unidades(self) -> tuple[AttendanceUnit, ...]:
        """Unidades publicadas, na ordem em que a ficha as exibe."""
        return tuple(self.denominador)


METRICS: dict[str, Metric] = {
    CAMARA_PLENARIO: Metric(
        key=CAMARA_PLENARIO,
        house=House.CAMARA,
        fonte="Relatório de presença em plenário — Câmara dos Deputados",
        derived=False,
        note=(
            "Números oficiais da Mesa (critério do Ato da Mesa nº 191/2017), no período "
            "de exercício do mandato — por isso o total varia de um deputado para outro "
            "no mesmo ano. Dia e sessão são réguas distintas e não batem entre si: quem "
            "faltou a uma sessão do dia e compareceu a outra conta como dia com presença. "
            "Não inclui reuniões de comissão. A Câmara admite justificativa posterior à "
            "sessão, então o número consolidado pode mudar depois."
        ),
        denominador={
            AttendanceUnit.DIA: "dias com sessão deliberativa realizada",
            AttendanceUnit.SESSAO: "sessões deliberativas com Ordem do Dia iniciada",
        },
    ),
    SENADO_VOTACAO_NOMINAL: Metric(
        key=SENADO_VOTACAO_NOMINAL,
        house=House.SENADO,
        fonte="Votações nominais — Dados Abertos do Senado Federal",
        derived=True,
        note=(
            "O Senado não publica lista de presença: não existe serviço de presença nos "
            "dados abertos. Esta contagem é DERIVADA dos códigos de comparecimento das "
            "votações nominais, então cobre apenas as sessões em que houve votação "
            "nominal — uma minoria das sessões deliberativas — e não é comparável com a "
            "da Câmara. Onde a fonte registra apenas \"não compareceu\", a ausência fica "
            "sem classificação: ela não diz se foi justificada."
        ),
        denominador={
            AttendanceUnit.SESSAO: "sessões de plenário com votação nominal",
        },
    ),
    ALESC_SESSAO_PLENARIA: Metric(
        key=ALESC_SESSAO_PLENARIA,
        house=House.ASSEMBLEIA,
        fonte="Folha de presença por sessão plenária — e-Legis/ALESC",
        derived=False,
        note=(
            "A ALESC publica a presença sessão a sessão, mas rotula TODA ausência como "
            "justificada e não publica o motivo — com esta fonte não há como afirmar "
            "falta injustificada. Só entram as sessões cuja folha de presença foi "
            "publicada (as extraordinárias em geral não são), e o e-Legis não tem "
            "registro anterior a fevereiro de 2023."
        ),
        denominador={
            AttendanceUnit.SESSAO: "sessões plenárias com folha de presença publicada",
        },
    ),
}

# Métrica derivada de cada `AttendanceRecord.derivation`. O que não estiver aqui não
# vira resumo: a Câmara alimenta `attendance_record` só com presenças (o endpoint de
# eventos não devolve ausentes), e agregar isso daria 100% para todo deputado.
DERIVATION_METRIC: dict[str, tuple[str, House]] = {
    "senado_votacao_comparecimento": (SENADO_VOTACAO_NOMINAL, House.SENADO),
    "alesc_sessao_presenca": (ALESC_SESSAO_PLENARIA, House.ASSEMBLEIA),
}


def metric_for(key: str | None) -> Metric | None:
    return METRICS.get(key) if key else None


@dataclass(frozen=True)
class SummaryRow:
    """Uma linha de resumo pronta para upsert, na unidade da fonte."""

    ano: int
    unidade: AttendanceUnit
    total: int
    presenca: int
    ausencia_justificada: int | None = None
    ausencia_nao_justificada: int | None = None
    ausencia_nao_classificada: int | None = None

    def as_row(
        self,
        *,
        mandate_id: uuid.UUID,
        house: House,
        house_member_id: str,
        metrica: str,
        derivation: str | None,
        source_url: str | None,
        ambito: str = AMBITO_PLENARIO,
    ) -> dict:
        return {
            "mandate_id": mandate_id,
            "house": house,
            "house_member_id": house_member_id,
            "ano": self.ano,
            "ambito": ambito,
            "unidade": self.unidade,
            "total": self.total,
            "presenca": self.presenca,
            "ausencia_justificada": self.ausencia_justificada,
            "ausencia_nao_justificada": self.ausencia_nao_justificada,
            "ausencia_nao_classificada": self.ausencia_nao_classificada,
            "metrica": metrica,
            "derivation": derivation,
            "source_url": source_url,
        }


def summarize_records(
    session: Session,
    *,
    derivation: str,
    mandate_ids: list[uuid.UUID] | None = None,
) -> dict[uuid.UUID, list[SummaryRow]]:
    """Consolida :class:`AttendanceRecord` por mandato e ano, em ``SESSAO``.

    O grão da tabela de origem é o evento, e para Senado e ALESC um evento **é** uma
    sessão — o Senado grava uma linha por ``codigoSessao`` e a ALESC uma por sessão
    plenária. Logo a contagem de linhas já é contagem de sessões; nada é convertido.

    Regras de classificação, iguais para as duas Casas:

    * ``presente IS NULL`` fica **fora do denominador**. A fonte publicou um código
      que ela própria não define; contá-lo como presença infla o histórico e
      contá-lo como ausência inventa uma falta.
    * ausência **com** justificativa publicada -> ``ausencia_justificada``.
    * ausência **sem** justificativa -> ``ausencia_nao_classificada``, nunca
      "não justificada". Nem o Senado nem a ALESC afirmam que a falta foi
      injustificada; só a Câmara publica essa distinção, e ela não passa por aqui.

    Linhas sem `data` não entram: sem ano não há a que somá-las.
    """
    absent = AttendanceRecord.presente.is_(False)
    stmt = (
        select(
            AttendanceRecord.mandate_id,
            extract("year", AttendanceRecord.data).label("ano"),
            func.count().label("total"),
            func.count().filter(AttendanceRecord.presente.is_(True)).label("presenca"),
            func.count()
            .filter(absent, AttendanceRecord.justificativa.isnot(None))
            .label("justificada"),
            func.count()
            .filter(absent, AttendanceRecord.justificativa.is_(None))
            .label("nao_classificada"),
        )
        .where(
            AttendanceRecord.derivation == derivation,
            AttendanceRecord.mandate_id.isnot(None),
            AttendanceRecord.data.isnot(None),
            AttendanceRecord.presente.isnot(None),
        )
        .group_by(AttendanceRecord.mandate_id, extract("year", AttendanceRecord.data))
    )
    if mandate_ids is not None:
        if not mandate_ids:
            return {}
        stmt = stmt.where(AttendanceRecord.mandate_id.in_(mandate_ids))

    out: dict[uuid.UUID, list[SummaryRow]] = {}
    for mandate_id, ano, total, presenca, justificada, nao_classificada in session.execute(stmt):
        out.setdefault(mandate_id, []).append(
            SummaryRow(
                ano=int(ano),
                unidade=AttendanceUnit.SESSAO,
                total=int(total),
                presenca=int(presenca),
                ausencia_justificada=int(justificada),
                ausencia_nao_classificada=int(nao_classificada),
            )
        )
    return out


def count_undated(session: Session, *, derivation: str) -> int:
    """Registros que não puderam entrar em nenhum ano por não terem data.

    Reportado no resultado do coletor em vez de descartado em silêncio: uma fonte que
    começa a omitir a data da sessão esvazia o resumo sem quebrar nada.
    """
    return session.scalar(
        select(func.count())
        .select_from(AttendanceRecord)
        .where(
            AttendanceRecord.derivation == derivation,
            AttendanceRecord.presente.isnot(None),
            AttendanceRecord.data.is_(None),
        )
    ) or 0


def leave_days(inicio: dt.date | None, fim: dt.date | None) -> int | None:
    """Dias corridos de uma licença, inclusive nas duas pontas (o padrão do Senado:
    uma licença de 14/12 a 14/12 é um dia, não zero)."""
    if inicio is None or fim is None or fim < inicio:
        return None
    return (fim - inicio).days + 1
