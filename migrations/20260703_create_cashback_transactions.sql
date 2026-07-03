BEGIN;

CREATE TABLE IF NOT EXISTS cashback_transactions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    customer_id UUID NOT NULL REFERENCES customers(id),
    restaurant_id UUID NULL REFERENCES restaurants(id),
    order_id UUID NULL REFERENCES orders(id),
    type TEXT NOT NULL,
    amount NUMERIC(10, 2) NOT NULL,
    status TEXT NOT NULL,
    expires_at TIMESTAMPTZ NULL,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    idempotency_key TEXT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ck_cashback_transactions_type
        CHECK (type IN ('earned', 'redeemed', 'expired', 'cancelled', 'adjustment')),
    CONSTRAINT ck_cashback_transactions_status
        CHECK (status IN ('pending', 'available', 'used', 'cancelled', 'expired')),
    CONSTRAINT uq_cashback_transactions_idempotency_key UNIQUE (idempotency_key)
);

CREATE INDEX IF NOT EXISTS ix_cashback_transactions_customer_id
    ON cashback_transactions (customer_id);

CREATE INDEX IF NOT EXISTS ix_cashback_transactions_created_at
    ON cashback_transactions (created_at);

CREATE OR REPLACE FUNCTION prevent_cashback_transaction_mutation()
RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'cashback_transactions is an immutable ledger';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS cashback_transactions_immutable ON cashback_transactions;
CREATE TRIGGER cashback_transactions_immutable
BEFORE UPDATE OR DELETE ON cashback_transactions
FOR EACH ROW EXECUTE FUNCTION prevent_cashback_transaction_mutation();

COMMIT;
