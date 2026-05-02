CREATE TABLE IF NOT EXISTS resilience.rollback_state (
    rollback_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id UUID NOT NULL REFERENCES resilience.test_run(run_id) ON DELETE CASCADE,
    action_id UUID NOT NULL REFERENCES resilience.test_action(action_id) ON DELETE CASCADE,
    rollback_mode TEXT NOT NULL,
    rollback_supported BOOLEAN NOT NULL DEFAULT FALSE,
    before_state_json JSONB,
    after_state_json JSONB,
    rollback_plan_json JSONB,
    rollback_status TEXT,
    rollback_reason TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    rolled_back_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_rollback_state_run_id
    ON resilience.rollback_state (run_id);

CREATE INDEX IF NOT EXISTS idx_rollback_state_action_id
    ON resilience.rollback_state (action_id);

CREATE INDEX IF NOT EXISTS idx_rollback_state_supported
    ON resilience.rollback_state (run_id, rollback_supported);
