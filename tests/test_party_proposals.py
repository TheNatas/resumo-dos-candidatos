from resumo.ingestion.party_proposals import discover_pdf_urls


def test_discovery_stays_on_allowlisted_host(respx_mock):
    respx_mock.get("https://party.example/").respond(
        200,
        headers={"content-type": "text/html"},
        text=(
            '<a href="/programa-2026.pdf">Programa 2026</a>'
            '<a href="https://other.example/programa-2026.pdf">fora</a>'
        ),
    )
    respx_mock.get("https://party.example/programa-2026.pdf").respond(
        200, headers={"content-type": "application/pdf"}, content=b"%PDF"
    )

    assert discover_pdf_urls(["https://party.example"], 2026) == [
        "https://party.example/programa-2026.pdf"
    ]