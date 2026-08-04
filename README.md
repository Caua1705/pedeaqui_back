# PedeAqui Backend

FastAPI backend for PedeAqui, a white-label restaurant ordering platform. The first restaurant can be Junior da Picanha, but the backend is multi-restaurant by design and resolves public data by restaurant slug.

## Architecture

- `api/endpoints`: HTTP route layer.
- `services`: business rules and orchestration.
- `repositories`: database queries only.
- `schemas`: Pydantic request and response DTOs.
- `models`: SQLAlchemy ORM models matching the existing PostgreSQL schema.
- `utils`: shared helpers for storage URLs and money conversion.

This project intentionally does not use a `controllers` folder. In this codebase, endpoints are the controller-like HTTP layer.

## Environment

Create a local `.env` from `.env.example`:

```bash
cp .env.example .env
```

Configure:

- `DATABASE_URL`: Supabase/PostgreSQL connection string using `postgresql+psycopg`.
- `SUPABASE_URL`: Supabase project URL.
- `SUPABASE_STORAGE_BUCKET`: public storage bucket, default `restaurant-assets`.
- `CUSTOMER_AUTH_SECRET`: signing secret for customer tokens.
- `ADMIN_AUTH_SECRET`: signing secret for merchant tokens. Falls back to
  `CUSTOMER_AUTH_SECRET` when empty; a dedicated value is recommended.
- `INTERNAL_API_KEY`: deprecated in Phase 1, no longer used by any route.

## Run Locally

Linux/macOS:

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
uvicorn main:app --reload
```

Windows:

```powershell
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
uvicorn main:app --reload
```

Open docs at:

```text
http://localhost:8000/docs
```

## Run With Docker

```bash
docker compose up -d --build
docker logs -f pedeaqui-api
```

The compose file is prepared for Traefik on the external `n8n_default` network and does not expose public ports directly.

## Database Migrations (Alembic)

Schema evolution is owned by Alembic (`alembic/versions/`). The `migrations/`
folder holds the 13 hand-applied `.sql` files from before Phase 1 and is
frozen — see `migrations/README.md`.

### Baseline: run once per existing database

The production schema already exists and was never under Alembic. Applying
the baseline as a migration would fail. Instead, **stamp** it — this records
the revision in `alembic_version` without executing any DDL:

```bash
alembic stamp 20260726_0001
```

Do this once, on every database that already has the schema (production and
your current dev database). Check it worked:

```bash
alembic current   # -> 20260726_0001 (head)
```

### Development

```bash
alembic upgrade head              # apply pending migrations
alembic revision -m "add x"       # new empty migration
alembic revision --autogenerate -m "add x"
alembic downgrade -1              # roll back one revision
alembic history --verbose
```

`--autogenerate` compares the ORM models against the live database. Always
read the generated file before applying: the production database has objects
the ORM does not map (sequences, hand-made indexes from the old `.sql`
files), and autogenerate will propose dropping them. Delete those lines.

### Production

Migrations do **not** run automatically at container start. That is
deliberate: an automatic `upgrade head` on boot means a bad migration takes
the API down with it, and with more than one container they race. Run it as
an explicit step:

```bash
# 1. Back up first — this is Supabase/Postgres, a failed DDL can be costly
docker exec pedeaqui-api alembic current      # confirm current revision
docker compose up -d --build                  # deploy new code
docker exec pedeaqui-api alembic upgrade head # then migrate
docker exec pedeaqui-api alembic current      # confirm new revision
```

Migrations in this project are written to be backward compatible with the
previous code version, so deploying the image before migrating is safe.

## Public Endpoints

- `GET /health`
- `GET /restaurants/{restaurant_slug}`
- `GET /restaurants/{restaurant_slug}/menu`
- `GET /restaurants/{restaurant_slug}/categories/{category_slug}/products`
- `GET /restaurants/{restaurant_slug}/products/{product_slug}`
- `POST /restaurants/{restaurant_slug}/orders`
- `GET /restaurants/{restaurant_slug}/orders/track/{tracking_token}`
- `POST /restaurants/{restaurant_slug}/orders/{tracking_token}/payment`
- `POST /payments/webhooks/{provider}` — chamada pelo gateway, nao pelo app

A rota de pagamento e a unica com `detail` em formato de OBJETO quando
falha (`{code, message, retryable, provider_error_code}`, 502 ou 503): sem
o `retryable` o frontend nao tem como escolher entre oferecer "tentar de
novo" e mandar o cliente falar com o restaurante. Ver `docs/arquitetura.md`
4.5.2.

The lookup by `order_number` + phone was **removed in Phase 2**. Order
numbers come from a global sequence, so with one phone number an attacker
could walk the neighbouring numbers and read other people's home address,
items and history. The tracking token is random, returned once, to whoever
created the order. A logged-in customer does not need it:
`GET /customers/me/orders/{order_id}` derives access from `customer_id`.

## Admin Endpoints

Admin routes authenticate with a merchant JWT. The shared `X-API-Key` was
removed in Phase 1: one key for everyone could not say *who* was calling or
*which restaurant* they belonged to, so no route could scope anything by
tenant.

```bash
curl -X POST http://localhost:8000/admin/auth/login \
  -H "Content-Type: application/json" \
  -d '{"email": "junior@exemplo.com", "password": "..."}'
