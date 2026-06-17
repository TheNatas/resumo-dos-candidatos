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
    CAMARA = "CAMARA"
    SENADO = "SENADO"  # reserved for S5; the model already supports it.


class MatchMethod(str, enum.Enum):
    cpf_exact = "cpf_exact"
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


# ── Legislative side ─────────────────────────────────────────────────────────
class Mandate(Base):
    """A held legislative term (the 'exercício'). Source of truth: Câmara
    /deputados (+ detail). `house` kept generic so Senado slots in later (S5)."""

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
    expenses: Mapped[list[Expense]] = relationship(back_populates="mandate")
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
