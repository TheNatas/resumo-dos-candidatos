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
            "SG_UF": "SP",
            "SG_UE": "SP",
            "NM_UE": "SÃO PAULO",
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


def make_tse_zip(rows: list[dict], member: str = "consulta_cand_2022_SP.csv") -> bytes:
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


def deputado_detail(member_id: str, cpf: str, nome: str, **status) -> dict:
    s = {
        "nomeEleitoral": nome.split()[0],
        "siglaPartido": "PT",
        "siglaUf": "SP",
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
            "ufNascimento": "SP",
            "ultimoStatus": s,
        }
    }
