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
pip install -r requirements.txt
copy .env.example .env     # e preencha os segredos
uvicorn main:app --reload
```

Linux/macOS: `python3 -m venv venv && source venv/bin/activate` no lugar das duas
primeiras linhas.

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
docker compose up -d --build
docker logs -f pedeaqui-api
```

O Traefik roteia pela rede externa `n8n_default`; o compose não expõe porta.

⚠️ **O container roda `alembic upgrade head` antes do Uvicorn.** Migração ruim
vira loop de restart, e `CREATE INDEX` em tabela grande trava escrita com a API
fora do ar. Detalhes em [`docs/operacao.md`](docs/operacao.md).

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
| [docs/modelo-de-dados.md](docs/modelo-de-dados.md) | tabelas e isolamento entre restaurantes |
| [docs/pagamentos-e-comissao.md](docs/pagamentos-e-comissao.md) | cobrança, Mercado Pago, comissão |
| [docs/entrega-e-horarios.md](docs/entrega-e-horarios.md) | estimativa, taxa por km, horário de funcionamento |
| [docs/autenticacao-e-escopo.md](docs/autenticacao-e-escopo.md) | tokens e escopo por restaurante/filial |
| [docs/impressao.md](docs/impressao.md) | setores, comandas, print-agent |
| [docs/operacao.md](docs/operacao.md) | deploy, migrações, logs, diagnóstico |
| [docs/onboarding-de-restaurante.md](docs/onboarding-de-restaurante.md) | **pôr um restaurante novo no ar** — a ordem real, o que só dá para fazer no banco, e os cinco passos que falham em silêncio |

Armadilhas de quem escreve código: `.claude/skills/rapidex-backend/SKILL.md`.
