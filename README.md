# Resilience Testing Framework

This repository contains a manifest-driven resilience testing framework for AWS workloads.

It currently supports two execution models:

1. AWS Fault Injection Service (FIS) for component and site resilience tests
2. AWS ARC Region switch for regional Aurora Global Database failover and switchover tests

The project is designed so that a human engineer or another AI can continue the work with minimal re-discovery. This README is intended to be the main design handoff document for the current codebase.

## Goals

The framework is meant to:

1. Read a YAML manifest
2. Resolve the real AWS resources targeted by the test
3. Execute the test using the correct control plane
4. Collect observability signals before, during, and after the test
5. Persist machine-readable artifacts
6. Generate an HTML report

The current architecture deliberately separates:

- FIS-based infrastructure chaos actions
- ARC-based regional switch actions
- observability and reporting
- resource discovery

## High-Level Flows

### Component and Site Tests

Manifest:

- `resilience_test_type: component`
- `resilience_test_type: site`

Execution path:

1. `scripts/fis.py` loads the manifest
2. `scripts/fis_template_generator.py` delegates to `scripts/template_generator/`
3. A FIS experiment template payload is generated
4. `scripts/resource.py` discovers impacted resources
5. `scripts/observability.py` starts collectors
6. FIS template is created and the experiment is started
7. The experiment is polled until completion
8. Results are written to disk
9. `scripts/chart.py` generates an HTML report

### Region Tests

Manifest:

- `resilience_test_type: region`

Execution path:

1. `scripts/fis.py` detects `region` mode
2. `scripts/region_switch.py` validates the manifest
3. `scripts/resource.py` discovers the Aurora Global Database and member cluster ARNs from tags
4. `scripts/region_switch.py` builds an ARC Region switch plan payload
5. ARC plan is created and executed
6. Health-check observability runs around the switch window
7. Results are written to disk
8. `scripts/chart.py` generates an HTML report

Important design point:

- Region tests are not forced through the FIS template path.
- If an action is a native FIS action, it belongs in `scripts/template_generator/`.
- If an action is an ARC orchestration or another direct AWS control-plane workflow, it belongs in a separate execution path like `scripts/region_switch.py`.

## Repository Structure

### Core Orchestrator

- `scripts/fis.py`

Responsibilities:

- loads the manifest
- decides whether the run is FIS-based or ARC-based
- writes payload and result artifacts
- starts observability
- executes and polls the selected control plane
- triggers HTML report generation

### FIS Template Generation

- `scripts/fis_template_generator.py`
- `scripts/template_generator/__init__.py`
- `scripts/template_generator/registry.py`
- `scripts/template_generator/base.py`
- service-specific generator files under `scripts/template_generator/`

Responsibilities:

- translate supported manifest service/action pairs into FIS template targets and actions
- keep service-specific logic isolated in separate files
- preserve open-closed style extension so new services can be added with minimal changes

### ARC Region Switch

- `scripts/region_switch.py`

Responsibilities:

- validate region-switch manifests
- resolve Aurora Global Database targets from tags
- build ARC Region switch plan payloads
- create and start plan executions
- poll execution status
- summarize execution into the same result-file pattern used by reporting

### Resource Discovery

- `scripts/resource.py`

Responsibilities:

- discover impacted resources for component and site tests
- discover Aurora Global Database targets for region tests
- output `impacted_resources.json`

### Observability

- `scripts/observability.py`

Responsibilities:

- health check collector
- load balancer CloudWatch collector
- automatic CloudWatch metrics for some impacted resources

### Reporting

- `scripts/chart.py`

Responsibilities:

- read run artifacts from the output directory
- compute timeline and approximate SLO summary
- group charts by resource type
- generate a self-contained HTML report with base64-embedded images

### Shared Utilities

- `scripts/utility.py`

Responsibilities:

- YAML loading
- filename sanitization
- JSON formatting
- tag parsing
- CSV append helpers
- IAM role resolution
- Artifactory upload helpers

## Supported Capabilities

