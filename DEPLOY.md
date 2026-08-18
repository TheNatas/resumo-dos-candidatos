# Deploy

Site estático publicado no **GitHub Pages**, gerado por **GitHub Actions** em cron
diário. Custo **zero**, sem cartão, sem expiração de trial e sem servidor exposto.

O banco **não** vai para o repositório. Ele continua sendo um Postgres — só deixa de
estar no caminho da requisição e passa para o caminho do build:

```
hoje    visitante → FastAPI → SQL → HTML          (uma vez por visitante)
deploy  cron      → SQL → arquivos HTML           (uma vez por dia)
                   visitante → arquivo
```

Mesmo `queries.py`, mesmos templates Jinja, mesmo `pg_trgm`/`unaccent`. Muda *quando* a
query roda, não de onde os dados vêm.

---

## Por que esta escolha

Restrição declarada: **nada pago**. Sobrou pouca coisa de pé:

| Opção | Por que não |
|---|---|
| Render free | Postgres grátis expira em 30 dias; web service dorme após 15 min |
| Fly.io | não tem mais faixa gratuita |
| Neon / Supabase free | teto de 500 MB. O banco já tem **196 MB**, e só a prestação de contas 2022/SC responde por ~130 MB dele — quando as contas de 2026 chegarem (13–15/09 e 03/11) o teto estoura **no meio da campanha** |
| Oracle Always Free | funciona e não exige mudança de código — ver *Alternativa* no fim |

E o formato estático não é só o que sobrou; ele encaixa neste projeto:

- A superfície pública é read-only e pequena: **658 candidaturas** no escopo 2026.
- A busca já é `ilike` sobre uma coluna pré-normalizada (`nome_normalizado`) —
  isso é um JSON de ~390 KB e um filtro em JS, não uma dependência de Postgres.
- `candidate_detail` devolve **agregados**, não dumps de linha: a página mais pesada
  do dataset (incumbente com votos, despesas e emendas) tem **3,1 KB**.
- CDN aguenta pico de dia de eleição; free tier de banco não.
- O log público do Actions vira um segundo registro de proveniência, ao lado de
  `RawIngestion`.
- Sem banco público, sem escrita, sem autenticação, sem rate limiting para construir.

## Arquitetura do build

```
GitHub Actions (cron, ~05:00 BRT)
  ├── restaura snapshot do banco ──► service container postgres:16
  │                                  (é superuser: CREATE EXTENSION funciona)
  ├── uv run resumo collect …        ← idempotente: no-op quando o hash não mudou
  ├── uv run resumo resolve --year 2026
  ├── uv run resumo render ────────► _site/
  ├── salva o snapshot
  └── actions/deploy-pages ────────► GitHub Pages (TLS grátis)
```

Todo coletor é idempotente e **nenhuma fonte exige chave**. Se o snapshot for perdido,
o backfill completo simplesmente roda de novo — bem dentro do limite de 6 h por job.
O pipeline se recupera sozinho por construção.

## Onde cada coisa vive

Tamanhos **medidos**, não estimados:

| Artefato | Tamanho | Onde | Vai pro git? |
|---|---|---|---|
| Postgres | 196 MB | sua máquina (dev) + service container efêmero no CI | não |
| Snapshot (`pg_dump -Fc`) | **23 MB** | cache do Actions (10 GB grátis, mantido quente pelo cron diário) | **não** |
| Site renderizado | **12 MB** | deployment do Pages | **não** |
| Código + templates | 159 KB | o repositório | sim |

O site, medido em `resumo render` sobre os dados reais (658 fichas, 2,1 s):

| | |
|---|---|
| `proposta/` (8 PDFs) | 5,2 MB |
| `candidato/` (658 fichas) | 4,3 MB |
| `api/` (índice + 658 JSON) | 1,4 MB |
| `index.html` (todos os 658 cards) | 446 KB |
| `sitemap.xml` · `static/` · `404.html` | 90 KB |

O que o visitante realmente baixa, com o gzip que o Pages aplica: **23 KB** na home —
já com as 658 candidaturas dentro, sem nenhuma requisição extra — e **1,3 KB** por ficha.

> **Nada é commitado.** O jeito antigo de publicar no Pages era empurrar HTML para uma
> branch `gh-pages` — isso somaria ~8,5 MB de objetos git **por build**, ~3 GB de
> histórico por ano. `actions/deploy-pages` sobe o site como artefato de deployment:
> cada deploy substitui o anterior e o git nunca vê o conteúdo.

## O que nunca é publicado

As 29.270 linhas de 2022, as 65.300 linhas de pagamento, os 95.987 bens declarados e
`candidacy.cpf_raw` ficam no Postgres e no snapshot privado. Só as 658 candidaturas
de 2026 no escopo são renderizadas.

Se algum dia o dataset for publicado (é um ganho de transparência legítimo), publique
um dump **sem CPF** — não o snapshot do pipeline.

## Passos

- [x] **1. Correções de pré-lançamento**
  - [x] busca pública fixada no ano configurado — o banco também guarda o conjunto de
        validação de 2022, e sem escopo a busca por "silva" devolvia 4.586 linhas de
        2022 contra 55 de 2026, com selo `ELEITO`, sob um cabeçalho "Eleições 2026"
  - [x] propostas de governo viraram link (`/proposta/{id}.pdf`), com o caminho de
        arquivo do servidor fora do payload público
  - [x] `site_base_url`: URLs internas funcionam na raiz e em subpath do Pages
  - [x] aviso quando o `User-Agent` ainda usa o contato de exemplo
  - [ ] **definir um contato real em `RESUMO_HTTP_USER_AGENT`** — nenhuma fonte tem
        chave de API, então esse header é a única identificação do operador; coletar
        de IP do GitHub com contato falso é como se toma bloqueio de WAF
