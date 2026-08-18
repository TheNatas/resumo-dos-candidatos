"""Central configuration — and the 2022 -> 2026 re-pointing seam.

Every collector reads the election year / legislatura from here, so switching the
whole platform from historical validation (2022/2024) to the live 2026 cycle is a
change of environment variables, never a code change.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="RESUMO_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ── Re-point seam ────────────────────────────────────────────────────────
    election_year: int = Field(default=2026, description="TSE election year (ANO_ELEICAO).")
    cd_eleicao: int | None = Field(
        default=None, description="Optional TSE election code; None = all turns in the year."
    )
    id_legislatura: int = Field(default=57, description="Câmara legislatura (57 = 2023-2027).")

    # ── Geographic / office scope ────────────────────────────────────────────
    # The platform ships scoped to one state so the public surface is auditable
    # end-to-end. Widening is a config change: set RESUMO_TARGET_UFS="" for national.
    target_ufs: str = Field(
        default="SC", description='Comma-separated UFs to ingest; "" = all (national).'
    )
    target_cargos: str = Field(
        default="3,5,6,7",
        description=(
            'Comma-separated TSE CD_CARGO to ingest (3=Governador, 5=Senador, '
            '6=Dep. Federal, 7=Dep. Estadual); "" = all offices.'
        ),
    )

    # ── Infra ────────────────────────────────────────────────────────────────
    database_url: str = "postgresql+psycopg://resumo:resumo@localhost:5439/resumo"
    storage_dir: Path = Path("./data/storage")

    # ── Official source bases (no auth) ──────────────────────────────────────
    tse_cdn_base: str = "https://cdn.tse.jus.br/estatistica/sead/odsele"
    tse_ckan_base: str = "https://dadosabertos.tse.jus.br/api/3/action"
    camara_api_base: str = "https://dadosabertos.camara.leg.br/api/v2"
    senado_api_base: str = "https://legis.senado.leg.br/dadosabertos"

    # ALESC (Assembleia Legislativa de SC) has no API; three distinct hosts back it.
    alesc_site_base: str = "https://www.alesc.sc.gov.br"
    alesc_elegis_base: str = "https://portalelegis.alesc.sc.gov.br"
    alesc_transparencia_base: str = "https://transparencia.alesc.sc.gov.br"
    # 20th legislature = 2023-2027. e-Legis has NO data before Feb 2023.
    alesc_id_legislatura: int = 20

    # Emendas parlamentares: CGU bulk download, no auth and no API key needed
    # (the Portal da Transparência REST API would require a `chave-api-dados`).
    emendas_bulk_url: str = (
        "https://dadosabertos-download.cgu.gov.br/PortalDaTransparencia/saida/"
        "emendas-parlamentares/EmendasParlamentares.zip"
    )

    # ── HTTP etiquette ───────────────────────────────────────────────────────
    http_user_agent: str = (
        "resumo-dos-candidatos/0.1 (transparencia eleitoral; contato@example.org)"
    )
    request_delay_seconds: float = 0.2
    http_timeout_seconds: float = 60.0

    @field_validator("cd_eleicao", mode="before")
    @classmethod
    def _blank_is_none(cls, v):
        """`RESUMO_CD_ELEICAO=` (documented as "leave blank") arrives as "", which is
        not a valid int. Treat blank as unset rather than failing to boot."""
        if isinstance(v, str) and not v.strip():
            return None
        return v

    def storage_path(self) -> Path:
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        return self.storage_dir

    @property
    def uf_list(self) -> tuple[str, ...]:
        """Normalized target UFs; empty tuple means "no filter" (national)."""
        return tuple(u.strip().upper() for u in self.target_ufs.split(",") if u.strip())

    @property
    def cargo_set(self) -> frozenset[int]:
        """Normalized target cargos; empty frozenset means "no filter" (all offices)."""
        from resumo.cargos import parse_cargos

        return parse_cargos(self.target_cargos)


@lru_cache
def get_settings() -> Settings:
    return Settings()
