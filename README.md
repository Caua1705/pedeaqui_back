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

#### Convention: `IF NOT EXISTS` on baseline tables

An index created on a table that predates Alembic — anything built by the
frozen `.sql` files (`orders`, `products`, `categories`, `branches`, …) — must
pass `if_not_exists=True`, and its `drop_index` must pass `if_exists=True`.
The name may already be taken by an object nobody recorded, and a migration
has no way to know. `20260806_0010` broke in production for exactly this.

An index on a table the same revision creates does **not** get it: there the
name collision would be a genuine bug and should fail loudly. Same rule for
`create_table` and `add_column` when you touch a baseline table.

#### `IF NOT EXISTS` matches by name, not by definition

This is the half the convention above does **not** cover, and it fails
silently instead of loudly.

`CREATE INDEX IF NOT EXISTS ix_foo ON t (c)` skips only if something named
`ix_foo` exists. If the same columns are already indexed as `idx_foo`, the
statement succeeds and the table ends up with **two identical indexes**,
both maintained on every write, serving zero extra queries. Nothing errors
and nothing shows up in a diff.

That is exactly what `order_items.order_id` was: indexed by hand as
`idx_order_items_order_id`. `20260810_0012` therefore does not just create
the canonical name — it drops the legacy one first, so Alembic ends up
owning the index instead of standing a duplicate next to it.

So: **before adding an index to a baseline table, check what is already
indexed there**, by definition and not by name.

```bash
docker exec pedeaqui-api python scripts/audit_indexes.py
```

It is read-only. It reports three things: duplicate definitions (with the
`op.drop_index` to paste into a revision), names outside the `ix_`/`uq_`
convention, and indexes on baseline tables that no revision declares. It
exits non-zero when it finds a duplicate, so it can gate a deploy.

#### Rebuilding an index blocks writes

`CREATE INDEX` without `CONCURRENTLY` locks the table against writes while
it runs, and the entrypoint runs `alembic upgrade head` **before** Uvicorn.
On a large table that means the API stays down and no new orders can be
written for the whole build. Apply revisions that build an index on
`orders` or `order_items` outside peak hours.

If there is no window, do the swap by hand with `CREATE INDEX CONCURRENTLY`
(which cannot run inside a transaction, and so cannot live in a migration)
and then `alembic stamp <revision>`.

### Production

Migrations run automatically at container start. `docker-entrypoint.sh` runs
`alembic upgrade head` and only then hands off to Uvicorn, so deploying new
code cannot leave the API answering against the old schema.

```bash
# 1. Back up first — this is Supabase/Postgres, a failed DDL can be costly
docker exec pedeaqui-api alembic current  # confirm current revision
docker compose up -d --build              # migrates, then serves
docker logs -f pedeaqui-api               # "[entrypoint] alembic upgrade head"
docker exec pedeaqui-api alembic current  # confirm new revision
```

Two consequences to know about:

- **A failing migration keeps the API down.** The entrypoint does not swallow
  the error, so the container exits and `restart: always` retries it in a
  loop. That is the intended trade: a visible restart loop beats an API
  serving requests against a schema it does not match.
- **Scaling past one container makes them race.** They would all run
  `upgrade head` at once. Before adding a replica, move the migration to a
  one-shot step (or take a Postgres advisory lock inside `env.py`).

A database that already has the schema must be stamped (see the baseline
section above) **before** its first `up` with this entrypoint — otherwise the
upgrade tries to apply `0002` onward on top of tables that already exist.

Migrations in this project are written to be backward compatible with the
previous code version, so a container still on the old image keeps working
while the new one migrates.

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
- `GET  /admin/orders/{order_id}` — includes each item's chosen add-ons, grouped by option group
- `PATCH /admin/orders/{order_id}/status`
- `PATCH /admin/orders/{order_id}/cancel` — requires `{"reason": "..."}`, stored in the status history
- `PATCH /admin/branches/{branch_id}/prep-time` — the +5/-10 shortcut for the period in effect right now
- `GET  /admin/orders/{order_id}/print-jobs` — the receipts, already laid out as fixed-width text
- `GET/POST /admin/branches/{branch_id}/printing-sectors`, `PATCH /admin/printing-sectors/{sector_id}`
- `PATCH /admin/products/{product_id}/printing-sector`, `PATCH /admin/categories/{category_id}/printing-sector`
- `PATCH /admin/categories/reorder`, `PATCH /admin/products/reorder` — send the whole list, the server numbers it
- `GET  /admin/reports/commission?start_date=2026-07-01&end_date=2026-07-31`
- `GET/POST/PATCH /admin/restaurants/{restaurant_id}/coupons`

Every one of these is scoped to the `restaurant_id` in the token. A
restaurant slug or id in the URL never grants access on its own — it is
checked against the token and returns 404 when it does not match.

### Reordering

