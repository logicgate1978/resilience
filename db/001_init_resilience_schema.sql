CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE SCHEMA IF NOT EXISTS resilience;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'engine_family_enum') THEN
        CREATE TYPE resilience.engine_family_enum AS ENUM ('fis', 'arc', 'custom');
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'run_status_enum') THEN
        CREATE TYPE resilience.run_status_enum AS ENUM ('running', 'completed', 'failed', 'stopped', 'skipped');
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'artifact_type_enum') THEN
        CREATE TYPE resilience.artifact_type_enum AS ENUM (
            'manifest',
            'impacted_resources',
            'fis_template',
            'custom_execution_plan',
            'region_execution_plan',
            'result_summary',
            'html_report',
            'observability_summary',
            'other'
        );
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'validation_status_enum') THEN
        CREATE TYPE resilience.validation_status_enum AS ENUM ('passed', 'failed', 'skipped');
    END IF;
END
$$;

CREATE TABLE IF NOT EXISTS resilience.test_run (
    run_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_name TEXT,
    status resilience.run_status_enum NOT NULL DEFAULT 'running',
    engine_family resilience.engine_family_enum NOT NULL,
    manifest_path TEXT NOT NULL,
    manifest_sha256 TEXT,
    manifest_yaml TEXT,
    manifest_json JSONB,
    dry_run BOOLEAN NOT NULL DEFAULT FALSE,
    skip_validation BOOLEAN NOT NULL DEFAULT FALSE,
    region_context JSONB,
    initiated_by TEXT,
    host_name TEXT,
    host_ip TEXT,
    git_commit TEXT,
    git_branch TEXT,
    git_dirty BOOLEAN,
    runner_version TEXT,
    report_path TEXT,
    report_url TEXT,
    notes TEXT,
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ended_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (manifest_sha256 IS NULL OR char_length(manifest_sha256) >= 32),
    CHECK (ended_at IS NULL OR ended_at >= started_at)
);

COMMENT ON TABLE resilience.test_run IS 'One logical resilience test execution started from scripts/main.py.';
COMMENT ON COLUMN resilience.test_run.region_context IS 'Resolved top-level/service-level region and zone context captured as JSON.';

CREATE TABLE IF NOT EXISTS resilience.test_action (
    action_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id UUID NOT NULL REFERENCES resilience.test_run(run_id) ON DELETE CASCADE,
    sequence_no INTEGER NOT NULL,
    action_ref TEXT,
    service_name TEXT NOT NULL,
    action_name TEXT NOT NULL,
    engine_family resilience.engine_family_enum NOT NULL,
    start_after TEXT[],
    requested_region TEXT,
    requested_zone TEXT,
    status resilience.run_status_enum NOT NULL DEFAULT 'running',
    reason TEXT,
    started_at TIMESTAMPTZ,
    ended_at TIMESTAMPTZ,
    service_config_json JSONB NOT NULL DEFAULT '{}'::JSONB,
    execution_target_json JSONB,
    result_json JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (sequence_no > 0),
    CHECK (ended_at IS NULL OR started_at IS NULL OR ended_at >= started_at),
    UNIQUE (run_id, sequence_no)
);

COMMENT ON TABLE resilience.test_action IS 'One service action inside a manifest run.';
COMMENT ON COLUMN resilience.test_action.action_ref IS 'Optional stable action reference such as ec2:stop#1.';

