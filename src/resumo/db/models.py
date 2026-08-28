"""Normalized data model.

The CENTRAL entity is :class:`CandidateMandateLink` — a materialized, auditable
edge ("this TSE candidacy is the same person who holds this Câmara mandate") that
carries the match method, confidence and provenance. The public site only shows a
track record when this edge confirms incumbent-reelection at an accepted tier.

Source of truth per entity is noted in the docstring.
"""

from __future__ import annotations

import datetime as dt
import enum
import uuid

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from resumo.db.session import Base


# ── Enums ────────────────────────────────────────────────────────────────────
class House(str, enum.Enum):
    """The body a mandate is held in.

    ASSEMBLEIA is the generic state-assembly slot (ALESC for SC); the concrete state
    is carried by `Mandate.sigla_uf`, so the enum stays national.

    🚨 EXECUTIVO is **not** a legislative house, and three of this enum's members
    being legislatures is load-bearing everywhere else in the schema. It is the
    generic state-executive slot (Governadoria for SC), and it exists because a
    sitting governor demonstrably holds a mandate: without a row here, the platform
    could only say "no accepted link", which reads to a reader as "not an incumbent"
    — precisely the false negative the rest of the code works to avoid.

    What it necessarily does NOT have, by nature and not by a collection gap:
    `Vote` (an executive casts none), `AttendanceRecord` (no roll is called),
    `MandateLeave` and `Expense` (no cota parlamentar exists to reimburse). Only
    `Proposition` is populated, and it means something different there — see
    `resumo.ingestion.executivo.atos`. Anything that fans out over `House` must
    therefore ask what the office *can* publish, never assume the legislative shape.
    """

    CAMARA = "CAMARA"
    SENADO = "SENADO"
    ASSEMBLEIA = "ASSEMBLEIA"
    EXECUTIVO = "EXECUTIVO"

    @property
    def is_legislative(self) -> bool:
        """Whether roll-calls, attendance and a cota parlamentar exist for this body.

        The predicate the ficha branches on. Written as a property rather than an
        `is not EXECUTIVO` check at each call site so that adding a second executive
        slot (prefeitura, presidência) stays a one-line change here.
        """
        return self is not House.EXECUTIVO

    @property
    def label(self) -> str:
        """Human-readable name for the public UI."""
        return {
            House.CAMARA: "Câmara dos Deputados",
            House.SENADO: "Senado Federal",
            House.ASSEMBLEIA: "Assembleia Legislativa",
            House.EXECUTIVO: "Governo do Estado",
        }[self]

    @property
    def expense_label(self) -> str:
        """What THIS house calls the office-expense allowance.

        Not cosmetic. "CEAP" is the *Câmara's* name for its own cota; the Senado's is
        CEAPS; and the ALESC lines are verba de gabinete somada a diárias. Three
        regimes, three sets of rules, three different ceilings — printing "CEAP" over
        a state deputy's total invites exactly the cross-house comparison the rest of
        the ficha goes out of its way to refuse.

        The executive has no such allowance at all — the state budget is not a
        reimbursement of a member's office costs — so it gets a label that says so
        instead of borrowing a legislative one. The ficha does not draw the counter
        for an executive mandate (`is_legislative`), but a payload consumer might.
        """
        return {
            House.CAMARA: "CEAP (cota parlamentar)",
            House.SENADO: "CEAPS (cota parlamentar)",
            House.ASSEMBLEIA: "verba de gabinete e diárias",
            House.EXECUTIVO: "não há cota parlamentar em cargo executivo",
        }[self]


class AttendanceUnit(str, enum.Enum):
    """The ruler a source counts attendance in — and therefore the one we display.

    Não é escolha nossa: a Câmara publica *dias com sessão deliberativa*, a ALESC
    publica *sessões plenárias*, e o Senado só permite contar as *sessões* em que
    houve votação nominal. Converter um no outro exigiria inventar o que a fonte não
    diz (duas sessões no mesmo dia não são dois dias; um dia sem votação nominal não
    é uma sessão ausente), então cada resumo carrega a unidade em que foi publicado e
    a ficha usa esse substantivo — "dias" ou "sessões" — em vez de um número mudo.
    """

    SESSAO = "SESSAO"
    DIA = "DIA"

    @property
    def label(self) -> str:
        return {AttendanceUnit.SESSAO: "sessões", AttendanceUnit.DIA: "dias"}[self]

    @property
    def label_singular(self) -> str:
        return {AttendanceUnit.SESSAO: "sessão", AttendanceUnit.DIA: "dia"}[self]


class MatchMethod(str, enum.Enum):
    cpf_exact = "cpf_exact"
    # CPF recuperado do histórico do próprio TSE para Casas que não o publicam
    # (ALESC). O vínculo é igualdade de CPF, mas passa por um salto de nome de urna:
    # método distinto para que a ficha não anuncie mais certeza do que existe.
    cpf_via_tse = "cpf_via_tse"
    titulo_exact = "titulo_exact"
    probabilistic = "probabilistic"
    manual = "manual"


class ConfidenceTier(str, enum.Enum):
    auto_strong = "auto_strong"
    auto_weak = "auto_weak"
    review = "review"


class ReviewStatus(str, enum.Enum):
    pending = "pending"
    match = "match"
    no_match = "no_match"
    uncertain = "uncertain"