| Action | Description | FIS/ARC action | Allowed Values |
| --- | --- | --- | --- |
| `ec2:pause-launch` | Simulate insufficient EC2 capacity for instance launches in a site/AZ-scoped test. | `aws:ec2:api-insufficient-instance-capacity-error` | `pause-launch` |
| `ec2:stop` | Stop selected EC2 instances and restart them after the configured duration. | `aws:ec2:stop-instances` | `stop` |
| `ec2:reboot` | Reboot selected EC2 instances. | `aws:ec2:reboot-instances` | `reboot` |
| `ec2:terminate` | Terminate selected EC2 instances. | `aws:ec2:terminate-instances` | `terminate` |
| `rds:reboot` | Reboot selected RDS DB instances. | `aws:rds:reboot-db-instances` | `reboot` |
| `rds:failover` | Fail over a selected RDS or Aurora DB cluster to a replica. | `aws:rds:failover-db-cluster` | `failover` |
| `asg:pause-launch` | Simulate insufficient capacity for Auto Scaling launches in a site/AZ-scoped test. | `aws:ec2:asg-insufficient-instance-capacity-error` | `pause-launch` |
| `network:disrupt-connectivity` | Disrupt connectivity for selected subnets. | `aws:network:disrupt-connectivity` | `disrupt-connectivity` |
| `eks:delete-pod` | Delete selected EKS pods by namespace and selector. | `aws:eks:pod-delete` | `delete-pod` |
| `rds:failover-global-db` | Fail over an Aurora Global Database across Regions. Uses ARC when `use_arc: true`; otherwise uses a custom boto3 RDS implementation. | `AuroraGlobalDatabase` | `failover-global-db` |
| `rds:switchover-global-db` | Switchover an Aurora Global Database across Regions. Uses ARC when `use_arc: true`; otherwise uses a custom boto3 RDS implementation. | `AuroraGlobalDatabase` | `switchover-global-db` |

Current placeholder generator files still exist for `s3` and `efs`, but they are scaffolds only and do not currently define real actions.

## Manifest Design

### Component Example

See `manifests/component-1.yml`.

Example shape:

```yaml
resilience_test_type: component
region: ap-southeast-1
services:
- name: ec2
  action: terminate
  tags: environment=development,project=clouddash
  instance_count: 1
- name: rds
  action: reboot
  tags: environment=development,project=clouddash
- name: eks
  action: delete-pod
  target:
    cluster_identifier: my-eks-cluster
    namespace: default
    selector_type: labelSelector
    selector_value: app=my-service
    count: 1
  parameters:
    kubernetes_service_account: myserviceaccount
    grace_period_seconds: 0
    max_errors_percent: 0
observability:
  start_before: 2
  stop_after: 2
  health_check:
    endpoint: http://example
    http_method: get
    healthy_status_code: 200
    interval: 10
```

### Site Example

Expected additions relative to `component`:

- `resilience_test_type: site`
- `zone: <availability-zone>`

Site scoping is applied to targets where supported. The code already contains special handling for:

- EC2 instances
- subnets
- RDS clusters
- RDS DB instances
- Auto Scaling groups

### Region Example

See `manifests/geo-1.yml`.

Example shape:

```yaml
resilience_test_type: region
primary_region: ap-southeast-1
secondary_region: ap-southeast-2
services:
- name: rds
  action: failover-global-db
  tags: environment=development,project=clouddash
  from: primary
  use_arc: true
observability:
  start_before: 2
  stop_after: 2
  health_check:
    endpoint: http://example
    http_method: get
    healthy_status_code: 200
    interval: 10
```

Notes on region manifests:

- `from: primary` means switch away from the current primary Region into the secondary Region
- `from: secondary` means switch back into the primary Region
- tags are used for discovery; the user does not need to provide the Aurora Global Database ARN or member cluster ARNs
- `use_arc` only applies to `resilience_test_type: region`
- if `use_arc` is omitted, the current default behavior is `true`

## ARC Region Switch Design

The ARC path is intentionally separate from the FIS path.

### Why

Aurora Global Database failover and switchover are orchestrated using ARC Region switch rather than a native FIS action in this codebase. That keeps regional orchestration separate from FIS experiment-template generation and makes it easier to grow into more sophisticated region-recovery workflows later.

### Current Region Execution Implementation

The current region path in `scripts/region_switch.py` does the following:

1. validates the region manifest
2. discovers Aurora Global Database targets from tags
3. resolves:
   - one global cluster identifier
   - one member cluster ARN in `primary_region`
   - one member cluster ARN in `secondary_region`
