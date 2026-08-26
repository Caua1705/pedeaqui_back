# Rapidex — backend

API de pedidos de restaurante, multi-restaurante desde a base: cada restaurante é
um `slug` na URL pública e um `restaurant_id` em toda consulta. Serve o cardápio,
autentica cliente e lojista, calcula entrega, aplica cupom, grava o pedido, cobra
pelo Mercado Pago e monta as comandas que o agente de impressão imprime na loja.

FastAPI + SQLAlchemy sobre PostgreSQL (Supabase).

**Onde mexer:** [`docs/arquitetura.md`](docs/arquitetura.md) é o mapa.

---

## Rodar local

```powershell
py -m venv venv
venv\Scripts\activate
pip install -r requirements.lock.txt
copy .env.example .env     # e preencha os segredos
uvicorn main:app --reload
```

Linux/macOS: `python3 -m venv venv && source venv/bin/activate` no lugar das duas
primeiras linhas.

**O lock, e não o `requirements.txt`** — é o mesmo arquivo que o `Dockerfile` e
o CI instalam, congelado do que a produção tem de pé, e é o que faz "passa aqui"
e "passa lá" serem a mesma frase. `requirements.txt` é a lista do que o projeto
usa, com o motivo de cada escolha; instalá-lo deixaria o pip resolver versão
sozinho. Para atualizar dependência:
[`docs/operacao.md`](docs/operacao.md#5-dependências).

Sanidade: `curl http://localhost:8000/health`
Docs interativas: `http://localhost:8000/docs` — desligadas quando
`APP_ENV=production`.

## Rodar os testes

São **duas suítes**, separadas pelo marcador `db`:

```powershell
py -m pytest -q -m "not db"         # rápida, sem Docker — o laço de desenvolvimento
py -m pytest -q                     # tudo, antes de commitar
cd print-agent && py -m pytest -q   # 90 testes do agente de impressão
```

A rápida usa fakes em memória e não abre conexão nenhuma, mas precisa de um
`.env` válido: `src.core.config` é importado na cadeia.

A marcada `db` roda contra um Postgres 17 descartável, o mesmo major do
Supabase:

```powershell
docker compose -f docker-compose.test.yml up -d
py -m pytest -q -m db
docker compose -f docker-compose.test.yml down -v
```

O schema desse banco sai de `alembic/schema_baseline.sql` + `alembic upgrade
head`, **nunca** de `Base.metadata.create_all()` — o ORM não mapeia as
sequences (inclusive a de `order_number`), os defaults nem os índices criados à
mão. Detalhes em [`docs/testes.md`](docs/testes.md).

O `print-agent/` é um projeto separado e **não** roda no `pytest` da raiz, de
propósito.

## Deploy

```bash
GIT_SHA=$(git rev-parse --short HEAD) docker compose up -d --build
docker logs -f pedeaqui-api
```

**O `GIT_SHA=` na frente não é enfeite.** Sem ele a imagem nasce sem carimbo, o
boot escreve `git_sha=nao-carimbado` e não há como saber que código está no ar —
o que já custou uma bateria de medição em produção para ser respondido.

O Traefik roteia pela rede externa `n8n_default`; o compose não expõe porta.

⚠️ **O container roda `alembic upgrade head` antes do Uvicorn.** Migração ruim
vira loop de restart, e `CREATE INDEX` em tabela grande trava escrita com a API
fora do ar. Detalhes em [`docs/operacao.md`](docs/operacao.md).

Duas réplicas subindo juntas **não** migram ao mesmo tempo: `alembic/env.py`
toma um `pg_advisory_xact_lock` antes de ler `alembic_version`, e a segunda
espera (sem timeout, dizendo no log que está esperando). O porquê de cada
escolha está em [`src/db/advisory_lock.py`](src/db/advisory_lock.py).

## Antes de mexer em produção

```bash
docker exec pedeaqui-api python scripts/estado_da_producao.py
```

Uma tela, sem argumentos, e as quatro perguntas que precedem toda investigação:
**que código está no ar** (o `git_sha` da imagem, e se `ALEMBIC_TARGET` ficou
preso no `.env`), **em que revisão está o banco** (contra o `head` desta mesma
imagem — e distingue "atrás" de "à frente", que pedem coisas opostas), **se o
Redis responde** (e se despejou chave, que derruba rate limit em silêncio) e
**se as credenciais do Mercado Pago estão cadastradas** — conferidas
*decifrando*, o único jeito de saber que a `PAYMENT_CREDENTIALS_ENCRYPTION_KEY`
deste `.env` é a que cifrou o que está no banco.

Só leitura, e sai com 1 quando há erro. Para conferir **um restaurante** antes
do primeiro pedido dele, é outro script:
`python scripts/check_restaurant.py <slug>`.

## Variáveis de ambiente obrigatórias

Sem estas, o boot falha com `ValidationError` do pydantic-settings:

| Variável | O que é |
|---|---|
| `DATABASE_URL` | conexão Postgres. **Precisa do driver**: `postgresql+psycopg://...` |
| `SUPABASE_URL` | URL do projeto Supabase |
| `CUSTOMER_AUTH_SECRET` | segredo de assinatura dos tokens de cliente |
| `ADMIN_AUTH_SECRET` | segredo dos tokens de lojista. **Precisa ser diferente do de cliente** — mesmo valor derruba o boot |
| `EMAIL_CODE_SECRET` | segredo dos códigos de verificação de e-mail |
| `PASSWORD_RESET_SECRET` | segredo dos códigos de recuperação de senha |
| `OPENAI_API_KEY` | chat do Rapi (embeddings + LLM) |

Gere cada segredo separadamente:

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

⚠️ **Trocar `ADMIN_AUTH_SECRET` invalida todo token de lojista em circulação.**
Quem estiver no painel precisa logar de novo, e o agente de impressão instalado
com `token =` fixo no `config.ini` para de imprimir até receber um token novo —
o que roda com `email`/`password` refaz o login sozinho.

Condicionalmente obrigatórias — o boot **derruba** se faltarem no cenário delas:

| Variável | Quando é obrigatória |
|---|---|
| `GOOGLE_MAPS_ROUTES_API_KEY` | se `DELIVERY_ESTIMATE_PROVIDER=google_routes` (o padrão) |
| `PAYMENT_CREDENTIALS_ENCRYPTION_KEY` | se `PAYMENT_PROVIDER=mercadopago` |

O `.env.example` está anotado por categoria e cobre o resto (Redis, rate limit,
limites de corpo, TTLs).

---

## Documentação

| Arquivo | Assunto |
|---|---|
| [docs/arquitetura.md](docs/arquitetura.md) | **o mapa** — pastas, caminho de um pedido, onde mora o dinheiro, máquina de estados |
| [docs/modelo-de-dados.md](docs/modelo-de-dados.md) | **o diagrama ER** (Mermaid, seis por assunto), as 40 tabelas e o isolamento entre restaurantes |
| [docs/cardapio-por-filial.md](docs/cardapio-por-filial.md) | por que o cardápio pende de filial e nada nele herda |
| [docs/cashback.md](docs/cashback.md) | crédito, resgate, validade e por que ligar a chave mexe em faturamento |
| [docs/testes.md](docs/testes.md) | as duas suítes, e por que o schema de teste não sai do `create_all` |
| [docs/pagamentos-e-comissao.md](docs/pagamentos-e-comissao.md) | cobrança, Mercado Pago, comissão |
| [docs/entrega-e-horarios.md](docs/entrega-e-horarios.md) | estimativa, taxa por km, horário de funcionamento |
| [docs/autenticacao-e-escopo.md](docs/autenticacao-e-escopo.md) | tokens e escopo por restaurante/filial |
| [docs/impressao.md](docs/impressao.md) | setores, comandas, print-agent |
| [docs/operacao.md](docs/operacao.md) | deploy, migrações, logs, diagnóstico |
| [docs/onboarding-de-restaurante.md](docs/onboarding-de-restaurante.md) | **pôr um restaurante novo no ar** — a ordem real, o que só dá para fazer no banco, e os cinco passos que falham em silêncio |
| [docs/contrato-filiais-frontend.md](docs/contrato-filiais-frontend.md) | contrato da tela de escolha de filial, para quem implementa o app |
| [docs/operacao-por-filial.md](docs/operacao-por-filial.md) | **o "fechar agora" e os preços por filial** — os dois regimes (estado do dia × termo comercial), as rotas que mudaram de lugar e o que rodar depois do deploy |

Armadilhas de quem escreve código: `.claude/skills/rapidex-backend/SKILL.md`.