def _uuid() -> uuid.UUID:
    return uuid.uuid4()


# ── Identity ─────────────────────────────────────────────────────────────────
class Person(Base):
    """Canonical cross-election identity (synthesized by the resolution pipeline;
    seeded from Câmara /deputados/{id} which always exposes CPF)."""

    __tablename__ = "person"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    cpf: Mapped[str | None] = mapped_column(String(11), unique=True, index=True)
    titulo_eleitoral: Mapped[str | None] = mapped_column(String(20), index=True)
    nome_civil: Mapped[str | None] = mapped_column(String(255))
    nome_normalizado: Mapped[str | None] = mapped_column(String(255), index=True)
    data_nascimento: Mapped[dt.date | None] = mapped_column(Date)
    uf_nascimento: Mapped[str | None] = mapped_column(String(2))
    match_confidence_tier: Mapped[ConfidenceTier | None] = mapped_column(Enum(ConfidenceTier))

    candidacies: Mapped[list[Candidacy]] = relationship(back_populates="person")
    mandates: Mapped[list[Mandate]] = relationship(back_populates="person")
    aliases: Mapped[list[PersonIdentifierAlias]] = relationship(back_populates="person")


class PersonIdentifierAlias(Base):
    """Alternate names/handles for a person (nome_urna, nome_social, social URLs)."""

    __tablename__ = "person_identifier_alias"
    __table_args__ = (UniqueConstraint("person_id", "alias_type", "value"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    person_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("person.id"), index=True)
    alias_type: Mapped[str] = mapped_column(String(32))
    value: Mapped[str] = mapped_column(String(255))

    person: Mapped[Person] = relationship(back_populates="aliases")


# ── TSE side ─────────────────────────────────────────────────────────────────
class Candidacy(Base):
    """One TSE candidacy = one person running for one office in one turno.
    Source of truth: TSE consulta_cand bulk CSV."""

    __tablename__ = "candidacy"

    # SQ_CANDIDATO is globally unique in the TSE universe; used as natural PK.
    sq_candidato: Mapped[str] = mapped_column(String(32), primary_key=True)

    ano_eleicao: Mapped[int] = mapped_column(Integer, index=True)
    nr_turno: Mapped[int] = mapped_column(Integer, default=1)
    cd_eleicao: Mapped[int | None] = mapped_column(Integer)
    ds_eleicao: Mapped[str | None] = mapped_column(String(255))

    person_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("person.id"), index=True)

    # Raw identity as delivered by TSE (kept for re-resolution; may be masked).
    cpf_raw: Mapped[str | None] = mapped_column(String(14), index=True)
    titulo_raw: Mapped[str | None] = mapped_column(String(20), index=True)

    nome_candidato: Mapped[str | None] = mapped_column(String(255))
    nome_urna: Mapped[str | None] = mapped_column(String(255))
    nome_normalizado: Mapped[str | None] = mapped_column(String(255), index=True)
    data_nascimento: Mapped[dt.date | None] = mapped_column(Date)

    cd_cargo: Mapped[int | None] = mapped_column(Integer)
    ds_cargo: Mapped[str | None] = mapped_column(String(64), index=True)
    sg_uf: Mapped[str | None] = mapped_column(String(2), index=True)
    sg_ue: Mapped[str | None] = mapped_column(String(8))
    nm_ue: Mapped[str | None] = mapped_column(String(120))

    nr_candidato: Mapped[str | None] = mapped_column(String(8))
    sg_partido: Mapped[str | None] = mapped_column(String(32))
    nr_partido: Mapped[int | None] = mapped_column(Integer)
    nm_partido: Mapped[str | None] = mapped_column(String(120))

    sq_coligacao: Mapped[str | None] = mapped_column(ForeignKey("coalition.sq_coligacao"))
    ds_situacao_candidatura: Mapped[str | None] = mapped_column(String(64))
    ds_detalhe_situacao_cand: Mapped[str | None] = mapped_column(String(120))
    ds_sit_tot_turno: Mapped[str | None] = mapped_column(String(64))  # ELEITO/SUPLENTE/...
    st_reeleicao: Mapped[str | None] = mapped_column(String(8))

    is_majoritario: Mapped[bool] = mapped_column(Boolean, default=False)

    person: Mapped[Person | None] = relationship(back_populates="candidacies")
    coalition: Mapped[Coalition | None] = relationship(back_populates="candidacies")
    assets: Mapped[list[CandidateAsset]] = relationship(back_populates="candidacy")
    proposals: Mapped[list[GovernmentProposal]] = relationship(back_populates="candidacy")
    photo: Mapped[CandidatePhoto | None] = relationship(back_populates="candidacy")
    links: Mapped[list[CandidateMandateLink]] = relationship(back_populates="candidacy")


