"""Static renderer: the published site must carry exactly the scoped surface, and
nothing the live API would refuse to hand out."""

from __future__ import annotations

import json

import pytest
from tests.helpers import TINY_JPEG

from resumo.config import get_settings
from resumo.db.models import Candidacy, CandidatePhoto, GovernmentProposal
from resumo.render import render_site


@pytest.fixture
def _storage(tmp_path, monkeypatch):
    """Point the storage root at a temp dir; settings are cached, so clear it."""
    root = tmp_path / "storage"
    root.mkdir()
    monkeypatch.setenv("RESUMO_STORAGE_DIR", str(root))
    get_settings.cache_clear()
    yield root
    get_settings.cache_clear()


def _seed(session):
    session.add_all(
        [
            Candidacy(
                sq_candidato="C1", ano_eleicao=2026, sg_uf="SC", cd_cargo=3,
                ds_cargo="GOVERNADOR", nome_candidato="MARIA DA SILVA",
                nome_urna="MARIA", nome_normalizado="MARIA DA SILVA",
                sg_partido="PT", is_majoritario=True,
            ),
            # Historical validation row: in the same table, out of the published scope.
            Candidacy(
                sq_candidato="OLD1", ano_eleicao=2022, sg_uf="SC", cd_cargo=3,
                ds_cargo="GOVERNADOR", nome_candidato="JOAO ANTIGO",
                nome_normalizado="JOAO ANTIGO", sg_partido="PL", is_majoritario=True,
            ),
            # Out-of-scope UF: config says SC.
            Candidacy(
                sq_candidato="RS1", ano_eleicao=2026, sg_uf="RS", cd_cargo=3,
                ds_cargo="GOVERNADOR", nome_candidato="PEDRO GAUCHO",
                nome_normalizado="PEDRO GAUCHO", sg_partido="PSOL", is_majoritario=True,
            ),
        ]
    )
    session.commit()


def test_renders_only_the_scoped_surface(session, tmp_path, _storage):
    _seed(session)
    out = tmp_path / "site"

    result = render_site(session, out=out, base_url="", site_url=None)

    assert result.pages == 1
    assert (out / "candidato" / "C1" / "index.html").is_file()
    # Neither the other election year nor the other UF may be published.
    assert not (out / "candidato" / "OLD1").exists()
    assert not (out / "candidato" / "RS1").exists()

    index = json.loads((out / "api" / "candidates.json").read_text(encoding="utf-8"))
    assert [c["sq_candidato"] for c in index] == ["C1"]

    page = (out / "index.html").read_text(encoding="utf-8")
    assert "MARIA" in page
    assert "JOAO ANTIGO" not in page
    # The published page carries no third-party script.
    assert "htmx" not in page
    # Pages would otherwise run Jekyll and drop underscore paths.
    assert (out / ".nojekyll").is_file()


def test_partido_options_come_from_the_rendered_rows(session, tmp_path, _storage):
    _seed(session)
    out = tmp_path / "site"

    render_site(session, out=out, base_url="", site_url=None)

    page = (out / "index.html").read_text(encoding="utf-8")
    assert '<option value="PT">' in page
    # A party that only exists outside the published scope would filter to nothing.
    assert 'value="PL"' not in page
    assert 'value="PSOL"' not in page


def test_base_url_prefixes_every_internal_link(session, tmp_path, _storage):
    _seed(session)
    out = tmp_path / "site"

    render_site(session, out=out, base_url="/resumo-dos-candidatos", site_url=None)

    page = (out / "index.html").read_text(encoding="utf-8")
    assert 'href="/resumo-dos-candidatos/static/style.css"' in page
    assert 'href="/resumo-dos-candidatos/candidato/C1"' in page
    # No internal link may escape the prefix, or it 404s on a Pages project site.
    assert 'href="/static' not in page
    assert 'href="/candidato' not in page


