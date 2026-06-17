# Resumo dos Candidatos

Plataforma pública de transparência eleitoral para as **Eleições Gerais 2026**. Para
cada candidato, agrega:

- **Propostas** da candidatura (começando pela *proposta de governo* oficial do TSE).
- **Histórico de atuação pública** (votos, proposições, presença, gastos CEAP) —
  **apenas para incumbentes em reeleição** que já exercem mandato.

Tudo construído sobre **dados oficiais, abertos e gratuitos** (TSE + Câmara dos
Deputados), sem dependências de nuvem.

> **Status.** A base de candidatos 2026 só existe após o registro (prazo **15/08/2026**).
> O sistema é construído e **validado agora com dados históricos de 2022/2024** (mesmos
> esquemas) e **re-apontado para 2026 por configuração** (`RESUMO_ELECTION_YEAR`), sem
> reescrita. Cobertura atual: **Câmara (deputados federais)**. Senado e assembleias
> estaduais são fases posteriores.

---

## Arquitetura

```
[Coletores CLI idempotentes]  ─►  [Postgres normalizado]  ─►  [FastAPI + front Jinja/htmx]
 cron-friendly, ledger de            vínculo candidato↔mandato     busca + ficha pública
 proveniência (RawIngestion)         como ARESTA CENTRAL           (histórico só p/ incumbente)
        │
        └─ fontes oficiais: TSE CDN/CKAN (CSV/PDF em lote) + Câmara API v2 (JSON)
```

A peça central é **`CandidateMandateLink`** — uma aresta materializada e auditável
(*"esta candidatura 2026 é a mesma pessoa que exerce este mandato"*) com método de
match, confiança e proveniência. O front só exibe histórico quando essa aresta confirma
reeleição de incumbente em um nível de confiança aceito; caso contrário mostra
*"incumbência não confirmada"* — nunca um vínculo adivinhado.

**Stack:** Python 3.11+ · SQLAlchemy 2 + Alembic · Postgres (`pg_trgm`/`unaccent`) ·
httpx · polars/csv · rapidfuzz (Splink opcional) · FastAPI · Typer · uv.

---

## Quickstart

Pré-requisitos: **uv**, **Docker** (Postgres).

```bash
uv sync --extra dev                 # instala dependências (+ extras de teste)
cp .env.example .env                # ajuste se quiser; padrões já funcionam
docker compose up -d                # Postgres em localhost:5435
uv run resumo db-upgrade            # aplica as migrações (cria o schema)
```

Coletar, resolver e servir (validação com dados de 2022/2024):

```bash
# 1. Câmara — mandatos + identidade (CPF vem do detalhe). Use --limit para amostrar.
uv run resumo collect camara-deputados --legislatura 57 --limit 20
uv run resumo collect camara-despesas  --anos 2024 --limit 20
uv run resumo collect camara-proposicoes --limit 20
uv run resumo collect camara-votacoes  --inicio 2024-03-01 --fim 2024-03-31
uv run resumo collect camara-eventos   --inicio 2024-03-01 --fim 2024-03-31

# 2. TSE — candidaturas + proposta de governo (baixa em lote do CDN; ou --source arquivo local)
uv run resumo collect tse-candidates --year 2022
uv run resumo collect tse-assets     --year 2022
uv run resumo collect tse-proposta   --year 2022 --uf SP

# 3. Resolução — materializa o vínculo candidato↔mandato
uv run resumo resolve --year 2022

# 4. Front público
uv run resumo serve                 # http://127.0.0.1:8000
```

> Os coletores são **idempotentes**: re-rodar com a mesma fonte é um *no-op* quando o
> hash do artefato não mudou (ver `RawIngestion`). Bons para `cron`.

### Re-apontar para 2026

Quando o registro abrir (ago/2026), troque a configuração — **sem mudar código**:

```bash
# no .env
RESUMO_ELECTION_YEAR=2026
```

Os coletores TSE passam a baixar `consulta_cand_2026.zip` etc.; a legislatura da Câmara
(57 = 2023–2027) já cobre os incumbentes que podem se recandidatar.

---

## Modelo de dados (resumo)

`Person` (identidade canônica) · `Candidacy` (TSE) · `Mandate` (Câmara/Senado) ·
**`CandidateMandateLink`** (aresta central) · `GovernmentProposal` · `Vote` ·
`Proposition` · `AttendanceRecord` · `Expense` · `CandidateAsset` · `Coalition` ·
`ReviewQueue` · `RawIngestion` (proveniência/idempotência). Definições em
[src/resumo/db/models.py](src/resumo/db/models.py).

## Resolução de identidade

Determinística primeiro (CPF exato é o caminho dominante, pois TSE 2022 e o detalhe da
Câmara expõem CPF), com fallback probabilístico (rapidfuzz; Splink opcional via
`uv sync --extra resolution`). Níveis de confiança:

- **auto_strong / auto_weak** → vínculo materializado e exibido publicamente.
- **review** → vai para a `ReviewQueue` (homônimos, score baixo) e **não** aparece no
  front até decisão manual:

```bash
uv run resumo review list
uv run resumo review decide <review_id> match   # match | no_match | uncertain
uv run resumo resolve                           # aplica os overrides (autoritativos)
```

## Testes

```bash
docker compose up -d        # Postgres é necessário (usa o banco resumo_test)
uv run pytest -q
```

Cobre parsing TSE Latin-1 + idempotência, paginação da Câmara (mock respx),
regras de resolução (CPF exato, homônimo→revisão, sem-match) e a API (gating do
histórico). Sem chamadas a APIs reais no CI.

## Conformidade

Dados públicos (LAI) — republicar é legítimo; proveniência registrada. Histórico nunca
é vinculado por palpite (limite de confiança publicado, mecanismo de correção). CPF é
usado para o match mas **não** exibido na UI pública (LGPD). Metodologia de qualquer
métrica derivada (ex.: faltas) é documentada e rotulada como derivada.

## Roadmap (pós-MVP)

S5 Senado + assembleias estaduais · S6 extração/estruturação do texto das propostas ·
S7 camada de avaliação/rankings (presença, fidelidade partidária, anomalias CEAP).
