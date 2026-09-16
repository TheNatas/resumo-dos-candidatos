from resumo.ingestion.tse.rede_social_candidato import _social_row


def test_social_row_accepts_official_tse_fields_and_rejects_non_urls():
    assert _social_row(
        {
            "SQ_CANDIDATO": "250001607903",
            "DS_TIPO_REDE_SOCIAL": "Instagram",
            "DS_URL_REDE_SOCIAL": "https://instagram.com/candidata",
        }
    ) == {
        "sq_candidato": "250001607903",
        "network": "Instagram",
        "url": "https://instagram.com/candidata",
    }
    assert _social_row(
        {
            "SQ_CANDIDATO": "250001607903",
            "DS_TIPO_REDE_SOCIAL": "Instagram",
            "DS_URL_REDE_SOCIAL": "javascript:alert(1)",
        }
    ) is None