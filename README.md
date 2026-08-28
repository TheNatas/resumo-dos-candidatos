# Resumo dos Candidatos

Plataforma pública de transparência eleitoral para as **Eleições Gerais 2026 em Santa
Catarina**. Para cada candidato, agrega:

- **Foto oficial de registro** da candidatura (a mesma do DivulgaCandContas).
- **Propostas** da candidatura (*proposta de governo* oficial do TSE, quando o cargo exige).
- **Bens declarados** e **financiamento de campanha** (receitas, despesas, doadores).
- **Histórico de atuação pública** (votos, proposições, presença, gastos de gabinete,
  emendas parlamentares) — **apenas para quem já exerce mandato**.

Tudo sobre **dados oficiais, abertos e gratuitos**, sem chaves de API e sem nuvem.

---

## Escopo

Deliberadamente estreito, para que a superfície pública possa ser auditada de ponta a
ponta. Escopo é **configuração**, não código (`RESUMO_TARGET_UFS`, `RESUMO_TARGET_CARGOS`):

```bash
uv run resumo scope     # imprime o escopo efetivo
```

| Cargo (CD_CARGO) | Candidatura, bens, contas | Histórico de atuação |
|---|---|---|
| **Governador** (3) | ✅ + proposta de governo | ⚠️ **parcial** — atos perante a ALESC (ver ressalva) |
| **Senador** (5) | ✅ (sem proposta — ver abaixo) | ✅ Senado Federal |
| **Deputado federal** (6) | ✅ | ✅ Câmara dos Deputados |
| **Deputado estadual** (7) | ✅ | ⚠️ **parcial** — ALESC (ver ressalva) |

> **Majoritário ≠ tem proposta de governo.** Senador é eleito pelo sistema majoritário
> mas **não** entrega proposta de governo — essa obrigação é só dos cargos *executivos*
> (presidente, governador, prefeito; Lei 9.504/97, art. 11 §1º XI). O código trata os
> dois predicados separadamente (`cargos.is_majoritario` vs `cargos.requires_proposta`).

Ampliar é trocar variáveis de ambiente: `RESUMO_TARGET_UFS=""` roda nacional,
`RESUMO_TARGET_CARGOS=""` inclui todos os cargos.

---

## Fontes

| Fonte | O que traz | Auth |
|---|---|---|
| **TSE — CDN/CKAN** (`consulta_cand`, `bem_candidato`, `proposta_governo`) | candidaturas, bens, propostas | — |
| **TSE — `foto_cand`** | fotos oficiais de registro | — |
| **TSE — `prestacao_contas`** | receitas, despesas contratadas, pagamentos, doadores originários | — |
| **Câmara dos Deputados API v2** | mandatos, votações, proposições, eventos, CEAP | — |
| **Câmara — portal (`camara.leg.br`)** | **frequência oficial em plenário** (dias e sessões, com faltas justificadas e não justificadas) | — |
| **Senado Federal — dados abertos** | mandatos, votações, proposições, CEAPS, licenças | — |
| **ALESC** (e-Legis + portal da transparência) | mandatos, votações nominais, proposições, presença, gastos de gabinete | — |
| **ALESC — e-Legis `iniciativa=governador-do-estado`** | projetos de iniciativa do Executivo e mensagens de veto do governador | — |
| **CGU — `EmendasParlamentares.zip`** | emendas parlamentares individuais (empenhado/liquidado/pago) | — |

**Nenhuma fonte exige chave de API.** Para emendas isso foi uma escolha: a API do
Portal da Transparência exigiria `chave-api-dados`, devolve 15 linhas por página e não
filtra por UF — enquanto o arquivo em lote da CGU é *mais rico* (traz município, UF,
programa e ação, que a API não devolve).

### Ressalvas de fonte que a interface exibe explicitamente

- **ALESC — cobertura parcial.** Gastos, proposições e presença são completos, mas
  **~96% das matérias são decididas em votação simbólica**, que por definição não
  registra a posição individual de cada deputado. Restam ~200–250 votos nominais em
  toda a legislatura, e o e-Legis não tem nada anterior a **fev/2023**. Números de
  votação **não são comparáveis** com os de deputados federais.