def test_proposal_pdf_is_copied_and_path_stays_private(session, tmp_path, _storage):
    _seed(session)
    pdf = _storage / "proposta" / "2026" / "p.pdf"
    pdf.parent.mkdir(parents=True)
    pdf.write_bytes(b"%PDF-1.4 fake")
    proposal = GovernmentProposal(
        sq_candidato="C1", source="tse_bulk_pdf", storage_path=str(pdf),
        original_filename="p.pdf", content_hash="abc",
    )
    session.add(proposal)
    session.commit()
    out = tmp_path / "site"

    result = render_site(session, out=out, base_url="", site_url=None)

    assert result.proposals == 1
    assert (out / "proposta" / f"{proposal.id}.pdf").read_bytes() == b"%PDF-1.4 fake"
    detail = json.loads(
        (out / "api" / "candidates" / "C1.json").read_text(encoding="utf-8")
    )
    # The build-time hint must never survive into the published payload.
    assert "_storage_path" not in detail["proposals"][0]
    assert "storage_path" not in detail["proposals"][0]
    assert f"/proposta/{proposal.id}.pdf" in (
        out / "candidato" / "C1" / "index.html"
    ).read_text(encoding="utf-8")


def test_proposal_outside_storage_root_is_not_copied(session, tmp_path, _storage):
    """A row pointing outside the storage root must not pull an arbitrary file into
    a public site — the same confinement the live route applies."""
    _seed(session)
    outside = tmp_path / "secret.pdf"
    outside.write_bytes(b"%PDF-1.4 secret")
    session.add(
        GovernmentProposal(
            sq_candidato="C1", source="tse_bulk_pdf", storage_path=str(outside),
            original_filename="secret.pdf", content_hash="def",
        )
    )
    session.commit()
    out = tmp_path / "site"

    result = render_site(session, out=out, base_url="", site_url=None)

    assert result.proposals == 0
    assert not (out / "proposta").exists()


def test_refuses_to_clean_a_directory_it_did_not_write(session, tmp_path, _storage):
    """--out is a path typed by a human or interpolated in CI; deleting whatever it
    points at is not an acceptable default."""
    _seed(session)
    out = tmp_path / "meus-documentos"
    out.mkdir()
    (out / "importante.txt").write_text("não apague", encoding="utf-8")

    with pytest.raises(ValueError, match="não parece um site renderizado"):
        render_site(session, out=out, base_url="", site_url=None)

    assert (out / "importante.txt").is_file()


def test_sitemap_and_robots_only_with_an_absolute_url(session, tmp_path, _storage):
    _seed(session)
    out = tmp_path / "site"

    render_site(session, out=out, base_url="", site_url="https://exemplo.org/")

    sitemap = (out / "sitemap.xml").read_text(encoding="utf-8")
    assert "<loc>https://exemplo.org/</loc>" in sitemap
    assert "<loc>https://exemplo.org/candidato/C1/</loc>" in sitemap
    assert "Sitemap: https://exemplo.org/sitemap.xml" in (
        out / "robots.txt"
    ).read_text(encoding="utf-8")


def test_publishes_the_caveats_page(session, tmp_path, _storage):
    """The source caveats qualify every number on the site, so they ship with it —
    a reader looking at "12 votos" has to be one click from learning that 96% of that
    House's votes are simbólicas."""
    _seed(session)
    out = tmp_path / "site"

    render_site(session, out=out, base_url="", site_url="https://exemplo.org")

    sobre = (out / "sobre" / "index.html").read_text(encoding="utf-8")
    assert "votação simbólica" in sobre
    assert "57% das votações" in sobre
    assert "exibido nem publicado" in sobre and "LGPD" in sobre
    # E toda página aponta para ela.
    assert 'href="/sobre/"' in (out / "index.html").read_text(encoding="utf-8")
    assert 'href="/sobre/"' in (
        out / "candidato" / "C1" / "index.html"
    ).read_text(encoding="utf-8")
    assert "<loc>https://exemplo.org/sobre/</loc>" in (
        out / "sitemap.xml"
    ).read_text(encoding="utf-8")