- [x] **2. `uv run resumo render`** — mesmos templates e queries do app vivo
  - [x] `_site/` completo: index, 658 fichas, JSON por candidato, PDFs, `404.html`,
        `robots.txt`, `sitemap.xml`, `.nojekyll`
  - [x] busca client-side sem htmx: as 658 candidaturas já vêm na página e o filtro é
        um predicado no DOM — **funciona com JavaScript desligado** (lista tudo) e a
        normalização espelha `resumo.util.normalize_name`, com a mesma semântica de
        substring do `ILIKE '%…%'` do servidor. Conferido: "silva" devolve 55 no
        filtro estático e 55 na API
  - [x] `--out` se recusa a apagar diretório que não foi ele que escreveu
- [x] **3. Workflow do Actions** — [`.github/workflows/build-and-deploy.yml`](.github/workflows/build-and-deploy.yml)
  - [x] `validate` (arquivo de decisões + ruff) → `test` (suíte com Postgres) →
        `build` → `deploy`: nada é publicado sem a suíte passar
  - [x] snapshot restaurado/salvo no cache do Actions: **42 MB** por entrada
        (23 MB de dump + 20 MB de PDFs), contra um limite de 10 GB
  - [x] coletores em `continue-on-error`: fonte fora do ar é "sem dado novo hoje",
        não site fora do ar
  - [x] prefixo de URL vem do `actions/configure-pages`, então trocar para domínio
        próprio não exige mexer no workflow
  - [ ] **habilitar Pages no repositório** (Settings → Pages → Source: GitHub Actions)
  - [ ] **definir a variável `RESUMO_CONTACT`** (Settings → Secrets and variables →
        Actions → Variables) — é ela que entra no `User-Agent`
- [ ] **4. Página `/sobre`** — carregar as ressalvas que hoje só existem no README
      (ALESC ~96% simbólicas · Senado 57% secretas · emendas sem "valor autorizado")
- [ ] **5. Cron diário 05:00 BRT** — a prestação parcial de 13–15/09 entra sozinha

## Limite conhecido

Ampliando para nacional (`RESUMO_TARGET_UFS=""`), são ~29 mil candidatos: ~130 MB de
páginas (ainda dentro do limite de 1 GB do Pages), mas o índice de busca vai de 390 KB
para **~17 MB** — grande demais para mandar ao navegador em um arquivo só. Nesse ponto:
dividir o índice por UF, ou SQLite no navegador (sql.js / DuckDB-WASM). Não é problema
em 658, mas ampliar escopo é objetivo declarado do projeto.

## Alternativa, se um banco vivo importar mais

**Oracle Cloud Always Free** — 4 cores ARM / 24 GB / 200 GB, sem expiração, roda o
`docker compose` e o cron exatamente como estão hoje, zero mudança de código. Custos
reais: cartão para verificação de identidade, capacidade ARM frequentemente indisponível
na região de São Paulo, instâncias ociosas podem ser recuperadas — e backup, TLS e
patching de uma máquina exposta durante o período eleitoral passam a ser seus.

## Decisões de revisão: declarativas

Com o CI mantendo o banco canônico haveria dois escritores — o pipeline e você rodando
`resumo review decide` localmente. A saída escolhida é
[`review-decisions.yml`](review-decisions.yml): o julgamento manual fica versionado, e
o pipeline o aplica antes de resolver.

```bash
uv run resumo review export -o pendencias.yml   # esqueleto já preenchido
# decida cada caso e explique no `note`; copie as entradas decididas
uv run resumo review validate                   # roda também no CI, a cada push
uv run resumo review apply && uv run resumo resolve
```

**A chave é natural, nunca o id da fila.** `review_queue.id` e `mandate.id` são uuids
gerados na inserção: mudam a cada rebuild do banco, e um arquivo indexado por eles
pararia de casar silenciosamente justo quando o pipeline se recupera de um backfill
frio. A chave é `sq_candidato` (TSE) + `house`/`house_member_id`/`id_legislatura` (a
própria Casa) — tudo o que as fontes publicam e que sobrevive a um rebuild.

Três garantias, todas testadas:

- **Contradição é erro, não desempate.** Duas decisões para o mesmo par, ou dois
  `match` para a mesma candidatura, recusam o arquivo inteiro — `resolve` força no
  máximo um mandato por candidatura, então a segunda decisão faria o resultado
  depender da ordem das linhas.
- **`apply` não inventa entrada na fila.** Se o pipeline nunca propôs o par, a decisão
  é reportada e ignorada: forçar um vínculo que ninguém propôs publicaria um palpite
  humano como se fosse achado do pipeline.
- **Idempotente.** Reaplicar um arquivo inalterado não escreve nada, que é o que
  permite o build rodar todo dia.

No workflow a ordem é `resolve` → `review apply` → `resolve`: a fila é *saída* do
resolve e as decisões são *entrada* dele, e num banco recém-construído a fila só
existe depois da primeira passada.
