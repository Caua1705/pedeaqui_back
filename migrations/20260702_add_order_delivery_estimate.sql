BEGIN;

ALTER TABLE orders
    ADD COLUMN IF NOT EXISTS address_city TEXT,
    ADD COLUMN IF NOT EXISTS address_state TEXT,
    ADD COLUMN IF NOT EXISTS address_zipcode TEXT,
    ADD COLUMN IF NOT EXISTS delivery_latitude NUMERIC(10, 7),
    ADD COLUMN IF NOT EXISTS delivery_longitude NUMERIC(10, 7),
    ADD COLUMN IF NOT EXISTS delivery_distance_km NUMERIC(10, 2),
    ADD COLUMN IF NOT EXISTS delivery_travel_time_min INTEGER,
    ADD COLUMN IF NOT EXISTS delivery_prep_time_min INTEGER,
    ADD COLUMN IF NOT EXISTS delivery_eta_min INTEGER,
    ADD COLUMN IF NOT EXISTS delivery_eta_max INTEGER,
    ADD COLUMN IF NOT EXISTS delivery_estimate_provider TEXT,
    ADD COLUMN IF NOT EXISTS delivery_estimated_at TIMESTAMPTZ;

COMMIT;