```

Then send `Authorization: Bearer <access_token>` on every admin route.

- `POST /admin/auth/login` — public
- `GET  /admin/auth/me`
- `GET  /admin/restaurants/{restaurant_slug}/orders`
- `GET  /admin/orders/{order_id}`
- `PATCH /admin/orders/{order_id}/status`
- `GET  /admin/reports/commission?start_date=2026-07-01&end_date=2026-07-31`
- `GET/POST/PATCH /admin/restaurants/{restaurant_id}/coupons`

Every one of these is scoped to the `restaurant_id` in the token. A
restaurant slug or id in the URL never grants access on its own — it is
checked against the token and returns 404 when it does not match.

### Creating the first merchant

There is no merchant signup screen, and the first admin cannot be created
through the API (nobody is authenticated yet to authorize it). Use the
script:

```bash
docker exec -it pedeaqui-api python scripts/create_admin_user.py \
  --restaurant-slug junior-da-picanha \
  --name "Junior" \
  --email junior@exemplo.com \
  --role owner
```

The password is asked in a hidden prompt — never pass it as an argument, it
would land in the shell history. Roles come from `ADMIN_USER_ROLES`:
`owner`, `manager`, `attendant`.

## Idempotency

`POST /restaurants/{slug}/orders` and `PATCH /admin/orders/{id}/status`
accept an `Idempotency-Key` header. Send a fresh UUID per logical operation
and reuse it on every retry: resending the same key with the same body
returns the original response instead of creating a second order.

- same key, same body, already completed → the stored response
- same key, different body → 422
- same key, still running → 409
- no key → accepted without protection (a warning is logged). Set
  `IDEMPOTENCY_REQUIRED=true` to reject with 400 instead.

Keys expire after `IDEMPOTENCY_TTL_HOURS` (24h). Purge expired rows with a
cron:

```bash
docker exec pedeaqui-api python scripts/cleanup_idempotency_keys.py
```

## Example Requests

Health:

```bash
curl http://localhost:8000/health
```

Menu:

```bash
curl http://localhost:8000/restaurants/junior-da-picanha/menu
```

Menu response shape:

```json
{
  "restaurant": {},
  "settings": {},
  "branches": [],
  "banners": [],
  "coupons": [],
  "categories": [],
  "products": []
}
```

Create order:

```bash
curl -X POST http://localhost:8000/restaurants/junior-da-picanha/orders \
  -H "Content-Type: application/json" \
  -d '{
    "branch_id": "00000000-0000-0000-0000-000000000000",
    "customer": {"name": "Caua", "phone": "85999999999"},
    "order_type": "delivery",
    "payment_method": "pix",
    "delivery_estimate_token": "<token devolvido por POST /delivery/estimate>",
    "address": {
      "street": "Rua Exemplo",
      "number": "123",
      "neighborhood": "Varjota",
      "complement": "Apto 101",
      "reference": "Perto da praca"
    },
    "notes": "Sem cebola",
    "items": [
      {"product_id": "00000000-0000-0000-0000-000000000000", "quantity": 2, "observation": "Ao ponto"}
    ]
  }'
```

## Deployment Notes

On the VPS, keep Traefik attached to the same Docker network (`n8n_default`) and set DNS for `api.pederapidex.com` to the server. Secrets should be provided only through `.env` or the deployment environment.

## TODO

- Integration tests against a real Postgres (Phase 4): idempotency under
  concurrency, tenant filtering at the SQL level, TTL cleanup.
- Versioned schema dump so a database can be built from scratch — today the
  Alembic baseline assumes the schema already exists.
- Per-branch scoping for merchants (`admin_users.branch_id` is stored but not
  yet enforced on routes).
- Merchant management screen, replacing `scripts/create_admin_user.py`.
