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

```powershell
py -m pytest -q                     # 614 testes da API
cd print-agent && py -m pytest -q   # 65 testes do agente de impressão
```

Não precisam de banco (usam fakes em memória), mas precisam de um `.env` válido:
`src.core.config` é importado na cadeia.

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
| `EMAIL_CODE_SECRET` | segredo dos códigos de verificação de e-mail |
| `PASSWORD_RESET_SECRET` | segredo dos códigos de recuperação de senha |
| `OPENAI_API_KEY` | chat do Rapi (embeddings + LLM) |

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

Armadilhas de quem escreve código: `.claude/skills/rapidex-backend/SKILL.md`.
