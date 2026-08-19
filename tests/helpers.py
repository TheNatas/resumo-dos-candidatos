"""Test fixtures builders (synthetic TSE files, Câmara payloads)."""

from __future__ import annotations

import csv
import io
import zipfile

# Minimal but realistic consulta_cand columns used by the collector.
TSE_COLUMNS = [
    "DT_GERACAO", "ANO_ELEICAO", "NR_TURNO", "CD_ELEICAO", "DS_ELEICAO", "SQ_CANDIDATO",
    "NR_CPF_CANDIDATO", "NR_TITULO_ELEITORAL_CANDIDATO", "NM_CANDIDATO", "NM_URNA_CANDIDATO",
    "DT_NASCIMENTO", "CD_CARGO", "DS_CARGO", "SG_UF", "SG_UE", "NM_UE", "NR_CANDIDATO",
    "SG_PARTIDO", "NR_PARTIDO", "NM_PARTIDO", "SQ_COLIGACAO", "NM_COLIGACAO",
    "DS_COMPOSICAO_COLIGACAO", "DS_SITUACAO_CANDIDATURA", "DS_DETALHE_SITUACAO_CAND",
    "DS_SIT_TOT_TURNO", "ST_REELEICAO",
]


def tse_row(**over) -> dict[str, str]:
    base = {c: "" for c in TSE_COLUMNS}
    base.update(
        {
            "DT_GERACAO": "01/09/2022",
            "ANO_ELEICAO": "2022",
            "NR_TURNO": "1",
            "CD_ELEICAO": "546",
            "DS_ELEICAO": "Eleições Gerais Estaduais 2022",
            "CD_CARGO": "6",
            "DS_CARGO": "DEPUTADO FEDERAL",
            "SG_UF": "SC",
            "SG_UE": "SC",
            "NM_UE": "SANTA CATARINA",
            "SG_PARTIDO": "PT",
            "NR_PARTIDO": "13",
            "NM_PARTIDO": "Partido dos Trabalhadores",
            "DS_SITUACAO_CANDIDATURA": "APTO",
            "DS_SIT_TOT_TURNO": "ELEITO",
            "ST_REELEICAO": "S",
        }
    )
    base.update({k: str(v) for k, v in over.items()})
    return base


def make_tse_zip(rows: list[dict], member: str = "consulta_cand_2022_SC.csv") -> bytes:
    """A national-style zip with one Latin-1, ';'-delimited per-UF CSV."""
    text = io.StringIO()
    writer = csv.DictWriter(text, fieldnames=TSE_COLUMNS, delimiter=";", extrasaction="ignore")
    writer.writeheader()
    for r in rows:
        writer.writerow(r)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(member, text.getvalue().encode("latin-1"))
    return buf.getvalue()


def make_proposta_zip(pdf_name: str, content: bytes = b"%PDF-1.4 fake") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(pdf_name, content)
    return buf.getvalue()


# 1x1 JPEG. Real bytes rather than b"fake": the collector hashes what it stores and
# the renderer copies it verbatim, so a test that never handles a real image would
# not notice either one mangling the file.
TINY_JPEG = bytes.fromhex(
    "ffd8ffe000104a46494600010100000100010000ffdb004300ffffffffffffffffffffffffff"
    "ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff"
    "ffffffffffffffffffffffffffffffffffffc2000b080001000101011100ffc4001400010000"
    "0000000000000000000000000000ffda0008010100000001d2cf20ffd9"
)


def make_foto_zip(members: dict[str, bytes] | None = None, **named: bytes) -> bytes:
    """A foto_cand-style zip: image members keyed by path, no manifest."""
    entries = dict(members or {})
    entries.update(named)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return buf.getvalue()


def deputado_detail(member_id: str, cpf: str, nome: str, **status) -> dict:
    s = {
        "nomeEleitoral": nome.split()[0],
        "siglaPartido": "PT",
        "siglaUf": "SC",
        "idLegislatura": 57,
        "situacao": "Exercício",
        "condicaoEleitoral": "Titular",
        "data": "2023-02-01",
    }
    s.update(status)
    return {
        "dados": {
            "id": int(member_id),
            "nomeCivil": nome,
            "cpf": cpf,
            "dataNascimento": "1970-05-10",
            "ufNascimento": "SC",
            "ultimoStatus": s,
        }
    }


