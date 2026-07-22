BEGIN;

ALTER TABLE restaurant_coupons
    ADD COLUMN IF NOT EXISTS title text,
    ADD COLUMN IF NOT EXISTS description text,
    ADD COLUMN IF NOT EXISTS discount_type text,
    ADD COLUMN IF NOT EXISTS discount_value numeric(12, 2),
    ADD COLUMN IF NOT EXISTS max_discount_amount numeric(12, 2),
    ADD COLUMN IF NOT EXISTS valid_from timestamptz,
    ADD COLUMN IF NOT EXISTS valid_until timestamptz,
    ADD COLUMN IF NOT EXISTS total_usage_limit integer,
    ADD COLUMN IF NOT EXISTS usage_limit_per_customer integer,
    ADD COLUMN IF NOT EXISTS first_order_only boolean NOT NULL DEFAULT false,
    ADD COLUMN IF NOT EXISTS is_public boolean NOT NULL DEFAULT true;

UPDATE restaurant_coupons AS coupon
SET title = COALESCE(coupon.title, template.name),
    discount_type = COALESCE(coupon.discount_type, template.discount_type),
    discount_value = COALESCE(coupon.discount_value, template.discount_value, 0),
    valid_from = COALESCE(coupon.valid_from, coupon.created_at, now()),
    valid_until = COALESCE(coupon.valid_until, now() + interval '1 year'),
    min_order_value = COALESCE(coupon.min_order_value, 0)
FROM coupon_templates AS template
WHERE template.id = coupon.coupon_template_id;

ALTER TABLE restaurant_coupons
    ALTER COLUMN title SET NOT NULL,
    ALTER COLUMN discount_type SET NOT NULL,
    ALTER COLUMN discount_value SET NOT NULL,
    ALTER COLUMN discount_value SET DEFAULT 0,
    ALTER COLUMN min_order_value SET NOT NULL,
    ALTER COLUMN min_order_value SET DEFAULT 0,
    ALTER COLUMN valid_from SET NOT NULL,
    ALTER COLUMN valid_until SET NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS uq_restaurant_coupons_restaurant_code_ci
    ON restaurant_coupons (restaurant_id, lower(code));
CREATE INDEX IF NOT EXISTS ix_restaurant_coupons_public_window
    ON restaurant_coupons (restaurant_id, is_public, is_active, valid_from, valid_until);

ALTER TABLE orders
    ADD COLUMN IF NOT EXISTS coupon_id uuid REFERENCES restaurant_coupons(id),
    ADD COLUMN IF NOT EXISTS coupon_code_snapshot text,
    ADD COLUMN IF NOT EXISTS coupon_discount_amount numeric(12, 2) NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS cashback_redeemed_amount numeric(12, 2) NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS discount_total numeric(12, 2) NOT NULL DEFAULT 0;

CREATE TABLE IF NOT EXISTS coupon_redemptions (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    coupon_id uuid NOT NULL REFERENCES restaurant_coupons(id),
    customer_id uuid NOT NULL REFERENCES customers(id),
    order_id uuid NOT NULL REFERENCES orders(id),
    discount_amount numeric(12, 2) NOT NULL,
    status text NOT NULL DEFAULT 'applied',
    idempotency_key text NOT NULL,
    applied_at timestamptz NOT NULL DEFAULT now(),
    reversed_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ck_coupon_redemptions_status CHECK (status IN ('applied', 'reversed')),
    CONSTRAINT uq_coupon_redemptions_order_id UNIQUE (order_id),
    CONSTRAINT uq_coupon_redemptions_idempotency_key UNIQUE (idempotency_key)
);

CREATE INDEX IF NOT EXISTS ix_coupon_redemptions_coupon_applied
    ON coupon_redemptions (coupon_id, status);
CREATE INDEX IF NOT EXISTS ix_coupon_redemptions_coupon_customer_applied
    ON coupon_redemptions (coupon_id, customer_id, status);
CREATE UNIQUE INDEX IF NOT EXISTS uq_coupon_redemptions_order_id_idx
    ON coupon_redemptions (order_id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_coupon_redemptions_idempotency_key_idx
    ON coupon_redemptions (idempotency_key);

COMMIT;
