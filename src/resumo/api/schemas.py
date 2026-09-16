"""Public response contracts for the candidate API."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict


class CandidateSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sq_candidato: str
    nome: str | None
    nome_urna: str | None
    numero: str | None
    foto_url: str | None
    iniciais: str
    nome_normalizado: str | None
    ano: int
    cd_cargo: int
    cargo: str | None
    uf: str | None
    partido: str | None
    situacao: str | None
    incumbent_confirmed: bool
    incumbent_house: str | None
    reelection_same_office: bool
    resultado: str | None
    majoritario: bool | None
    requires_proposta: bool
    history_status: str
    history_note: str | None


class CandidateDetail(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidacy: CandidateSummary
    photo: dict[str, Any] | None
    social_links: list[dict[str, str]]
    proposals: list[dict[str, Any]]
    incumbent_confirmed: bool
    link: dict[str, Any] | None
    track_record: dict[str, Any] | None
    amendments: dict[str, Any] | None
    campaign_finance: dict[str, Any]
    top_donors: list[dict[str, Any]]