CREATE SEQUENCE IF NOT EXISTS orders_order_number_seq
    AS BIGINT
    START WITH 10901
    INCREMENT BY 1
    NO CYCLE;

ALTER TABLE orders
    ALTER COLUMN order_number SET DEFAULT nextval('orders_order_number_seq'::regclass);

LOCK TABLE orders IN SHARE ROW EXCLUSIVE MODE;

SELECT setval(
    'orders_order_number_seq',
    GREATEST(
        COALESCE((SELECT MAX(order_number) FROM orders WHERE order_number > 0), 10900),
        10900
    ),
    true
);

UPDATE orders
SET order_number = nextval('orders_order_number_seq'::regclass)
WHERE order_number IS NULL OR order_number <= 0;

SELECT setval(
    'orders_order_number_seq',
    GREATEST((SELECT MAX(order_number) FROM orders), 10900),
    true
);

ALTER TABLE orders
    ALTER COLUMN order_number SET NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS ux_orders_order_number
    ON orders (order_number);

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conrelid = 'orders'::regclass
          AND conname = 'ck_orders_order_number_positive'
    ) THEN
        ALTER TABLE orders
            ADD CONSTRAINT ck_orders_order_number_positive
            CHECK (order_number > 0);
    END IF;
END
$$;
