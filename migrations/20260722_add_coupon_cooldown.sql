BEGIN;

ALTER TABLE restaurant_coupons
    ADD COLUMN IF NOT EXISTS cooldown_days integer;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'ck_restaurant_coupons_cooldown_days_positive'
    ) THEN
        ALTER TABLE restaurant_coupons
            ADD CONSTRAINT ck_restaurant_coupons_cooldown_days_positive
            CHECK (cooldown_days IS NULL OR cooldown_days > 0);
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS ix_coupon_redemptions_last_applied_customer
    ON coupon_redemptions (coupon_id, customer_id, applied_at DESC)
    WHERE status = 'applied';

COMMIT;
