# Database Schema

This directory contains the PostgreSQL schema bootstrap for storing resilience test runs in AWS RDS PostgreSQL or any compatible PostgreSQL instance.

## Files

- `001_init_resilience_schema.sql`
  - creates the `resilience` schema
  - creates the enum types used by the run model
  - creates the six core tables:
    - `test_run`
    - `test_action`
    - `impacted_resource`
    - `execution_artifact`
    - `validation_result`
    - `metric_series`
  - creates the recommended indexes for common lookups

## Data Model Summary

The schema is built around one top-level run row in `resilience.test_run`.

- `test_run`
  - one row per `python main.py --manifest ...` execution
- `test_action`
  - one row per service action inside the manifest
- `impacted_resource`
  - resources selected or reported as impacted
- `execution_artifact`
  - references to generated JSON, HTML, and uploaded artifacts
- `validation_result`
  - optional validation outcomes when validation is enabled
- `metric_series`
  - optional CloudWatch or health-check time-series samples

The design intentionally keeps artifact storage flexible:

- store normalized, queryable metadata in PostgreSQL
- store large JSON/HTML artifacts in object storage if needed
- save only URLs, paths, checksums, or compact JSON summaries in the database

## Apply the Schema

Example:

```powershell
psql "host=<host> dbname=<db> user=<user> password=<password> sslmode=require" -f db/001_init_resilience_schema.sql
```

## Notes

- `pgcrypto` is enabled for `gen_random_uuid()`.
- JSON-heavy fields use `JSONB` so the schema can evolve without constant DDL changes.
- Status values are normalized with enums to keep run/action state consistent with the framework.
- `metric_series` is optional in practice. If you want a lean first rollout, you can create the full schema now and simply defer writing into that table until later.