- **Governador — histórico existe, mas é uma fatia do cargo.** Um cargo executivo não
  tem votação nominal, presença em plenário nem cota de gabinete, e a ficha **não
  desenha** esses contadores em vez de imprimir zeros (um "0 votos nominais" para um
  governador afirmaria que ele faltou a tudo, e o leitor não teria como distinguir
  isso de uma falha nossa). O que existe são os atos que ele assina perante a
  Assembleia — projetos de iniciativa do Executivo e **mensagens de veto**, 510 no
  mandato 2023-2026. Execução orçamentária, decretos, nomeações e programas de governo
  ficam **fora**, e a interface diz isso onde o número aparece.
- **O `ano` do e-Legis é ignorado na consulta do Executivo.** O coletor dos deputados
  particiona o crawl com `?ano=`, e ali funciona; já
  `?iniciativa=governador-do-estado&ano=2024` devolve as mesmas 10.552 linhas que
  `&ano=2026` — todos os governos que o e-Legis guarda. A janela do mandato precisa ser
  expressa em `inicio`/`fim`, e em **ISO**: `inicio=01/01/2023` não é só ignorado, ele
  renderiza uma página vazia sem total nenhum. Ambos verificados na fonte.
- **`iniciativa=governador-do-estado` é o CARGO, não a pessoa.** O e-Legis não sabe
  quem o ocupava. A atribuição é por **data**: cada ato vai para o mandato cuja janela
  contém sua entrada, e um ato fora de toda janela conhecida é descartado e contado —
  nunca pendurado no governador mais próximo. Além disso, cada card precisa dizer
  "Governador do Estado" em *Autoria* para ser aceito.
- **Senado — 57% das votações da legislatura 57 são secretas.** Nelas o voto individual
  não é publicado (o campo vem como `"Votou"`); só os totais agregados existem. Por
  isso um senador tem *ordens de grandeza* menos votos nominais que um deputado
  federal, sem que isso diga nada sobre ele.
- **ALESC — dois dos três CSVs do portal são aproveitáveis.**
  `gabinetes-parlamentares` e `diarias` identificam o deputado;
  `despesas/csv` é o empenho da instituição e **não tem coluna de deputado alguma**,
  então não é ingerido (fica registrado em `UNSUPPORTED_DATASETS` com o motivo, em vez
  de ser atribuído por adivinhação).
- **ALESC — o `iniciativa` do e-Legis é um vocabulário próprio, e o filtro falha em
  silêncio.** `Mandate.house_member_id` guarda o slug do perfil no WordPress
  (`profa-vanessa-da-rosa`); o e-Legis quer o dele (`vanessa-da-rosa`), e os dois
  divergem para ~1 deputado em 9. O problema não é a divergência: é que
  `?iniciativa=<valor-desconhecido>` devolve **exatamente a mesma coisa que não mandar
  filtro nenhum** — a Casa inteira. Sem defesa, as 538 PLs de 2026 de quarenta autores
  viravam autoria de uma deputada só. O valor é resolvido antes da coleta, contra o
  `<select>` que o próprio e-Legis publica (igualdade de slug, depois nome único), e
  quem não resolve **não é coletado**; além disso, toda resposta idêntica à consulta
  sem filtro é recusada em tempo de execução. Ver `resolve_iniciativa`.
- **Gasto de gabinete da ALESC não é CEAP.** CEAP é o nome da cota **da Câmara**; o do
  Senado é CEAPS; e as linhas da ALESC são verba de gabinete somada a diárias. Três
  regimes, três tetos, e a ficha usa o nome de cada Casa (`House.expense_label`).
- **Nenhuma fonte aberta ingerida publica teto de gasto.** O total exibido é o que foi
  reembolsado, nunca uma fração de uma cota — a Câmara divulga a cota mensal por UF
  fora da API, e o portal da ALESC não divulga limite para estas rubricas. Como régua
  a ficha usa a **mediana da própria Casa na mesma janela**, que sai do dado já
  coletado, e mostra ao lado do total os **anos efetivamente cobertos**: sem isso o
  leitor assume o mandato inteiro.