# ── Prestação de contas eleitorais ───────────────────────────────────────────
# Verified column sets (identical between 2022 and 2026 — zero schema drift).
PC_RECEITA_COLUMNS = [
    "SQ_PRESTADOR_CONTAS", "SG_UF", "SG_UE", "CD_CARGO", "DS_CARGO", "SQ_CANDIDATO",
    "NR_CANDIDATO", "NM_CANDIDATO", "ST_TURNO", "TP_PRESTACAO_CONTAS", "DT_PRESTACAO_CONTAS",
    "CD_FONTE_RECEITA", "DS_FONTE_RECEITA", "DS_ORIGEM_RECEITA", "DS_NATUREZA_RECEITA",
    "DS_ESPECIE_RECEITA", "CD_CNAE_DOADOR", "DS_CNAE_DOADOR", "NR_CPF_CNPJ_DOADOR", "NM_DOADOR",
    "NM_DOADOR_RFB", "SG_UF_DOADOR", "NM_MUNICIPIO_DOADOR", "SQ_CANDIDATO_DOADOR",
    "SG_PARTIDO_DOADOR", "SQ_RECEITA", "DT_RECEITA", "DS_RECEITA", "VR_RECEITA", "AA_ELEICAO",
]
PC_CONTRATADA_COLUMNS = [
    "SQ_PRESTADOR_CONTAS", "SG_UF", "SQ_CANDIDATO", "ST_TURNO", "TP_PRESTACAO_CONTAS",
    "CD_TIPO_FORNECEDOR", "DS_TIPO_FORNECEDOR", "CD_CNAE_FORNECEDOR", "DS_CNAE_FORNECEDOR",
    "NR_CPF_CNPJ_FORNECEDOR", "NM_FORNECEDOR", "NM_FORNECEDOR_RFB", "SG_UF_FORNECEDOR",
    "NM_MUNICIPIO_FORNECEDOR", "DS_TIPO_DOCUMENTO", "NR_DOCUMENTO", "CD_ORIGEM_DESPESA",
    "DS_ORIGEM_DESPESA", "SQ_DESPESA", "DT_DESPESA", "DS_DESPESA", "VR_DESPESA_CONTRATADA",
    "AA_ELEICAO",
]
# NB: no SQ_CANDIDATO and no supplier columns — resolves via SQ_PRESTADOR_CONTAS.
PC_PAGA_COLUMNS = [
    "SQ_PRESTADOR_CONTAS", "SG_UF", "DS_TIPO_DOCUMENTO", "NR_DOCUMENTO", "CD_FONTE_DESPESA",
    "DS_FONTE_DESPESA", "CD_ORIGEM_DESPESA", "DS_ORIGEM_DESPESA", "CD_NATUREZA_DESPESA",
    "DS_NATUREZA_DESPESA", "CD_ESPECIE_RECURSO", "DS_ESPECIE_RECURSO", "SQ_DESPESA",
    "SQ_PARCELAMENTO_DESPESA", "DT_PAGTO_DESPESA", "DS_DESPESA", "VR_PAGTO_DESPESA", "ST_TURNO",
    "TP_PRESTACAO_CONTAS", "AA_ELEICAO",
]
PC_ORIGINARIO_COLUMNS = [
    "SQ_PRESTADOR_CONTAS", "SG_UF", "NR_CPF_CNPJ_DOADOR_ORIGINARIO", "NM_DOADOR_ORIGINARIO",
    "NM_DOADOR_ORIGINARIO_RFB", "TP_DOADOR_ORIGINARIO", "CD_CNAE_DOADOR_ORIGINARIO",
    "DS_CNAE_DOADOR_ORIGINARIO", "SQ_RECEITA", "DT_RECEITA", "DS_RECEITA", "VR_RECEITA",
    "AA_ELEICAO",
]


def _pc_row(columns: list[str], defaults: dict, over: dict) -> dict[str, str]:
    base = {c: "" for c in columns}
    base.update(defaults)
    base.update({k: str(v) for k, v in over.items()})
    return base


def pc_receita_row(**over) -> dict[str, str]:
    """A receitas_candidatos row (money is TSE-formatted: '1.234,56')."""
    return _pc_row(
        PC_RECEITA_COLUMNS,
        {
            "SQ_PRESTADOR_CONTAS": "700001",
            "SG_UF": "SC",
            "SG_UE": "SC",
            "CD_CARGO": "6",
            "DS_CARGO": "DEPUTADO FEDERAL",
            "ST_TURNO": "1",
            "TP_PRESTACAO_CONTAS": "FINAL",  # receitas shout; despesas title-case
            "DT_PRESTACAO_CONTAS": "03/11/2022",
            "DS_FONTE_RECEITA": "FUNDO PARTIDÁRIO",
            "DS_ORIGEM_RECEITA": "RECURSOS DE PARTIDO POLÍTICO",
            "DS_NATUREZA_RECEITA": "ORDINÁRIA",
            "DS_ESPECIE_RECEITA": "TRANSFERÊNCIA ELETRÔNICA",
            "NR_CPF_CNPJ_DOADOR": "12345678000199",
            "NM_DOADOR": "DIRETORIO NACIONAL",
            "NM_DOADOR_RFB": "PARTIDO DOS TRABALHADORES",
            "SG_UF_DOADOR": "DF",
            "DT_RECEITA": "15/09/2022",
            "DS_RECEITA": "Doação via transferência eletrônica",
            "VR_RECEITA": "10.000,00",
            "AA_ELEICAO": "2022",
        },
        over,
    )


