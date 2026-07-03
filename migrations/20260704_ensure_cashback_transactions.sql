BEGIN;

CREATE TABLE IF NOT EXISTS cashback_transactions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    customer_id UUID NOT NULL,
    restaurant_id UUID NULL,
    order_id UUID NULL,
    type TEXT NOT NULL,
    amount NUMERIC(10, 2) NOT NULL,
    status TEXT NOT NULL,
    expires_at TIMESTAMPTZ NULL,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    idempotency_key TEXT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Recreate the foreign keys so this migration also repairs an older table
-- created without the required ON DELETE behavior.
ALTER TABLE cashback_transactions
    DROP CONSTRAINT IF EXISTS cashback_transactions_customer_id_fkey,
    DROP CONSTRAINT IF EXISTS cashback_transactions_restaurant_id_fkey,
    DROP CONSTRAINT IF EXISTS cashback_transactions_order_id_fkey;

ALTER TABLE cashback_transactions
    ADD CONSTRAINT cashback_transactions_customer_id_fkey
        FOREIGN KEY (customer_id) REFERENCES customers(id) ON DELETE CASCADE,
    ADD CONSTRAINT cashback_transactions_restaurant_id_fkey
        FOREIGN KEY (restaurant_id) REFERENCES restaurants(id) ON DELETE SET NULL,
    ADD CONSTRAINT cashback_transactions_order_id_fkey
        FOREIGN KEY (order_id) REFERENCES orders(id) ON DELETE SET NULL;

-- An older draft used a full unique constraint. PostgreSQL permits multiple
-- NULL values there, but the requested partial index makes the intent explicit.
ALTER TABLE cashback_transactions
    DROP CONSTRAINT IF EXISTS uq_cashback_transactions_idempotency_key;

CREATE INDEX IF NOT EXISTS ix_cashback_transactions_customer_id
    ON cashback_transactions (customer_id);

CREATE INDEX IF NOT EXISTS ix_cashback_transactions_customer_id_status
    ON cashback_transactions (customer_id, status);

CREATE INDEX IF NOT EXISTS ix_cashback_transactions_created_at
    ON cashback_transactions (created_at);

CREATE UNIQUE INDEX IF NOT EXISTS ux_cashback_transactions_idempotency_key
    ON cashback_transactions (idempotency_key)
    WHERE idempotency_key IS NOT NULL;

-- Remove the trigger from an earlier draft. It prevented the ON DELETE actions
-- above from working and is not needed by this read-only MVP endpoint.
DROP TRIGGER IF EXISTS cashback_transactions_immutable ON cashback_transactions;
DROP FUNCTION IF EXISTS prevent_cashback_transaction_mutation();

COMMIT;