def _seed_photo(session, storage, sq="C1", data=TINY_JPEG) -> CandidatePhoto:
    jpg = storage / "foto" / "2026" / f"{sq}_abc.jpg"
    jpg.parent.mkdir(parents=True, exist_ok=True)
    jpg.write_bytes(data)
    photo = CandidatePhoto(
        sq_candidato=sq, source="tse_bulk_foto", storage_path=str(jpg),
        original_filename=f"FSC{sq}_div.jpg", media_type="image/jpeg", content_hash="abc",
    )
    session.add(photo)
    session.commit()
    return photo


def test_photo_is_published_and_path_stays_private(session, tmp_path, _storage):
    _seed(session)
    _seed_photo(session, _storage)
    out = tmp_path / "site"

    result = render_site(session, out=out, base_url="", site_url=None)

    assert result.photos == 1
    assert (out / "foto" / "C1.jpg").read_bytes() == TINY_JPEG
    detail = json.loads((out / "api" / "candidates" / "C1.json").read_text(encoding="utf-8"))
    # The build-time hint must never survive into the published payload.
    assert "_storage_path" not in detail["photo"]
    assert "storage_path" not in detail["photo"]
    assert detail["photo"]["url"] == "/foto/C1.jpg"
    ficha = (out / "candidato" / "C1" / "index.html").read_text(encoding="utf-8")
    assert 'src="/foto/C1.jpg"' in ficha
    # Creditada onde o leitor a vê, não só no JSON.
    assert "Foto oficial de registro" in ficha
    # E o card na home aponta para o mesmo arquivo.
    assert 'src="/foto/C1.jpg"' in (out / "index.html").read_text(encoding="utf-8")


def test_photo_outside_storage_root_is_neither_copied_nor_linked(session, tmp_path, _storage):
    """A row pointing outside the storage root must not pull an arbitrary file into a
    public site — and, having refused to publish it, the build must not leave a URL
    behind either: a broken frame where a face should be reads as a fact about the
    candidate rather than about the build."""
    _seed(session)
    outside = tmp_path / "secret.jpg"
    outside.write_bytes(b"segredo")
    session.add(
        CandidatePhoto(
            sq_candidato="C1", source="tse_bulk_foto", storage_path=str(outside),
            original_filename="secret.jpg", media_type="image/jpeg", content_hash="def",
        )
    )
    session.commit()
    out = tmp_path / "site"

    result = render_site(session, out=out, base_url="", site_url=None)

    assert result.photos == 0
    assert not (out / "foto").exists()
    detail = json.loads((out / "api" / "candidates" / "C1.json").read_text(encoding="utf-8"))
    assert detail["photo"] is None
    assert detail["candidacy"]["foto_url"] is None
    index = json.loads((out / "api" / "candidates.json").read_text(encoding="utf-8"))
    assert index[0]["foto_url"] is None
    assert "/foto/C1.jpg" not in (out / "index.html").read_text(encoding="utf-8")


def test_candidacy_without_a_photo_gets_initials_not_a_broken_image(session, tmp_path, _storage):
    """TSE does not publish a photo for every candidacy. The page must say so with
    initials — never with an image borrowed from anywhere else, and never with an
    empty <img> that renders as a broken file icon under someone's name."""
    _seed(session)
    out = tmp_path / "site"

    result = render_site(session, out=out, base_url="", site_url=None)

    assert result.photos == 0
    ficha = (out / "candidato" / "C1" / "index.html").read_text(encoding="utf-8")
    assert "<img" not in ficha
    assert "foto-vazia" in ficha and ">MS<" in ficha  # MARIA DA SILVA -> MS
    assert "O TSE não publicou foto de registro" in ficha


def test_photo_base_url_is_prefixed(session, tmp_path, _storage):
    _seed(session)
    _seed_photo(session, _storage)
    out = tmp_path / "site"

    render_site(session, out=out, base_url="/resumo-dos-candidatos", site_url=None)

    ficha = (out / "candidato" / "C1" / "index.html").read_text(encoding="utf-8")
    assert 'src="/resumo-dos-candidatos/foto/C1.jpg"' in ficha
    assert 'src="/foto/' not in ficha
