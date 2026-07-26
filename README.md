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
- `INTERNAL_API_KEY`: temporary key for admin/internal endpoints.

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
- `GET /restaurants/{restaurant_slug}/orders/{order_number}?phone=85999999999`

## Admin/Internal Endpoints

Send `X-API-Key: <INTERNAL_API_KEY>`.

- `GET /admin/restaurants/{restaurant_slug}/orders`
- `GET /admin/orders/{order_id}`
- `PATCH /admin/orders/{order_id}/status`

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

- Add real admin authentication with JWT and password verification.
- Add customer login/authentication if the product requires order history by account.
- Add Alembic migrations if this backend starts owning schema evolution.