`PATCH /admin/categories/reorder` takes every category of the restaurant;
`PATCH /admin/products/reorder` takes a `category_id` plus every product **of
that category**. Product `sort_order` only means anything inside a category —
the public menu orders by `Category.sort_order, Product.sort_order,
Product.name` — so the set that shares a numbering is the category, not the
restaurant.

Both refuse a partial list with 400. Renumbering a subset would leave the
rest with duplicate `sort_order` and the final order would fall back to the
name tiebreak, which is not what anyone dragged on screen. A 400 here means
"reload the list" — someone added an item in another tab.

### Performance reports

All five take `start_date`/`end_date`, read in **America/Fortaleza** (not
UTC), capped at 92 days. Cancelled, rejected and refunded orders are excluded
by the same SQL predicate the commission extract uses —
`billable_order_conditions` in `src/repositories/order_repository.py`. There
is one definition of "an order that counted", not one per report.

- `GET /admin/reports/summary` — revenue, orders, average ticket,
  delivery/pickup split, and the same numbers for the preceding period of
  equal length. `change_percent` is `null` when the previous period was zero.
- `GET /admin/reports/sales-by-day` — one entry per day of the period,
  including days with no sales, bucketed by **local** date.
- `GET /admin/reports/payment-methods` — a `null` method means "not
  recorded", which is not the `other` payment method.
- `GET /admin/reports/products?limit=20` — ranked by units sold, grouped by
  the name snapshotted on the order item. Renaming a product splits it into
  two rows on purpose: they were two different items to whoever bought them.
  `listed_revenue_total` **does not** reconcile with `summary` — it is gross
  item revenue, before coupon, cashback and fees. The response says so in
  `revenue_note`.
- `GET /admin/reports/cancellations` — the exact complement of what the other
  four exclude. The rate is over *all* orders in the period, not just the
  billable ones.

## Printing

The local print agent is deliberately dumb: it reads a job's `content`,
selects `font_size`, writes it to the printer and cuts. It does not wrap,
align or decide what belongs on which copy — all of that lives in
`src/services/print_layout.py`, so the rule sits in one place, is unit
tested, and a layout fix is a deploy instead of a visit to every shop.

`GET /admin/orders/{order_id}/print-jobs` returns one customer copy (48
columns, prices, totals) plus one production copy per printing sector with
items in the order (24 columns because it prints in double-width font, no
prices at all). Each job carries `sector_name`, `columns`, `font_size` and
`content`.

Printing sectors belong to a **branch**, not to a restaurant: the printer is
a physical machine standing in one shop. A product with
`printing_sector_id = null` prints no production copy — that is the can of
soda taken from the counter fridge, not a missing setting.

An order whose online payment has not been confirmed gets **only** the
customer copy. A production copy is an order to start cooking, and the
"awaiting payment, do not prepare" rule cannot apply just to whoever is
looking at the screen.

The agent itself lives in [`print-agent/`](print-agent/README.md) — same
repo so it cannot drift from the API it consumes, but outside this build:
it is in `.dockerignore`, has its own `requirements.txt` (requests +
pywin32, nothing from `src/`), and its tests are not collected by the root
`pytest` (see `pytest.ini`).

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

`POST /restaurants/{slug}/orders`, `PATCH /admin/orders/{id}/status` and
`PATCH /admin/orders/{id}/cancel` accept an `Idempotency-Key` header. Send a fresh UUID per logical operation
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

## Delivery Fee Fallback

The delivery fee normally comes from the **branch**: `delivery_base_fee +
distance × delivery_fee_per_km`, clamped by `delivery_min_fee` /
`delivery_max_fee`. That needs a route from Google.

`restaurant_settings.default_delivery_fee` is the contingency value, used
only when that rule cannot be applied:

1. **Google Routes is unavailable.** No route, so no distance, so no per-km
   fee. Before this fallback existed, an outage at Google refused *every*
   delivery order on the platform — while the estimate already reported
   `provider: "configured_fallback"` with nothing configured behind it.
2. **The branch has no `delivery_base_fee`/`delivery_fee_per_km`.** The route
   is fine, the pricing rule is simply missing.

Two things to know:

- **Zero disables the fallback; it does not mean free delivery.** The column
  defaults to `0` and most production rows were never touched, so reading
  that `0` as a choice would turn a Google outage into free shipping for
  everyone. Free delivery is configured with `delivery_base_fee = 0` on the
  branch, which is the normal path and still works.
- **In case 1 the delivery radius is not checked.** Without a route there is
  no distance to compare against `delivery_max_distance_km`, so an
  out-of-area address gets through. That is accepted because the order still
  lands in `pending` and the merchant has to accept it, and the order carries
  `delivery_estimate_provider = "configured_fallback"` so those can be told
  apart. Case 2 still enforces the radius — there the distance is known.

The ETA in case 1 is prep time only, with no travel time, so it reads low.
It is the only honest number available without a route.

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
