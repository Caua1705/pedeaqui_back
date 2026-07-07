BEGIN;

ALTER TABLE branch_business_hours
    ADD COLUMN IF NOT EXISTS prep_time_min INTEGER,
    ADD COLUMN IF NOT EXISTS prep_time_max INTEGER;

ALTER TABLE orders
    ADD COLUMN IF NOT EXISTS delivery_prep_time_max INTEGER;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'branch_business_hours_prep_time_min_nonnegative'
          AND conrelid = 'branch_business_hours'::regclass
    ) THEN
        ALTER TABLE branch_business_hours
            ADD CONSTRAINT branch_business_hours_prep_time_min_nonnegative
            CHECK (prep_time_min IS NULL OR prep_time_min >= 0);
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'branch_business_hours_prep_time_max_gte_min'
          AND conrelid = 'branch_business_hours'::regclass
    ) THEN
        ALTER TABLE branch_business_hours
            ADD CONSTRAINT branch_business_hours_prep_time_max_gte_min
            CHECK (
                prep_time_min IS NULL
                OR prep_time_max IS NULL
                OR prep_time_max >= prep_time_min
            );
    END IF;
END $$;

ALTER TABLE branches
    DROP CONSTRAINT IF EXISTS branches_prep_time_min_nonnegative,
    DROP CONSTRAINT IF EXISTS branches_prep_time_max_gte_min,
    DROP COLUMN IF EXISTS prep_time_min,
    DROP COLUMN IF EXISTS prep_time_max;

COMMIT;
