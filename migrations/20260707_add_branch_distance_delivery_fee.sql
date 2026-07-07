BEGIN;

ALTER TABLE branches
    ADD COLUMN IF NOT EXISTS delivery_base_fee NUMERIC,
    ADD COLUMN IF NOT EXISTS delivery_fee_per_km NUMERIC,
    ADD COLUMN IF NOT EXISTS delivery_min_fee NUMERIC,
    ADD COLUMN IF NOT EXISTS delivery_max_fee NUMERIC,
    ADD COLUMN IF NOT EXISTS delivery_max_distance_km NUMERIC;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'branches_delivery_fee_values_nonnegative'
          AND conrelid = 'branches'::regclass
    ) THEN
        ALTER TABLE branches
            ADD CONSTRAINT branches_delivery_fee_values_nonnegative
            CHECK (
                (delivery_base_fee IS NULL OR delivery_base_fee >= 0)
                AND (delivery_fee_per_km IS NULL OR delivery_fee_per_km >= 0)
                AND (delivery_min_fee IS NULL OR delivery_min_fee >= 0)
                AND (delivery_max_fee IS NULL OR delivery_max_fee >= 0)
                AND (delivery_max_distance_km IS NULL OR delivery_max_distance_km >= 0)
            );
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'branches_delivery_max_fee_gte_min_fee'
          AND conrelid = 'branches'::regclass
    ) THEN
        ALTER TABLE branches
            ADD CONSTRAINT branches_delivery_max_fee_gte_min_fee
            CHECK (
                delivery_min_fee IS NULL
                OR delivery_max_fee IS NULL
                OR delivery_max_fee >= delivery_min_fee
            );
    END IF;
END $$;

COMMIT;
