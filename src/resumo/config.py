"""Central configuration — and the 2022 -> 2026 re-pointing seam.

Every collector reads the election year / legislatura from here, so switching the
whole platform from historical validation (2022/2024) to the live 2026 cycle is a
change of environment variables, never a code change.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="RESUMO_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ── Re-point seam ────────────────────────────────────────────────────────
    election_year: int = Field(default=2022, description="TSE election year (ANO_ELEICAO).")
    cd_eleicao: int | None = Field(
        default=None, description="Optional TSE election code; None = all turns in the year."
    )
    id_legislatura: int = Field(default=57, description="Câmara legislatura (57 = 2023-2027).")

    # ── Infra ────────────────────────────────────────────────────────────────
    database_url: str = "postgresql+psycopg://resumo:resumo@localhost:5435/resumo"
    storage_dir: Path = Path("./data/storage")

    # ── Official source bases (no auth) ──────────────────────────────────────
    tse_cdn_base: str = "https://cdn.tse.jus.br/estatistica/sead/odsele"
    tse_ckan_base: str = "https://dadosabertos.tse.jus.br/api/3/action"
    camara_api_base: str = "https://dadosabertos.camara.leg.br/api/v2"

    # ── HTTP etiquette ───────────────────────────────────────────────────────
    http_user_agent: str = (
        "resumo-dos-candidatos/0.1 (transparencia eleitoral; contato@example.org)"
    )
    request_delay_seconds: float = 0.2
    http_timeout_seconds: float = 60.0

    def storage_path(self) -> Path:
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        return self.storage_dir


@lru_cache
def get_settings() -> Settings:
    return Settings()