CREATE TABLE IF NOT EXISTS resilience.impacted_resource (
    resource_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id UUID NOT NULL REFERENCES resilience.test_run(run_id) ON DELETE CASCADE,
    action_id UUID REFERENCES resilience.test_action(action_id) ON DELETE CASCADE,
    service_action TEXT NOT NULL,
    resource_arn TEXT NOT NULL,
    resource_type TEXT,
    selection_mode TEXT,
    resource_region TEXT,
    resource_metadata JSONB NOT NULL DEFAULT '{}'::JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE resilience.impacted_resource IS 'Resources selected or reported as impacted by a run or specific action.';

CREATE TABLE IF NOT EXISTS resilience.execution_artifact (
    artifact_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id UUID NOT NULL REFERENCES resilience.test_run(run_id) ON DELETE CASCADE,
    action_id UUID REFERENCES resilience.test_action(action_id) ON DELETE CASCADE,
    artifact_type resilience.artifact_type_enum NOT NULL,
    local_path TEXT,
    object_url TEXT,
    content_sha256 TEXT,
    content_json JSONB,
    file_size_bytes BIGINT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (file_size_bytes IS NULL OR file_size_bytes >= 0),
    CHECK (content_sha256 IS NULL OR char_length(content_sha256) >= 32)
);

COMMENT ON TABLE resilience.execution_artifact IS 'Pointers to generated JSON, HTML, or uploaded artifacts for a run.';

CREATE TABLE IF NOT EXISTS resilience.validation_result (
    validation_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id UUID NOT NULL REFERENCES resilience.test_run(run_id) ON DELETE CASCADE,
    action_id UUID REFERENCES resilience.test_action(action_id) ON DELETE CASCADE,
    validation_name TEXT NOT NULL,
    status resilience.validation_status_enum NOT NULL,
    message TEXT,
    details_json JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE resilience.validation_result IS 'Outcome of pre-execution validations when validation is enabled.';

CREATE TABLE IF NOT EXISTS resilience.metric_series (
    metric_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id UUID NOT NULL REFERENCES resilience.test_run(run_id) ON DELETE CASCADE,
    action_id UUID REFERENCES resilience.test_action(action_id) ON DELETE CASCADE,
    resource_arn TEXT,
    namespace TEXT NOT NULL,
    metric_name TEXT NOT NULL,
    stat TEXT NOT NULL,
    unit TEXT,
    observed_at TIMESTAMPTZ NOT NULL,
    value DOUBLE PRECISION NOT NULL,
    dimensions_json JSONB NOT NULL DEFAULT '{}'::JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE resilience.metric_series IS 'Optional metric snapshots or time-series samples collected around a run.';

CREATE INDEX IF NOT EXISTS idx_test_run_started_at
    ON resilience.test_run (started_at DESC);

CREATE INDEX IF NOT EXISTS idx_test_run_status
    ON resilience.test_run (status, started_at DESC);

CREATE INDEX IF NOT EXISTS idx_test_run_engine_family
    ON resilience.test_run (engine_family, started_at DESC);

CREATE INDEX IF NOT EXISTS idx_test_run_manifest_sha256
    ON resilience.test_run (manifest_sha256);

CREATE INDEX IF NOT EXISTS idx_test_run_manifest_json_gin
    ON resilience.test_run USING GIN (manifest_json);

CREATE INDEX IF NOT EXISTS idx_test_action_run_id
    ON resilience.test_action (run_id, sequence_no);

CREATE INDEX IF NOT EXISTS idx_test_action_service_action
    ON resilience.test_action (service_name, action_name, started_at DESC);

CREATE INDEX IF NOT EXISTS idx_test_action_status
    ON resilience.test_action (status, started_at DESC);

CREATE INDEX IF NOT EXISTS idx_test_action_service_config_gin
    ON resilience.test_action USING GIN (service_config_json);

CREATE INDEX IF NOT EXISTS idx_impacted_resource_run_id
    ON resilience.impacted_resource (run_id);

CREATE INDEX IF NOT EXISTS idx_impacted_resource_action_id
    ON resilience.impacted_resource (action_id);

CREATE INDEX IF NOT EXISTS idx_impacted_resource_arn
    ON resilience.impacted_resource (resource_arn);

CREATE INDEX IF NOT EXISTS idx_impacted_resource_type_region
    ON resilience.impacted_resource (resource_type, resource_region);

CREATE INDEX IF NOT EXISTS idx_execution_artifact_run_id
    ON resilience.execution_artifact (run_id, artifact_type);

CREATE INDEX IF NOT EXISTS idx_execution_artifact_action_id
    ON resilience.execution_artifact (action_id);

CREATE INDEX IF NOT EXISTS idx_execution_artifact_content_json_gin
    ON resilience.execution_artifact USING GIN (content_json);

CREATE INDEX IF NOT EXISTS idx_validation_result_run_id
    ON resilience.validation_result (run_id, status);

CREATE INDEX IF NOT EXISTS idx_validation_result_action_id
    ON resilience.validation_result (action_id);

CREATE INDEX IF NOT EXISTS idx_metric_series_run_time
    ON resilience.metric_series (run_id, observed_at);

CREATE INDEX IF NOT EXISTS idx_metric_series_action_time
    ON resilience.metric_series (action_id, observed_at);

CREATE INDEX IF NOT EXISTS idx_metric_series_metric_lookup
    ON resilience.metric_series (namespace, metric_name, observed_at DESC);

CREATE INDEX IF NOT EXISTS idx_metric_series_dimensions_gin
    ON resilience.metric_series USING GIN (dimensions_json);
