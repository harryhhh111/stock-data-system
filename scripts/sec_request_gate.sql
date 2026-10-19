-- Additive migration; run only against the US coordination database.
-- Re-running must not reset an active reservation or cooldown.
BEGIN;
CREATE TABLE IF NOT EXISTS public.sec_request_gate (
    gate_key text PRIMARY KEY CHECK (gate_key = 'sec_edgar'),
    next_allowed_at timestamptz NOT NULL,
    blocked_until timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    CHECK (isfinite(next_allowed_at) AND isfinite(blocked_until)
           AND isfinite(updated_at))
);
INSERT INTO public.sec_request_gate VALUES
    ('sec_edgar', clock_timestamp(), clock_timestamp(), clock_timestamp())
ON CONFLICT (gate_key) DO NOTHING;
COMMIT;
