BEGIN;

ALTER TABLE customer_addresses
    ADD COLUMN IF NOT EXISTS client_reference TEXT;

-- Preserve one existing default per customer before enforcing uniqueness.
WITH ranked_defaults AS (
    SELECT
        id,
        ROW_NUMBER() OVER (
            PARTITION BY customer_id
            ORDER BY created_at DESC NULLS LAST, id
        ) AS position
    FROM customer_addresses
    WHERE is_default IS TRUE
)
UPDATE customer_addresses AS address
SET is_default = FALSE
FROM ranked_defaults
WHERE address.id = ranked_defaults.id
  AND ranked_defaults.position > 1;

-- Customers with addresses but no default receive their most recent address.
WITH customers_without_default AS (
    SELECT customer_id
    FROM customer_addresses
    GROUP BY customer_id
    HAVING BOOL_OR(is_default) IS FALSE
),
ranked_addresses AS (
    SELECT
        address.id,
        ROW_NUMBER() OVER (
            PARTITION BY address.customer_id
            ORDER BY address.created_at DESC NULLS LAST, address.id
        ) AS position
    FROM customer_addresses AS address
    JOIN customers_without_default USING (customer_id)
)
UPDATE customer_addresses AS address
SET is_default = TRUE
FROM ranked_addresses
WHERE address.id = ranked_addresses.id
  AND ranked_addresses.position = 1;

CREATE UNIQUE INDEX IF NOT EXISTS ux_customer_addresses_one_default
    ON customer_addresses (customer_id)
    WHERE is_default IS TRUE;

CREATE UNIQUE INDEX IF NOT EXISTS ux_customer_addresses_client_reference
    ON customer_addresses (customer_id, client_reference)
    WHERE client_reference IS NOT NULL;

ALTER TABLE orders
    ALTER COLUMN customer_address_id DROP NOT NULL;

DO $$
DECLARE
    existing_constraint TEXT;
BEGIN
    SELECT constraint_row.conname
    INTO existing_constraint
    FROM pg_constraint AS constraint_row
    JOIN pg_class AS table_row
      ON table_row.oid = constraint_row.conrelid
    JOIN pg_attribute AS column_row
      ON column_row.attrelid = table_row.oid
     AND column_row.attnum = ANY (constraint_row.conkey)
    WHERE table_row.relname = 'orders'
      AND constraint_row.contype = 'f'
      AND column_row.attname = 'customer_address_id'
    LIMIT 1;

    IF existing_constraint IS NOT NULL THEN
        EXECUTE format(
            'ALTER TABLE orders DROP CONSTRAINT %I',
            existing_constraint
        );
    END IF;

    ALTER TABLE orders
        ADD CONSTRAINT fk_orders_customer_address
        FOREIGN KEY (customer_address_id)
        REFERENCES customer_addresses(id)
        ON DELETE SET NULL;
END
$$;

COMMIT;