4. builds a region execution plan
5. selects the engine per service block:
   - ARC when `use_arc = true`
   - non-ARC custom execution when `use_arc = false`
6. runs the selected engine and writes a unified summary for reporting

### ARC Path

When `use_arc = true`, the code:

1. creates an ARC plan with:
   - `recoveryApproach = activePassive`
   - one workflow
   - one Aurora Global Database step
2. starts execution with:
   - `activate`
   - `ungraceful` for `failover-global-db`
   - `graceful` for `switchover-global-db`

### Non-ARC Path

When `use_arc = false`, the code currently uses direct boto3 RDS APIs:

- `rds.failover_global_cluster()` for `failover-global-db`
- `rds.switchover_global_cluster()` for `switchover-global-db`

This exists because native FIS support for Aurora Global Database failover and switchover is not assumed in this codebase. The framework is designed so a future region action can choose ARC or a different custom implementation without changing the manifest contract.

### Current Discovery Rules for Aurora Global Database

`scripts/resource.py` currently expects:

- exactly one matching Aurora Global Database per service block
- exactly one member cluster in each configured Region

If discovery returns:

- zero matches: the run fails
- multiple global databases: the run fails
- more than one member cluster in a configured Region: the run fails

This is deliberate. The current design prefers deterministic failure over ambiguous regional failover.

### Observability Choice for Region Tests

For region tests, `scripts/fis.py` currently passes an empty impacted-resource list into `start_observability_collectors()`.

Why:

- the current automatic CloudWatch resource collector is single-Region
- region tests can involve resources in both primary and secondary Regions
- querying secondary-region RDS cluster metrics with a primary-region CloudWatch client would be wrong

Current result:

- health-check observability still works
- automatic cross-Region CloudWatch resource metrics are intentionally not enabled yet

## Template Generator Design

The original monolithic `fis_template_generator.py` was split into a package.

### Files

- `scripts/template_generator/base.py`
- `scripts/template_generator/registry.py`
- one `*_template_generator.py` per service

### Design Rules

1. Service-specific logic belongs in the service file.
2. Shared mechanics belong in the base or registry layer.
3. `scripts/fis_template_generator.py` remains a compatibility wrapper so callers do not need to change.

### How New FIS Service Support Should Be Added

If the action is a true FIS action:

1. create or update a service file under `scripts/template_generator/`
2. define the `service_name`
3. define `action_map`
4. define `target_spec_map`
5. override any of:
   - `get_selection_mode()`
   - `get_resource_arns()`
   - `get_target_parameters()`
   - `build_action_parameters()`

Because the registry dynamically imports `*_template_generator.py` files, new service generators can be discovered without re-growing the old monolith.

### Example: EKS Pod Delete

`scripts/template_generator/eks_template_generator.py` uses nested manifest blocks:

- `target`
- `parameters`

This pattern is useful for future service actions that require structured target or action parameters rather than plain tags.

## Resource Discovery Design

`scripts/resource.py` serves two roles:

1. discover impacted resources for reporting
2. provide resolvable AWS target details for execution flows

### Existing Component and Site Discovery

Supported today:

- EC2 instances
- subnets
- RDS DB instances
- RDS clusters
- Auto Scaling groups
- IAM roles for `ec2:pause-launch`

### Existing Region Discovery

Supported today:

- Aurora Global Database by tags

The region discovery logic:

- scans global clusters
- narrows to member clusters in `primary_region` and `secondary_region`
- checks tags on the global cluster or the member clusters
- returns resolved target data used by ARC plan creation

## Observability Design

`scripts/observability.py` currently contains:

- HTTP health check collector
- load balancer CloudWatch collector
- automatic CloudWatch collector for some impacted ASG and RDS resources

### Important Note

The auto-collection mapping lives in `SERVICE_CLOUDWATCH_METRICS_MAP`.

Today it supports:

- `asg`
- `rds:db`
- `rds:cluster`

CloudWatch stat is still hardcoded to `Sum` for all metrics. This is a known simplification and a natural future improvement point.

## Reporting Design

`scripts/chart.py` scans the output directory and uses:

- `result_*.json`
- `impacted_resources.json`
- CSV files

It produces:

- experiment timeline
- SLO summary
- impacted resource summary
- grouped charts
- HTML report

