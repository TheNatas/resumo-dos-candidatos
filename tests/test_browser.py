"""Browser smoke coverage for the shipped static frontend."""

from __future__ import annotations

import functools
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

import pytest

from resumo.db.models import Candidacy
from resumo.render import render_site

playwright = pytest.importorskip("playwright.sync_api")


def _seed(session):
    session.add_all(
        [
            Candidacy(
                sq_candidato="BROWSER_PT",
                ano_eleicao=2026,
                sg_uf="SC",
                cd_cargo=3,
                ds_cargo="GOVERNADOR",
                nome_candidato="ANA BROWSER",
                nome_urna="ANA",
                nome_normalizado="ANA BROWSER",
                genero="FEMININO",
                sg_partido="PT",
                is_majoritario=True,
            ),
            Candidacy(
                sq_candidato="BROWSER_PSDB",
                ano_eleicao=2026,
                sg_uf="SC",
                cd_cargo=3,
                ds_cargo="GOVERNADOR",
                nome_candidato="BRUNO BROWSER",
                nome_urna="BRUNO",
                nome_normalizado="BRUNO BROWSER",
                genero="MASCULINO",
                sg_partido="PSDB",
                is_majoritario=True,
            ),
        ]
    )
    session.commit()


@pytest.fixture
def static_site(session, tmp_path, monkeypatch):
    _seed(session)
    storage = tmp_path / "storage"
    storage.mkdir()
    monkeypatch.setenv("RESUMO_STORAGE_DIR", str(storage))
    from resumo.config import get_settings

    get_settings.cache_clear()
    try:
        out = tmp_path / "site"
        render_site(session, out=out, base_url="", site_url=None)

        handler = functools.partial(SimpleHTTPRequestHandler, directory=str(out))
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        get_settings.cache_clear()


def test_filters_and_back_navigation_preserve_state(static_site):
    with playwright.sync_playwright() as browser_api:
        browser = browser_api.chromium.launch()
        page = browser.new_page()
        try:
            page.goto(f"{static_site}/", wait_until="networkidle")
            page.locator("[data-filtros-abrir]").click()
            assert page.locator("#filtros").is_visible()

            page.locator("[data-partido-toggle]").click()
            page.locator("[name='partido'][value='PT']").check()
            assert page.locator("#count").inner_text() == "1 candidatura"
            page.locator("[data-filtros-fechar]").last.click()
            assert not page.locator("#filtros").is_visible()
            candidate_link = page.locator(
                "#results .card:not([hidden]) a[href*='/candidato/']"
            )
            assert candidate_link.get_attribute("href") == "/candidato/BROWSER_PT?partido=PT"
            candidate_link.click()
            page.wait_for_load_state("networkidle")

            assert page.url.endswith("/candidato/BROWSER_PT/?partido=PT")
            page.locator("[data-static-back]").click()
            page.wait_for_load_state("networkidle")

            assert page.url.endswith("/?partido=PT")
            assert page.locator("[name='partido'][value='PT']").is_checked()
            assert page.locator("#results .card:not([hidden])").count() == 1
            assert "ANA" in page.locator("#results").inner_text()
        finally:
            browser.close()


def test_multiple_party_filter(static_site):
    with playwright.sync_playwright() as browser_api:
        browser = browser_api.chromium.launch()
        page = browser.new_page()
        try:
            page.goto(f"{static_site}/", wait_until="networkidle")
            page.locator("[data-filtros-abrir]").click()
            page.locator("[data-partido-toggle]").click()
            page.locator("[name='partido'][value='PT']").check()
            page.locator("[name='partido'][value='PSDB']").check()

            assert page.locator("#results .card:not([hidden])").count() == 2
            assert page.locator("[data-partido-resumo]").inner_text() == "2 partidos selecionados"
            assert "partido=PT" in page.url and "partido=PSDB" in page.url
        finally:
            browser.close()


def test_women_filter_shows_only_tse_declared_feminino(static_site):
    with playwright.sync_playwright() as browser_api:
        browser = browser_api.chromium.launch()
        page = browser.new_page()
        try:
            page.goto(f"{static_site}/", wait_until="networkidle")
            page.locator("[data-filtros-abrir]").click()
            page.locator("[name='mulher']").check()

            assert page.locator("#results .card:not([hidden])").count() == 1
            assert "mulher=sim" in page.url
            assert "ANA" in page.locator("#results").inner_text()
            assert "BRUNO" not in page.locator("#results").inner_text()
        finally:
            browser.close()