def pc_despesa_contratada_row(**over) -> dict[str, str]:
    return _pc_row(
        PC_CONTRATADA_COLUMNS,
        {
            "SQ_PRESTADOR_CONTAS": "700001",
            "SG_UF": "SC",
            "ST_TURNO": "1",
            "TP_PRESTACAO_CONTAS": "Final",
            "DS_TIPO_FORNECEDOR": "Pessoa Jurídica",
            "NR_CPF_CNPJ_FORNECEDOR": "99887766000155",
            "NM_FORNECEDOR": "GRAFICA CENTRAL",
            "NM_FORNECEDOR_RFB": "GRAFICA CENTRAL LTDA",
            "SG_UF_FORNECEDOR": "SC",
            "NM_MUNICIPIO_FORNECEDOR": "FLORIANÓPOLIS",
            "DS_TIPO_DOCUMENTO": "Nota Fiscal",
            "NR_DOCUMENTO": "1234",
            "DS_ORIGEM_DESPESA": "Publicidade por materiais impressos",
            "DT_DESPESA": "20/09/2022",
            "DS_DESPESA": "Santinhos",
            "VR_DESPESA_CONTRATADA": "5.000,00",
            "AA_ELEICAO": "2022",
        },
        over,
    )


def pc_despesa_paga_row(**over) -> dict[str, str]:
    return _pc_row(
        PC_PAGA_COLUMNS,
        {
            "SQ_PRESTADOR_CONTAS": "700001",
            "SG_UF": "SC",
            "DS_TIPO_DOCUMENTO": "Nota Fiscal",
            "NR_DOCUMENTO": "1234",
            "DS_FONTE_DESPESA": "Fundo Partidário",
            "DS_ORIGEM_DESPESA": "Publicidade por materiais impressos",
            "DS_NATUREZA_DESPESA": "Ordinária",
            "DS_ESPECIE_RECURSO": "Transferência eletrônica",
            "DT_PAGTO_DESPESA": "25/09/2022",
            "DS_DESPESA": "Santinhos",
            "VR_PAGTO_DESPESA": "2.500,00",
            "ST_TURNO": "1",
            "TP_PRESTACAO_CONTAS": "Final",
            "AA_ELEICAO": "2022",
        },
        over,
    )


def pc_doador_originario_row(**over) -> dict[str, str]:
    return _pc_row(
        PC_ORIGINARIO_COLUMNS,
        {
            "SQ_PRESTADOR_CONTAS": "700001",
            "SG_UF": "SC",
            "NR_CPF_CNPJ_DOADOR_ORIGINARIO": "11122233344",
            "NM_DOADOR_ORIGINARIO": "FULANO DE TAL",
            "NM_DOADOR_ORIGINARIO_RFB": "FULANO DE TAL",
            "TP_DOADOR_ORIGINARIO": "F",
            "DT_RECEITA": "15/09/2022",
            "DS_RECEITA": "Doação originária",
            "VR_RECEITA": "1.000,00",
            "AA_ELEICAO": "2022",
        },
        over,
    )


def _pc_csv(columns: list[str], rows: list[dict]) -> bytes:
    """Latin-1, ';'-delimited, quoted, CRLF — with the header present even at 0 rows."""
    text = io.StringIO()
    writer = csv.DictWriter(
        text,
        fieldnames=columns,
        delimiter=";",
        quotechar='"',
        quoting=csv.QUOTE_ALL,
        lineterminator="\r\n",
        extrasaction="ignore",
    )
    writer.writeheader()
    for r in rows:
        writer.writerow(r)
    return text.getvalue().encode("latin-1")


def make_prestacao_contas_zip(
    *,
    receitas: list[dict] | None = None,
    contratadas: list[dict] | None = None,
    pagas: list[dict] | None = None,
    originarios: list[dict] | None = None,
    year: int = 2022,
    uf: str = "SC",
) -> bytes:
    """A prestação de contas zip: the 4 CSV families for one UF, plus a leiame PDF.

    Every family is written even when empty, mirroring TSE: header-only files are
    normal (the whole 2026 zip was header-only as of Aug/2026).
    """
    families = [
        (f"receitas_candidatos_{year}_{uf}.csv", PC_RECEITA_COLUMNS, receitas or []),
        (
            f"receitas_candidatos_doador_originario_{year}_{uf}.csv",
            PC_ORIGINARIO_COLUMNS,
            originarios or [],
        ),
        (
            f"despesas_contratadas_candidatos_{year}_{uf}.csv",
            PC_CONTRATADA_COLUMNS,
            contratadas or [],
        ),
        (f"despesas_pagas_candidatos_{year}_{uf}.csv", PC_PAGA_COLUMNS, pagas or []),
    ]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for member, columns, rows in families:
            zf.writestr(member, _pc_csv(columns, rows))
        zf.writestr(f"leiame_receitas_candidatos_{year}.pdf", b"%PDF-1.4 fake")
    return buf.getvalue()