Current grouping behavior:

- `health_check.csv` -> Health Check
- `asg_*` -> Auto Scaling
- `rds_db_*` -> RDS DB
- `rds_cluster_*` -> RDS Cluster
- other `<prefix>_*` -> Load Balancer: `<prefix>`

The report currently expects a `result_*.json` file with:

- `experimentId`
- `experimentTemplateId`
- `status`
- `reason`
- `startTime`
- `endTime`
- `actions`

Both FIS and ARC flows write their summaries in this general shape so reporting can stay mostly shared.

## Output Files

A typical run creates some or all of:

- `template_payload_<name>.json`
- `region_execution_plan_<name>.json`
- `impacted_resources.json`
- `result_<name>.json`
- `health_check.csv`
- `<metric>.csv`
- `report_<name>_<yyyymmdd>.html`

## How to Run

### Default Arguments from `.env`

The repository root can contain a `.env` file with default values for `scripts/fis.py`.

Current supported keys:

- `MANIFEST`
- `FIS_ROLE_ARN`
- `ARC_ROLE_ARN`
- `OUTDIR`
- `POLL_SECONDS`
- `TIMEOUT_SECONDS`
- `UPLOAD_ARTIFACTORY`

Behavior:

- if a CLI argument is provided, it wins
- if a CLI argument is omitted, `fis.py` falls back to `.env`
- if the key is not present in `.env`, `fis.py` falls back to its hardcoded default

### Install Dependencies

Dependencies are listed in `scripts/requirements.txt`.

Typical install:

```powershell
pip install -r scripts/requirements.txt
```

### Run a Component or Site Test

From `scripts/`:

```powershell
python fis.py --manifest ..\manifests\component-1.yml --fis-role-arn <fis-role-arn>
```

### Run a Region Test

From `scripts/`:

```powershell
python fis.py --manifest ..\manifests\geo-1.yml --arc-role-arn <arc-role-arn>
```

### Dry Run

Dry run writes payload and discovery artifacts but does not create or execute the remote action:

```powershell
python fis.py --manifest ..\manifests\geo-1.yml --arc-role-arn <arc-role-arn> --dry-run
```

## Extension Guidance

### When Adding a New FIS Action

Use the `scripts/template_generator/` package.

Do not re-centralize service logic back into a single monolithic file.

### When Adding a New Region or Control-Plane Orchestration

Follow the ARC pattern:

- keep manifest validation close to the execution code
- keep discovery in `scripts/resource.py` if it is reusable
- keep orchestration logic separate from FIS template generation

### When Updating Reporting

Try to preserve the `result_*.json` contract so `scripts/chart.py` can continue to work across execution modes.

### When Updating Observability

Be careful about Region assumptions. The current collectors are mostly single-Region.

## Known Limitations

These are important if you continue the work:

1. The ARC region-switch path was implemented from the code and AWS API design, but it has not been live-validated in this workspace because local Python execution and AWS access were not available in the sandbox.
2. Cross-Region CloudWatch auto-collection is not implemented yet.
3. The current Aurora Global Database discovery is intentionally strict and fails on ambiguous matches.
4. `eks:delete-pod` exists in template generation, but EKS impacted-resource discovery and EKS-specific observability are not implemented.
5. `s3` and `efs` generator files are placeholders only.
6. There is no dedicated test suite in the repository yet.

## Recommended Next Improvements

Natural next steps for the project:

1. Add a real test harness for manifest validation and payload generation.
2. Add live integration validation for ARC Region switch payloads.
3. Implement cross-Region observability for region tests.
4. Add more region-switch workflows beyond Aurora Global Database.
5. Replace hardcoded CloudWatch stat selection with per-metric stat metadata.
6. Add a top-level historical dashboard or report index.

## Development Style Expectations

The codebase has been evolving with these principles:

1. Keep changes surgical and easy to review.
2. Preserve working flows unless there is a strong reason to refactor.
3. Prefer one service file per service when extending FIS actions.
4. Prefer separate execution modules for non-FIS orchestration.
5. Treat manifests as the user-facing contract and keep discovery logic inside Python.

If another engineer or AI continues this repo, the safest default is:

- preserve the split between FIS and ARC flows
- keep resource discovery explicit and deterministic
- keep result-file structure stable so reporting continues to work