- **Nem toda linha de despesa da ALESC casa com um deputado.** O portal usa o nome
  civil (`CARLOS HENRIQUE LIMA`) e o e-Legis o nome parlamentar (`Sargento Lima`).
  Quando o casamento não é exato, a linha fica **sem atribuição e é registrada em log**
  — pôr o gasto de um deputado na ficha de outro seria pior que não exibi-lo.
- **Emendas — não existe "valor autorizado" na fonte.** O empenhado é o melhor
  indicador disponível e é rotulado como tal. Só emendas **individuais** são
  atribuíveis a um parlamentar (bancada, comissão e relator são coletivas).
- **Proposta de governo — nem todo PDF é da candidatura.** O zip do TSE só diz sob
  qual candidato o arquivo foi entregue, nunca se o texto é o programa do partido. O
  que dá para derivar sem ler o PDF é a repetição: o **mesmo arquivo** (hash idêntico)
  entregue por duas candidaturas da mesma eleição não é específico de nenhuma delas —
  a ficha marca esses casos como *Documento do partido* (quando todas as candidaturas
  são do mesmo partido) ou *Documento compartilhado*, **antes** do link.
- **A foto é a do registro, não a da campanha.** É a imagem entregue à Justiça
  Eleitoral no pedido de registro — a mesma que o DivulgaCandContas exibe —, e costuma
  ser bem menos favorecedora que a do material de campanha; a ficha diz isso ao lado
  dela. Quando o TSE não publica foto para uma candidatura, a página mostra as
  iniciais: **nenhuma imagem vem de outra fonte**, nem de redes sociais nem da
  imprensa. As fotos são o **único produto de candidatura publicado fora de
  `odsele`** (ficam em `eleicoes/eleicoes<ano>/fotos/`), e esse caminho já mudou entre
  ciclos — por isso a URL inteira é configuração (`RESUMO_TSE_FOTOS_URL_TEMPLATE`),
  com o catálogo CKAN consultado antes do template.
- **Frequência: cada Casa publica numa régua diferente, e a ficha usa a régua da
  fonte.** Nada é convertido — converter exigiria afirmar o que a fonte não diz.
  - **Câmara — dias E sessões, oficiais.** O relatório de presença em plenário
    (Ato da Mesa nº 191/2017) publica as duas réguas já contadas, separando ausência
    justificada de não justificada, restrito ao período de exercício do parlamentar.
    As duas **não batem de propósito**: quem faltou a uma sessão do dia e compareceu
    a outra conta como *dia* com presença. A API de dados abertos não serve para isso
    — `/eventos/{id}/deputados` devolve **apenas quem compareceu**.
  - **Senado — sessões, derivada.** Não existe serviço de presença nos dados abertos;
    a contagem sai dos códigos de comparecimento das votações nominais e cobre só as
    sessões em que houve votação nominal. Onde a fonte diz apenas "não compareceu", a
    ausência fica **sem classificação** — ela não afirma que foi injustificada.
  - **ALESC — sessões, observada.** A folha de presença é publicada por sessão, mas
    **toda** ausência vem rotulada como justificada e sem motivo: com esta fonte não
    há como afirmar falta injustificada.
  - **Licença não é falta.** Licenças e afastamentos do Senado são medidos em dias
    corridos de calendário e ficam ao lado da presença, nunca somados a ela.
- **O denominador é por parlamentar, não por ano.** Quem se licencia tem menos sessões
  no período de exercício, então a ficha mostra o percentual — um ranking por número
  absoluto de faltas é enganoso.

---

## Arquitetura

```
[Coletores CLI idempotentes]  ─►  [Postgres normalizado]  ─►  [FastAPI + front Jinja/htmx]
 cron-friendly, ledger de            vínculo candidato↔mandato     busca + ficha pública
 proveniência (RawIngestion)         como ARESTA CENTRAL           (histórico só p/ mandato)
```

A peça central é **`CandidateMandateLink`** — uma aresta materializada e auditável
(*"esta candidatura 2026 é a mesma pessoa que exerce este mandato"*) com método de
match, confiança e proveniência.

O vínculo é **entre casas**, de propósito: um deputado federal concorrendo ao Senado é
o caso normal, e o histórico que interessa ao leitor é o da Câmara. A aresta significa
*"tem mandato"*, não *"disputa a mesma cadeira"*.

