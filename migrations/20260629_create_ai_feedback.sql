CREATE TABLE IF NOT EXISTS ai_feedback (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    restaurant_id UUID NOT NULL REFERENCES restaurants(id),
    session_id TEXT NOT NULL,
    user_message TEXT NOT NULL,
    assistant_message TEXT NOT NULL,
    response_type TEXT NOT NULL,
    selected_product_ids UUID[] NOT NULL,
    feedback TEXT NOT NULL CHECK (feedback IN ('like', 'dislike')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