class CandidateAsset(Base):
    """Declared assets (bens). Source of truth: TSE bem_candidato bulk."""

    __tablename__ = "candidate_asset"
    __table_args__ = (UniqueConstraint("sq_candidato", "nr_ordem_bem"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    sq_candidato: Mapped[str] = mapped_column(ForeignKey("candidacy.sq_candidato"), index=True)
    nr_ordem_bem: Mapped[int] = mapped_column(Integer)
    ds_tipo_bem: Mapped[str | None] = mapped_column(String(255))
    ds_bem: Mapped[str | None] = mapped_column(Text)
    vr_bem: Mapped[float | None] = mapped_column(Numeric(18, 2))
    dt_ultima_atualizacao: Mapped[dt.date | None] = mapped_column(Date)

    candidacy: Mapped[Candidacy] = relationship(back_populates="assets")


class Coalition(Base):
    """Coalition/federation composition. Source of truth: TSE consulta_coligacao."""

    __tablename__ = "coalition"

    sq_coligacao: Mapped[str] = mapped_column(String(32), primary_key=True)
    nm_coligacao: Mapped[str | None] = mapped_column(String(255))
    ds_composicao_coligacao: Mapped[str | None] = mapped_column(Text)
    ano_eleicao: Mapped[int | None] = mapped_column(Integer)
    sg_uf: Mapped[str | None] = mapped_column(String(2))
    cd_cargo: Mapped[int | None] = mapped_column(Integer)

    candidacies: Mapped[list[Candidacy]] = relationship(back_populates="coalition")


class GovernmentProposal(Base):
    """Proposta de governo (majoritarian offices only).
    Source of truth: TSE proposta_governo per-UF zips; DivulgaCand PDF fallback."""

    __tablename__ = "government_proposal"
    __table_args__ = (UniqueConstraint("sq_candidato", "content_hash"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    sq_candidato: Mapped[str] = mapped_column(ForeignKey("candidacy.sq_candidato"), index=True)
    source: Mapped[str] = mapped_column(String(32))  # tse_bulk_pdf | divulgacand
    storage_path: Mapped[str | None] = mapped_column(String(512))
    original_filename: Mapped[str | None] = mapped_column(String(255))
    content_hash: Mapped[str] = mapped_column(String(64))
    extracted_text: Mapped[str | None] = mapped_column(Text)  # filled in S6
    fetched_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    candidacy: Mapped[Candidacy] = relationship(back_populates="proposals")


class CandidatePhoto(Base):
    """The registration photo the candidate filed with the Justiça Eleitoral.
    Source of truth: TSE per-UF `foto_cand<ano>_<UF>_div.zip`.

    Keyed on `sq_candidato` alone — unlike a proposta, of which a candidacy can
    legitimately file several, a candidacy has exactly ONE official photo. A second
    row would force the page to choose which face to show, and a re-issued photo
    must REPLACE the old one rather than sit beside it, so the natural key is the
    candidacy and re-ingesting is an update in place.
    """

    __tablename__ = "candidate_photo"

    sq_candidato: Mapped[str] = mapped_column(
        ForeignKey("candidacy.sq_candidato"), primary_key=True
    )
    source: Mapped[str] = mapped_column(String(32))  # tse_bulk_foto
    storage_path: Mapped[str | None] = mapped_column(String(512))
    original_filename: Mapped[str | None] = mapped_column(String(255))
    # Served straight to a browser, so the type is stored rather than guessed from
    # the extension at request time.
    media_type: Mapped[str] = mapped_column(String(32), default="image/jpeg")
    content_hash: Mapped[str] = mapped_column(String(64))
    fetched_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    candidacy: Mapped[Candidacy] = relationship(back_populates="photo")


# ── Legislative side ─────────────────────────────────────────────────────────
class Mandate(Base):
    """A held term (the 'exercício'). Source of truth: Câmara /deputados (+ detail)
    for CAMARA, and the equivalent roster collector for every other `House`.

    🚨 `id_legislatura` is a **legislature number** for the three legislative houses
    (57 = Câmara/Senado 2023-2027, 20 = ALESC 2023-2027) but an executive term has no
    legislature: for `House.EXECUTIVO` this column carries the **calendar year the
    term began** (2023 for the 2023-2026 governorship). Both are integers that
    partition the same person's successive terms, which is all the unique constraint
    needs — but they are not the same vocabulary, so never compare the number across
    houses or print it raw.
    """

    __tablename__ = "mandate"
    __table_args__ = (UniqueConstraint("house", "house_member_id", "id_legislatura"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    house: Mapped[House] = mapped_column(Enum(House), index=True)
    house_member_id: Mapped[str] = mapped_column(String(32), index=True)
    person_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("person.id"), index=True)

    id_legislatura: Mapped[int] = mapped_column(Integer, index=True)
    nome_parlamentar: Mapped[str | None] = mapped_column(String(255))
    sigla_partido: Mapped[str | None] = mapped_column(String(32))
    sigla_uf: Mapped[str | None] = mapped_column(String(2), index=True)
    condicao_eleitoral: Mapped[str | None] = mapped_column(String(32))  # Titular/Suplente
    situacao: Mapped[str | None] = mapped_column(String(64))  # Exercício/Licença/...
    data_inicio: Mapped[dt.date | None] = mapped_column(Date)
    data_fim: Mapped[dt.date | None] = mapped_column(Date)

    person: Mapped[Person | None] = relationship(back_populates="mandates")
    votes: Mapped[list[Vote]] = relationship(back_populates="mandate")
    propositions: Mapped[list[Proposition]] = relationship(back_populates="mandate")
    attendance: Mapped[list[AttendanceRecord]] = relationship(back_populates="mandate")
    attendance_summaries: Mapped[list[AttendanceSummary]] = relationship(back_populates="mandate")
    leaves: Mapped[list[MandateLeave]] = relationship(back_populates="mandate")
    expenses: Mapped[list[Expense]] = relationship(back_populates="mandate")
    amendments: Mapped[list[BudgetAmendment]] = relationship(back_populates="mandate")
    links: Mapped[list[CandidateMandateLink]] = relationship(back_populates="mandate")


class Vote(Base):
    """One nominal vote in a roll-call. Source of truth: Câmara /votacoes/{id}/votos."""

    __tablename__ = "vote"
    __table_args__ = (UniqueConstraint("id_votacao", "house_member_id"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    mandate_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("mandate.id"), index=True)
    house_member_id: Mapped[str] = mapped_column(String(32), index=True)
    id_votacao: Mapped[str] = mapped_column(String(64), index=True)
    id_proposicao: Mapped[str | None] = mapped_column(String(32))
    tipo_voto: Mapped[str | None] = mapped_column(String(32))  # Sim/Não/Obstrução/Abstenção
    data_votacao: Mapped[dt.date | None] = mapped_column(Date)
    orientacao_partido: Mapped[str | None] = mapped_column(String(32))

    mandate: Mapped[Mandate | None] = relationship(back_populates="votes")


class Proposition(Base):
    """A bill authored by the mandate-holder. Source of truth: Câmara
    /proposicoes?idDeputadoAutor=."""

    __tablename__ = "proposition"

    proposition_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    house: Mapped[House] = mapped_column(Enum(House), default=House.CAMARA)
    authoring_mandate_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("mandate.id"), index=True
    )
    sigla_tipo: Mapped[str | None] = mapped_column(String(16))
    numero: Mapped[int | None] = mapped_column(Integer)
    ano: Mapped[int | None] = mapped_column(Integer)
    ementa: Mapped[str | None] = mapped_column(Text)
    data_apresentacao: Mapped[dt.date | None] = mapped_column(Date)
    situacao: Mapped[str | None] = mapped_column(String(255))

    mandate: Mapped[Mandate | None] = relationship(back_populates="propositions")


class AttendanceRecord(Base):
    """Presença/falta per event. DERIVED (Câmara: event cross-ref) — labeled as such.
    Source of truth: Câmara /eventos/{id}/deputados."""

    __tablename__ = "attendance_record"
    __table_args__ = (UniqueConstraint("id_evento", "house_member_id"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    mandate_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("mandate.id"), index=True)
    house_member_id: Mapped[str] = mapped_column(String(32), index=True)
    id_evento: Mapped[str] = mapped_column(String(32))
    data: Mapped[dt.date | None] = mapped_column(Date)
    tipo: Mapped[str | None] = mapped_column(String(64))
    presente: Mapped[bool | None] = mapped_column(Boolean)
    justificativa: Mapped[str | None] = mapped_column(String(255))
    derivation: Mapped[str | None] = mapped_column(String(64))

    mandate: Mapped[Mandate | None] = relationship(back_populates="attendance")


class AttendanceSummary(Base):
    """Frequência consolidada de um mandato, **na unidade em que a fonte publica**.

    :class:`AttendanceRecord` tem grão de evento e não responde "quantos dias faltou":
    o denominador ali é *o que foi coletado*, não *o que era esperado*. Esta tabela
    guarda o consolidado com o universo esperado junto — e uma linha por
    :class:`AttendanceUnit`, porque uma fonte pode publicar as duas réguas:

    * **Câmara** (``camara_ato_mesa_191_2017``) — o relatório de presença em plenário
      publica as duas: sessões deliberativas com Ordem do Dia iniciada, e dias com
      sessão. Duas linhas, e elas **não batem de propósito** (um deputado ausente na
      extraordinária nº 277 e presente na nº 278 conta como *dia* presente).
    * **Senado** (``senado_sessao_votacao_nominal``) — derivada: só sessões em que
      houve votação nominal, porque não existe lista de presença publicada.
    * **ALESC** (``alesc_sessao_plenaria``) — sessões plenárias, presença observada.

    `total` é o denominador tal como a fonte o define, sempre **restrito ao período de
    exercício do parlamentar** quando a fonte assim o faz — por isso ele varia entre
    parlamentares no mesmo ano, e por isso a ficha mostra percentual e não só o
    absoluto.
    """

    __tablename__ = "attendance_summary"
    __table_args__ = (UniqueConstraint("mandate_id", "ano", "ambito", "unidade"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    mandate_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("mandate.id"), index=True)
    house: Mapped[House] = mapped_column(Enum(House), index=True)
    house_member_id: Mapped[str] = mapped_column(String(32), index=True)
    ano: Mapped[int] = mapped_column(Integer, index=True)
    # "plenario" hoje; "comissao" quando/se as reuniões de comissão forem coletadas.
    ambito: Mapped[str] = mapped_column(String(16), default="plenario")
    unidade: Mapped[AttendanceUnit] = mapped_column(Enum(AttendanceUnit))

    total: Mapped[int | None] = mapped_column(Integer)
    presenca: Mapped[int | None] = mapped_column(Integer)
    # Separadas só quando a fonte separa. A Câmara separa; a ALESC rotula *toda*
    # ausência como justificada sem publicar o motivo; o Senado só sabe distinguir
    # pelos códigos de licença/missão. `ausencia_nao_classificada` existe para a
    # ausência que a fonte registra sem dizer de que tipo é — somá-la a qualquer um
    # dos dois lados seria uma afirmação que a fonte não faz.
    ausencia_justificada: Mapped[int | None] = mapped_column(Integer)
    ausencia_nao_justificada: Mapped[int | None] = mapped_column(Integer)
    ausencia_nao_classificada: Mapped[int | None] = mapped_column(Integer)

    metrica: Mapped[str] = mapped_column(String(64), index=True)
    derivation: Mapped[str | None] = mapped_column(String(64))
    source_url: Mapped[str | None] = mapped_column(String(1024))

    mandate: Mapped[Mandate | None] = relationship(back_populates="attendance_summaries")


class MandateLeave(Base):
    """Licença/afastamento formal, em datas — a única ausência que o Senado publica
    como *dias corridos*. Source of truth: ``/senador/{codigo}/licencas``.

    Não entra na conta de presença: uma licença explica por que o parlamentar não
    estava lá, e a régua dela (dias corridos de calendário) não é a mesma das sessões.
    Fica ao lado, com o motivo que a própria Casa publicou.
    """

    __tablename__ = "mandate_leave"
    __table_args__ = (UniqueConstraint("mandate_id", "leave_id"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    mandate_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("mandate.id"), index=True)
    house: Mapped[House] = mapped_column(Enum(House), default=House.SENADO)
    house_member_id: Mapped[str] = mapped_column(String(32), index=True)
    # Id da licença na fonte (`Licenca/Codigo`), para a chave natural do upsert.
    leave_id: Mapped[str] = mapped_column(String(32))
    data_inicio: Mapped[dt.date | None] = mapped_column(Date)
    data_fim: Mapped[dt.date | None] = mapped_column(Date)
    sigla_tipo: Mapped[str | None] = mapped_column(String(64))
    descricao_tipo: Mapped[str | None] = mapped_column(String(255))

    mandate: Mapped[Mandate | None] = relationship(back_populates="leaves")


class Expense(Base):
    """CEAP (Cota Parlamentar) reimbursement line. Source of truth: Câmara
    /deputados/{id}/despesas."""

    __tablename__ = "expense"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    # CEAP has no reliable natural key (a single document yields multiple line items,
    # e.g. a charge + an estorno sharing codDoc/numDoc/parcela). Identity is a
    # deterministic hash over the salient fields: idempotent on re-fetch, distinct
    # for genuinely different line items.
    row_hash: Mapped[str] = mapped_column(String(64), unique=True)
    mandate_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("mandate.id"), index=True)
    house: Mapped[House] = mapped_column(Enum(House), default=House.CAMARA)
    house_member_id: Mapped[str] = mapped_column(String(32), index=True)
    ano: Mapped[int] = mapped_column(Integer, index=True)
    mes: Mapped[int | None] = mapped_column(Integer)
    parcela: Mapped[int | None] = mapped_column(Integer)
    tipo_despesa: Mapped[str | None] = mapped_column(String(255))
    valor_documento: Mapped[float | None] = mapped_column(Numeric(18, 2))
    valor_liquido: Mapped[float | None] = mapped_column(Numeric(18, 2))
    valor_glosa: Mapped[float | None] = mapped_column(Numeric(18, 2))
    cnpj_cpf_fornecedor: Mapped[str | None] = mapped_column(String(20))
    nome_fornecedor: Mapped[str | None] = mapped_column(String(255))
    cod_documento: Mapped[str] = mapped_column(String(32), default="")
    num_documento: Mapped[str] = mapped_column(String(64), default="")
    url_documento: Mapped[str | None] = mapped_column(String(512))

    mandate: Mapped[Mandate | None] = relationship(back_populates="expenses")


# ── Prestação de contas eleitorais (campaign finance) ────────────────────────
# Source of truth: TSE bulk `prestacao_contas/prestacao_de_contas_eleitorais_
# candidatos_<ANO>.zip` (NB: the CDN directory is `prestacao_contas`, while the
# FILE is `prestacao_de_contas_...`). One national zip; the UF split is inside it.
#
# Two joins matter and only one of them is universal:
#   * `sq_candidato` exists in receitas and despesas_contratadas ONLY.
#   * `sq_prestador_contas` (the accounting entity) exists in all four families and
#     is how the other two resolve back to a candidacy.
# Both are kept on every row so the resolution is inspectable rather than implied.


class AccountFiling(str, enum.Enum):
    """TP_PRESTACAO_CONTAS. The FINAL file retains earlier parcial/relatório rows, so
    aggregations MUST filter on this or the same money is counted twice."""

    final = "final"
    parcial = "parcial"
    relatorio_financeiro = "relatorio_financeiro"
    regularizacao_omissao = "regularizacao_omissao"
    outro = "outro"


class CampaignRevenue(Base):
    """A campaign receipt (doação/recurso). Source: receitas_candidatos_<ANO>_<UF>.csv.

    `SQ_RECEITA` looks like a key but is NOT unique: in 2022/SC, 72 sequences cover
    241 extra rows, and the copies are genuinely different money — same candidate,
    same turno, same filing type, but different `VR_RECEITA` and `DS_RECEITA`
    (e.g. R$ 142,50 and R$ 750,00 both filed as sequence 28316985). Keying on it
    silently discarded ~0.2% of declared revenue, so identity is a row hash, exactly
    as for the two despesa families.
    """

    __tablename__ = "campaign_revenue"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    row_hash: Mapped[str] = mapped_column(String(64), unique=True)
    sq_receita: Mapped[str | None] = mapped_column(String(32), index=True)

    sq_candidato: Mapped[str | None] = mapped_column(
        ForeignKey("candidacy.sq_candidato"), index=True
    )
    sq_prestador_contas: Mapped[str | None] = mapped_column(String(32), index=True)
    ano_eleicao: Mapped[int] = mapped_column(Integer, index=True)
    st_turno: Mapped[int | None] = mapped_column(Integer)  # source column is ST_TURNO, not NR_TURNO
    tp_prestacao_contas: Mapped[AccountFiling] = mapped_column(
        Enum(AccountFiling), default=AccountFiling.outro, index=True
    )
    dt_prestacao_contas: Mapped[dt.date | None] = mapped_column(Date)

    dt_receita: Mapped[dt.date | None] = mapped_column(Date)
    vr_receita: Mapped[float | None] = mapped_column(Numeric(18, 2))
    ds_receita: Mapped[str | None] = mapped_column(Text)
    ds_fonte_receita: Mapped[str | None] = mapped_column(String(120))
    ds_origem_receita: Mapped[str | None] = mapped_column(String(160))
    ds_natureza_receita: Mapped[str | None] = mapped_column(String(160))
    ds_especie_receita: Mapped[str | None] = mapped_column(String(120))

    # Donor. `nm_doador_rfb` is Receita Federal's canonical name — prefer it over the
    # self-declared `nm_doador` when resolving entities.
    nr_cpf_cnpj_doador: Mapped[str | None] = mapped_column(String(20), index=True)
    nm_doador: Mapped[str | None] = mapped_column(String(255))
    nm_doador_rfb: Mapped[str | None] = mapped_column(String(255))
    ds_cnae_doador: Mapped[str | None] = mapped_column(String(255))
    sg_uf_doador: Mapped[str | None] = mapped_column(String(2))
    nm_municipio_doador: Mapped[str | None] = mapped_column(String(120))
    # Set when the donor is itself a candidacy (candidate-to-candidate transfer).
    sq_candidato_doador: Mapped[str | None] = mapped_column(String(32), index=True)
    sg_partido_doador: Mapped[str | None] = mapped_column(String(32))

    candidacy: Mapped[Candidacy | None] = relationship()


class CampaignRevenueOriginator(Base):
    """Pass-through disclosure: who ORIGINALLY funded a receipt that reached the
    candidate via a party/other transfer. Source: receitas_candidatos_doador_
    originario_<ANO>_<UF>.csv, joined on SQ_RECEITA.

    Kept as its own table rather than denormalized onto the receipt because a single
    receipt can legitimately disclose more than one original donor.

    `sq_receita` is a plain indexed column, NOT a foreign key: the sequence is not
    unique on the revenue side either (see CampaignRevenue), so this is a join key,
    not a reference to one row."""

    __tablename__ = "campaign_revenue_originator"
    __table_args__ = (UniqueConstraint("sq_receita", "nr_cpf_cnpj_doador_originario"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    sq_receita: Mapped[str] = mapped_column(String(32), index=True)
    nr_cpf_cnpj_doador_originario: Mapped[str] = mapped_column(String(20), default="")
    nm_doador_originario: Mapped[str | None] = mapped_column(String(255))
    nm_doador_originario_rfb: Mapped[str | None] = mapped_column(String(255))
    tp_doador_originario: Mapped[str | None] = mapped_column(String(64))
    ds_cnae_doador_originario: Mapped[str | None] = mapped_column(String(255))
    vr_receita: Mapped[float | None] = mapped_column(Numeric(18, 2))


class CampaignExpense(Base):
    """A contracted campaign expense. Source: despesas_contratadas_candidatos_*.csv.

    `sq_despesa` is NOT unique — one contract yields many line items (installments,
    multi-line invoices), repeating up to ~90x. Identity is therefore a row hash."""

    __tablename__ = "campaign_expense"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    row_hash: Mapped[str] = mapped_column(String(64), unique=True)

    sq_despesa: Mapped[str | None] = mapped_column(String(32), index=True)
    sq_candidato: Mapped[str | None] = mapped_column(
        ForeignKey("candidacy.sq_candidato"), index=True
    )
    sq_prestador_contas: Mapped[str | None] = mapped_column(String(32), index=True)
    ano_eleicao: Mapped[int] = mapped_column(Integer, index=True)
    st_turno: Mapped[int | None] = mapped_column(Integer)
    tp_prestacao_contas: Mapped[AccountFiling] = mapped_column(
        Enum(AccountFiling), default=AccountFiling.outro, index=True
    )

    dt_despesa: Mapped[dt.date | None] = mapped_column(Date)
    vr_despesa_contratada: Mapped[float | None] = mapped_column(Numeric(18, 2))
    ds_despesa: Mapped[str | None] = mapped_column(Text)
    ds_origem_despesa: Mapped[str | None] = mapped_column(String(255))
    ds_tipo_documento: Mapped[str | None] = mapped_column(String(120))
    nr_documento: Mapped[str | None] = mapped_column(String(64))

    nr_cpf_cnpj_fornecedor: Mapped[str | None] = mapped_column(String(20), index=True)
    nm_fornecedor: Mapped[str | None] = mapped_column(String(255))
    nm_fornecedor_rfb: Mapped[str | None] = mapped_column(String(255))
    ds_cnae_fornecedor: Mapped[str | None] = mapped_column(String(255))
    sg_uf_fornecedor: Mapped[str | None] = mapped_column(String(2))
    nm_municipio_fornecedor: Mapped[str | None] = mapped_column(String(120))

    candidacy: Mapped[Candidacy | None] = relationship()


class CampaignPayment(Base):
    """An actual payment against a contracted expense. Source:
    despesas_pagas_candidatos_*.csv.

    This family carries NO candidate and NO supplier columns — it resolves to a
    candidacy through `sq_prestador_contas`, and to a counterparty through
    `sq_despesa` -> CampaignExpense. Aggregate each side to `sq_despesa` before
    joining: the relation is many-to-many and a naive join fans the totals out."""

    __tablename__ = "campaign_payment"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    row_hash: Mapped[str] = mapped_column(String(64), unique=True)

    sq_despesa: Mapped[str | None] = mapped_column(String(32), index=True)
    sq_parcelamento_despesa: Mapped[str | None] = mapped_column(String(32))
    sq_prestador_contas: Mapped[str | None] = mapped_column(String(32), index=True)
    # Backfilled from the prestador -> candidacy map built off receitas/contratadas.
    sq_candidato: Mapped[str | None] = mapped_column(
        ForeignKey("candidacy.sq_candidato"), index=True
    )
    ano_eleicao: Mapped[int] = mapped_column(Integer, index=True)
    st_turno: Mapped[int | None] = mapped_column(Integer)
    tp_prestacao_contas: Mapped[AccountFiling] = mapped_column(
        Enum(AccountFiling), default=AccountFiling.outro, index=True
    )

    dt_pagto_despesa: Mapped[dt.date | None] = mapped_column(Date)
    vr_pagto_despesa: Mapped[float | None] = mapped_column(Numeric(18, 2))
    ds_despesa: Mapped[str | None] = mapped_column(Text)
    ds_natureza_despesa: Mapped[str | None] = mapped_column(String(120))
    ds_especie_recurso: Mapped[str | None] = mapped_column(String(120))
    ds_fonte_despesa: Mapped[str | None] = mapped_column(String(120))
    ds_origem_despesa: Mapped[str | None] = mapped_column(String(255))

    candidacy: Mapped[Candidacy | None] = relationship()


# ── Emendas parlamentares (budget amendments) ────────────────────────────────
class AmendmentType(str, enum.Enum):
    """RP modality. Only the two *individual* types name a single legislator; the
    others belong to a bancada, a committee or the relator-geral, and attributing
    them to one person would be wrong."""

    individual_finalidade_definida = "individual_finalidade_definida"  # RP6
    individual_transferencia_especial = "individual_transferencia_especial"  # RP6 "PIX"
    bancada = "bancada"  # RP7 — the state's whole delegation
    comissao = "comissao"  # RP9 — a committee
    relator = "relator"  # RP8 — relator-geral ("orçamento secreto"), ended 2022
    outro = "outro"

    @property
    def is_individual(self) -> bool:
        return self in (
            AmendmentType.individual_finalidade_definida,
            AmendmentType.individual_transferencia_especial,
        )


class BudgetAmendment(Base):
    """One emenda parlamentar row. Source of truth: CGU bulk
    `EmendasParlamentares.zip` (Portal da Transparência download, no auth).

    Grain matches the source: one row per emenda x localidade x ação, so a single
    `codigo_emenda` legitimately appears many times. Identity is a hash over the
    salient fields (the source has no row-level key)."""

    __tablename__ = "budget_amendment"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    row_hash: Mapped[str] = mapped_column(String(64), unique=True)

    codigo_emenda: Mapped[str] = mapped_column(String(32), index=True)
    ano: Mapped[int] = mapped_column(Integer, index=True)
    tipo_emenda_raw: Mapped[str | None] = mapped_column(String(120))
    tipo: Mapped[AmendmentType] = mapped_column(Enum(AmendmentType), index=True)

    # SIOP author code. Stable within a mandate, NOT across a career: the same person
    # gets a new code when they change house, and a departed member's code is
    # reassigned to their successor. Hence the (code, ano) grain on the author link.
    siop_author_code: Mapped[str | None] = mapped_column(String(16), index=True)
    author_name_raw: Mapped[str | None] = mapped_column(String(255))
    author_name_normalizado: Mapped[str | None] = mapped_column(String(255), index=True)

    # Resolved via AmendmentAuthorLink; null until the bridge is built/reviewed.
    person_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("person.id"), index=True)
    mandate_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("mandate.id"), index=True)

    codigo_municipio_ibge: Mapped[str | None] = mapped_column(String(16))
    municipio: Mapped[str | None] = mapped_column(String(120))
    codigo_uf_ibge: Mapped[str | None] = mapped_column(String(16))
    uf: Mapped[str | None] = mapped_column(String(64), index=True)  # full state NAME in the source
    regiao: Mapped[str | None] = mapped_column(String(32))

    nome_funcao: Mapped[str | None] = mapped_column(String(120))
    nome_subfuncao: Mapped[str | None] = mapped_column(String(120))
    nome_programa: Mapped[str | None] = mapped_column(String(255))
    nome_acao: Mapped[str | None] = mapped_column(String(255))

    # NB: the source publishes NO "valor autorizado"/dotação. `valor_empenhado` is the
    # best available proxy and must be labeled as such in any UI.
    valor_empenhado: Mapped[float | None] = mapped_column(Numeric(18, 2))
    valor_liquidado: Mapped[float | None] = mapped_column(Numeric(18, 2))
    valor_pago: Mapped[float | None] = mapped_column(Numeric(18, 2))
    valor_resto_inscrito: Mapped[float | None] = mapped_column(Numeric(18, 2))
    valor_resto_cancelado: Mapped[float | None] = mapped_column(Numeric(18, 2))
    valor_resto_pago: Mapped[float | None] = mapped_column(Numeric(18, 2))

    person: Mapped[Person | None] = relationship()
    mandate: Mapped[Mandate | None] = relationship(back_populates="amendments")


class AmendmentAuthorLink(Base):
    """Materialized bridge: SIOP author code (per year) -> the mandate that authored.

    The emendas source carries no CPF and no Câmara/Senado id, so this edge is built
    ONCE by UF-scoped exact match on the normalized nome parlamentar and then pinned
    as reviewed data — never re-fuzzed at request time. Same auditability contract as
    :class:`CandidateMandateLink`: method, confidence and provenance are recorded, and
    a human decision is authoritative."""

    __tablename__ = "amendment_author_link"
    __table_args__ = (UniqueConstraint("siop_author_code", "ano"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    siop_author_code: Mapped[str] = mapped_column(String(16), index=True)
    ano: Mapped[int] = mapped_column(Integer, index=True)

    mandate_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("mandate.id"), index=True)
    person_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("person.id"), index=True)

    author_name_raw: Mapped[str | None] = mapped_column(String(255))
    match_method: Mapped[MatchMethod] = mapped_column(Enum(MatchMethod))
    confidence_score: Mapped[float] = mapped_column(Float, default=1.0)
    confidence_tier: Mapped[ConfidenceTier] = mapped_column(Enum(ConfidenceTier))
    resolver: Mapped[str | None] = mapped_column(String(64))
    resolved_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


# ── THE CENTRAL EDGE ─────────────────────────────────────────────────────────
class CandidateMandateLink(Base):
    """Materialized candidate<->mandate resolution. Inspectable & overridable."""

    __tablename__ = "candidate_mandate_link"
    __table_args__ = (UniqueConstraint("sq_candidato", "mandate_id"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    sq_candidato: Mapped[str] = mapped_column(ForeignKey("candidacy.sq_candidato"), index=True)
    mandate_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("mandate.id"), index=True)
    person_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("person.id"), index=True)

    match_method: Mapped[MatchMethod] = mapped_column(Enum(MatchMethod))
    confidence_score: Mapped[float] = mapped_column(Float)
    confidence_tier: Mapped[ConfidenceTier] = mapped_column(Enum(ConfidenceTier), index=True)
    is_incumbent_reelection: Mapped[bool] = mapped_column(Boolean, default=False, index=True)

    pipeline_version: Mapped[str | None] = mapped_column(String(32))
    resolver: Mapped[str | None] = mapped_column(String(64))
    resolved_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    candidacy: Mapped[Candidacy] = relationship(back_populates="links")
    mandate: Mapped[Mandate] = relationship(back_populates="links")


class ReviewQueue(Base):
    """Low-confidence/ambiguous pairs awaiting human sign-off. A decision here is an
    authoritative manual override the pipeline never overwrites."""

    __tablename__ = "review_queue"
    __table_args__ = (UniqueConstraint("sq_candidato", "mandate_id"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    sq_candidato: Mapped[str] = mapped_column(ForeignKey("candidacy.sq_candidato"), index=True)
    mandate_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("mandate.id"), index=True)
    suggested_score: Mapped[float | None] = mapped_column(Float)
    reason: Mapped[str | None] = mapped_column(String(255))
    candidate_snapshot: Mapped[dict | None] = mapped_column(JSONB)
    mandate_snapshot: Mapped[dict | None] = mapped_column(JSONB)
    status: Mapped[ReviewStatus] = mapped_column(
        Enum(ReviewStatus), default=ReviewStatus.pending, index=True
    )
    decided_by: Mapped[str | None] = mapped_column(String(64))
    decided_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))


# ── Provenance / idempotency ─────────────────────────────────────────────────
class RawIngestion(Base):
    """One row per fetched source artifact (zip/CSV/API payload) with its hash, so
    every normalized row is traceable and re-pulls are no-ops when unchanged."""

    __tablename__ = "raw_ingestion"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    collector_name: Mapped[str] = mapped_column(String(64), index=True)
    source_url: Mapped[str] = mapped_column(String(1024), index=True)
    source_generated_at: Mapped[str | None] = mapped_column(String(64))
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    row_count: Mapped[int] = mapped_column(BigInteger, default=0)
    status: Mapped[str] = mapped_column(String(32), default="success")
    fetched_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