**Stack:** Python 3.11+ · SQLAlchemy 2 + Alembic · Postgres (`pg_trgm`/`unaccent`) ·
httpx · rapidfuzz (Splink opcional) · FastAPI · Typer · uv.

O front tem duas saídas a partir das **mesmas** queries e dos **mesmos** templates:
`serve` (FastAPI, uma query por visitante) e `render` (site estático, uma query por
build). Nada de dado é reescrito para arquivo — muda só *quando* a query roda.

---

## Quickstart

Pré-requisitos: **uv**, **Docker**.

```bash
uv sync --extra dev
cp .env.example .env
docker compose up -d                # Postgres em localhost:5439
uv run resumo db-upgrade
uv run resumo scope                 # confirme o escopo antes de coletar
```

```bash
# 1. TSE — candidaturas, bens, fotos, propostas, contas de campanha
uv run resumo collect tse-candidates
uv run resumo collect tse-assets
uv run resumo collect tse-proposta          # sem --uf: usa as UFs do escopo
uv run resumo collect tse-fotos             # idem — um pacote por UF
uv run resumo collect tse-contas            # vazio até set/2026 — ver calendário

# 2. Mandatos SEMPRE primeiro: os demais coletores só guardam linhas de quem
#    já tem mandato em base, e a ponte das emendas casa pelo nome do mandato.
uv run resumo collect camara-deputados
uv run resumo collect senado-senadores

# 3. Histórico de atuação
uv run resumo collect camara-despesas    --anos 2023,2024,2025,2026
uv run resumo collect camara-proposicoes
uv run resumo collect camara-votacoes    --inicio 2026-01-01 --fim 2026-08-18
uv run resumo collect camara-eventos     --inicio 2026-01-01 --fim 2026-08-18
uv run resumo collect camara-presenca                 # frequência OFICIAL (dias+sessões)
uv run resumo collect senado-despesas    --anos 2023,2024,2025,2026
uv run resumo collect senado-proposicoes
uv run resumo collect senado-votacoes    --inicio 2026-01-01 --fim 2026-08-18
uv run resumo collect senado-licencas                 # licenças formais, em dias
uv run resumo collect alesc-deputados
uv run resumo collect alesc-despesas     --anos 2025,2026
uv run resumo collect alesc-votacoes     --inicio 2026-01-01 --fim 2026-08-18
uv run resumo collect alesc-presenca     --inicio 2026-01-01 --fim 2026-08-18
uv run resumo collect alesc-proposicoes  --anos 2026

# 3a. Executivo — o mandato do governador em exercício e os atos que ele assinou.
#     `executivo-governadores` NÃO usa rede: deriva o mandato do resultado do TSE
#     (DS_SIT_TOT_TURNO=ELEITO em 2022) que o passo 1 já trouxe, então precisa dele.
#     `executivo-atos` precisa do mandato já em base para atribuir os atos.
uv run resumo collect executivo-governadores
uv run resumo collect executivo-atos

# 3b. Consolidação da frequência derivada (sem rede — lê o que já foi coletado).
#     A Câmara NÃO passa por aqui: `attendance_record` só recebe presenças dela,
#     e agregá-las daria 100% para todo deputado federal.
uv run resumo collect senado-presenca-resumo
uv run resumo collect alesc-presenca-resumo

# 4. Emendas parlamentares (arquivo único da CGU, ~32 MB), depois a ponte autor↔mandato
uv run resumo collect emendas
uv run resumo link-emendas-authors

# 5. Resolução — materializa os vínculos candidatura↔mandato
uv run resumo resolve --year 2026

# 6. Front público
uv run resumo serve                 # http://127.0.0.1:8000

# 7. Ou renderize o site estático (mesmos templates e queries, executados no build)
uv run resumo render --out _site    # 658 fichas + JSON + PDFs + fotos em ~2 s
```

O deploy é estático e gratuito (GitHub Pages + Actions) — o banco sai do caminho da
requisição e vai para o caminho do build. Detalhes, tamanhos medidos e o que nunca é
publicado: [DEPLOY.md](DEPLOY.md).

> **Ordem importa.** `link-emendas-authors` e todos os coletores de histórico
> dependem dos mandatos já coletados. Rodar fora de ordem não quebra nada — só
> produz resultado vazio até você coletar os mandatos e repetir.

> Os coletores são **idempotentes**: re-rodar com a mesma fonte é *no-op* quando o hash
> do artefato não mudou (`RawIngestion`). Bons para `cron`. A chave do ledger inclui o
> **escopo** — ampliar o escopo re-ingere o mesmo arquivo, em vez de pulá-lo como
> "inalterado".

---

## Calendário dos dados (2026)

Registro fechou em **15/08/2026**, então candidaturas, bens, fotos e propostas **já
existem**.
Prestação de contas **não**:

| Dado | Disponível |
|---|---|
| Candidaturas, bens, fotos, propostas de governo | ✅ agora (arquivo regenerado diariamente ~04:00 BRT) |
| Relatórios financeiros (72 h por recurso) | 🟡 desde 20/07, volume mínimo |
| **Prestação parcial** | 🔴 entrega 9–13/09, publicação ~13–15/09/2026 |
| **Contas finais** (1º turno) | 🔴 até 03/11/2026 |
| Contas finais (2º turno) | 🔴 até 14/11/2026 |

O coletor de contas trata arquivo vazio como resultado normal (`empty`), não como erro
— rode em `cron` desde já.

---

## Modelo de dados

`Person` · `Candidacy` · `Mandate` · **`CandidateMandateLink`** · `GovernmentProposal` ·
`CandidatePhoto` ·
`Vote` · `Proposition` · `AttendanceRecord` · `Expense` · `CandidateAsset` · `Coalition` ·
`CampaignRevenue` · `CampaignRevenueOriginator` · `CampaignExpense` · `CampaignPayment` ·
`BudgetAmendment` · `AmendmentAuthorLink` · `ReviewQueue` · `RawIngestion`.
Definições em [src/resumo/db/models.py](src/resumo/db/models.py).

## Resolução de identidade

Cada casa publica um conjunto diferente de identificadores, e isso governa a confiança:

| Casa | CPF | Nascimento | Ponte disponível | Tier alcançável |
|---|---|---|---|---|
| Câmara | ✅ | ✅ | CPF (determinístico) | `auto_strong` |
| Senado | ❌ **não publica** | ✅ | nome civil + nascimento | `auto_strong` |
| ALESC | ❌ | ❌ | CPF recuperado do TSE (ver abaixo) | `auto_strong` |
| ALESC *sem ponte* | ❌ | ❌ | só nome | `auto_weak` (teto) |

### A ponte de CPF pelo histórico do TSE

A ALESC não publica CPF nem nascimento, o que deixaria o nome como única ponte — e
nome sozinho emparelha pessoas diferentes (`SALMIR DA SILVA` com o mandato de
`ALTAIR DA SILVA`). Mas quem exerce mandato estadual **se elegeu**, e quem se elegeu
está no arquivo do próprio TSE daquela eleição, com CPF:

```
candidatura 2026  ──CPF (idêntico)──▶  candidatura 2022 (TSE)
                                             │
                             nome de urna ≡ nome parlamentar
                                             ▼
                                       mandato ALESC
```

O salto por nome continua existindo, mas compara **nome de urna com nome
parlamentar** — a mesma convenção — exigindo igualdade exata, correspondência única,
mesma UF, mesmo cargo, mesma eleição, e registro de quem ocupou ou podia ocupar a
cadeira (eleito ou suplente). O vínculo publicado é igualdade de CPF, e leva método
próprio (`cpf_via_tse`) para a ficha não anunciar mais certeza do que existe.

O sentido negativo é o mais valioso: **um mandato com CPF conhecido está fechado para
qualquer outro CPF**, o que refuta de uma vez os pares que a comparação de nomes
propôs por acaso. Em SC isso resolveu a maior parte da fila de revisão — de 85
pendências para 17, e de 20 para 58 vínculos `auto_strong`.

`resolution/identity.py` reúne a mesma pessoa vista por casas diferentes. **Nome
sozinho nunca funde dois registros** — duas pessoas homônimas são indistinguíveis sem
um campo corroborante, e uma fusão errada atribuiria o histórico de uma a outra. Uma
duplicata é recuperável; uma fusão errada, não.

Pelo mesmo motivo, um match sustentado **apenas pelo nome** é limitado a `auto_weak`
(`probabilistic.NAME_ONLY_CAP`) — nunca alcança o mesmo nível de um match por CPF.

- **auto_strong / auto_weak** → vínculo publicado (o nível aparece na ficha).
- **review** → fila manual, **não** aparece no front:

```bash
uv run resumo review list
uv run resumo review decide <review_id> match
uv run resumo resolve
```

Uma decisão manual é a única parte do pipeline que é julgamento humano, e por isso a
única que não pode viver só no banco: ela é versionada em
[review-decisions.yml](review-decisions.yml), com chave natural (`sq_candidato` +
`house`/`house_member_id`/`id_legislatura`) para sobreviver a um rebuild que
regenera todos os uuids.

```bash
uv run resumo review export -o pendencias.yml   # esqueleto já preenchido
uv run resumo review validate                   # roda no CI a cada push
uv run resumo review apply && uv run resumo resolve
```

## Testes

```bash
docker compose up -d
uv run pytest -q
```

Cobre parsing TSE Latin-1, filtro de UF/cargo, idempotência incluindo mudança de
escopo, paginação da Câmara (mock respx), taxonomia de cargos, identidade entre casas,
regras de resolução e o gating do histórico na API. Sem chamadas de rede.

## Conformidade

Dados públicos (LAI) — republicar é legítimo; proveniência registrada em
`RawIngestion`. Histórico nunca é vinculado por palpite: limite de confiança publicado,
nível exibido, fila de revisão manual. CPF é usado para o match mas **não** exibido na
UI (LGPD). Métricas derivadas (presença) são rotuladas como derivadas.

**Ausência de dado nunca é apresentada como juízo sobre o candidato.** A ficha
distingue quatro casos e diz qual é: histórico disponível mas sem incumbência
confirmada · cargo executivo (não se aplica) · cobertura parcial da Casa · sem fonte
pública.

## O que foi verificado contra dados reais

Não é uma lista de testes — é o que já rodou contra as fontes de produção:

| Verificação | Resultado |
|---|---|
| Candidaturas SC 2026 | 658 (8 governador · 13 senador · 229 dep. federal · 408 dep. estadual) |
| Bens declarados · propostas de governo | 3.428 · 8 (uma por candidatura a governador) |
| Mandatos | 26 Câmara · 9 Senado · 61 ALESC |
| Vínculo candidatura↔mandato (2026) | 64 (18 `cpf_exact` · 38 `cpf_via_tse` · 8 `probabilistic`) — 58 `auto_strong` |
| Fila de revisão manual | 17 (era 85 antes da ponte de CPF) |
| **Contas de campanha 2022/SC — receitas** | R$ 208.484.026,62 — **bate exatamente** com a fonte |
| **— despesas contratadas** | 58.320 linhas / R$ 204.602.522,45 — **exato** |
| **— despesas pagas** | 65.300 linhas / R$ 196.387.519,05 — **exato** |
| — conferência por candidato | idêntico ao `divulgacandcontas` (36 linhas / R$ 305.370,83) |
| Emendas SC (todos os anos) | 2.277 linhas · 132/236 códigos SIOP vinculados |
| Despesas ALESC 2025–26 | 26.593 linhas (823 sem atribuição, todas registradas em log) |

Duas correções de integridade que essa validação revelou, ambas silenciosas até serem
medidas contra a fonte:

- **`SQ_RECEITA` não é chave.** Usá-la como PK descartava ~0,2% da receita declarada
  (72 sequências cobrindo 241 linhas que são dinheiro diferente: mesmo candidato,
  mesmo turno, mesma prestação, valores distintos). A identidade passou a ser hash de
  linha, e o total passou a bater na casa dos centavos.
- **`-4` é sentinela do TSE**, não um valor. Era parseado como R$ −4,00.

## Roadmap

Extração/estruturação do texto das propostas · camada de avaliação (fidelidade
partidária, anomalias de despesa) · enriquecimento das emendas via Transferegov para
recuperar o município beneficiário quando a fonte diz "MÚLTIPLO".

---
