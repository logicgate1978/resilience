# Resilience Testing Framework

This repository contains a manifest-driven resilience testing framework for AWS workloads.

It currently supports three execution models:

1. AWS Fault Injection Service (FIS) for native FIS-backed actions
2. AWS ARC Region switch for Aurora Global Database failover and switchover when `service.use_arc: true`
3. Custom actions for behaviors that are not available as native FIS actions, or for supported `use_fis: false` / `use_arc: false` fallbacks

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
- ARC-based Aurora Global Database actions
- custom actions
- observability and reporting
- resource discovery

Important manifest rule:

- one manifest can contain many actions, but all actions in that manifest must resolve to the same engine family: FIS, ARC, or custom
- the framework does not support mixing FIS, ARC, and custom implementations in a single manifest

## High-Level Flows

### FIS and Custom Workflows

Execution path:

1. `scripts/main.py` loads the manifest
2. The action is routed to either:
   - `scripts/template_generator/` for native FIS actions
   - `scripts/component_actions/` for custom actions and supported `use_fis: false` fallbacks
3. Impacted resources are written
4. `scripts/observability.py` starts collectors
5. The selected execution engine runs
6. Results are written to disk
7. `scripts/chart.py` generates an HTML report

Top-level and service-level scope:

- `region`, `zone`, `primary_region`, and `secondary_region` can be declared at the top level or on an individual service block
- service-level values take precedence over top-level defaults

### ARC Workflows

Execution path:

1. `scripts/main.py` detects that all actions in the manifest are ARC-backed
2. `scripts/region_switch.py` validates the manifest
3. `scripts/resource.py` discovers the impacted resources for reporting
4. `scripts/region_switch.py` builds a region execution plan
5. ARC runs the selected Aurora Global Database actions
6. Health-check observability runs around the switch window
7. Results are written to disk
8. `scripts/chart.py` generates an HTML report

Important design point:

- Region tests are not forced through the FIS template path.
- If an action is a native FIS action, it belongs in `scripts/template_generator/`.
- If an action is an ARC orchestration or another direct AWS control-plane workflow, it belongs in a separate execution path like `scripts/region_switch.py`.

## Repository Structure

### Core Orchestrator

- `scripts/main.py`

Responsibilities:

- loads the manifest
- decides whether the run is FIS-based, ARC-based, or custom-component based
- writes payload and result artifacts
- starts observability
- executes and polls the selected control plane
- triggers HTML report generation
- optionally persists run metadata, actions, artifacts, validations, and metrics into PostgreSQL when `--db-dsn` is configured

### Database Persistence

- `scripts/persistence/__init__.py`
- `scripts/persistence/postgres.py`
- `db/001_init_resilience_schema.sql`
- `db/README.md`

Responsibilities:

- create a `test_run` row when a run starts
- persist action plans and final action outcomes
- persist impacted resources and generated artifacts
- persist validation outcomes and observability metric samples
- keep database persistence best-effort so a DB outage does not block the resilience test run

### Custom Component Actions

- `scripts/component_actions/__init__.py`
- `scripts/component_actions/base.py`
- `scripts/component_actions/registry.py`
- `scripts/component_actions/eks.py`
- `scripts/component_actions/k8s_auth.py`

Responsibilities:

- define non-FIS component actions
- build custom execution plans
- execute custom actions and return report-compatible summaries
- connect to the Kubernetes API for EKS custom actions

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

The example columns below show compact service-block snippets so each action stays searchable in one row. They are not full manifest files.

<table>
  <thead>
    <tr>
      <th>Action</th>
      <th>Implementation</th>
      <th>Description</th>
      <th>FIS/ARC action</th>
      <th>Minimal Service Block</th>
      <th>Full Service Block</th>
    </tr>
  </thead>
  <tbody>
    <tr><th colspan="6" align="left">Common</th></tr>
    <tr><td><code>common:wait</code></td><td>FIS or custom</td><td>Pause execution for a fixed duration between other actions. Uses FIS by default, or Python sleep when <code>service.use_fis: false</code>.</td><td><code>aws:fis:wait</code></td><td><pre><code class="language-yaml">- name: common
  action: wait
  duration: PT2M</code></pre></td><td><pre><code class="language-yaml">- name: common
  action: wait
  duration: PT30S
  use_fis: false
  start_after: ec2:stop</code></pre></td></tr>
    <tr><th colspan="6" align="left">DNS</th></tr>
    <tr><td><code>dns:set-value</code></td><td>Custom</td><td>Update the value of a simple Route 53 DNS record for component or region workflows.</td><td></td><td><pre><code class="language-yaml">- name: dns
  action: set-value
  target:
    hosted_zone: logicgate.biz
    record_name: dev.logicgate.biz
    record_type: A
  value: 1.2.3.4</code></pre></td><td><pre><code class="language-yaml">- name: dns
  action: set-value
  target:
    hosted_zone: logicgate.biz
    record_name: dev.logicgate.biz
    record_type: A
  value: 1.2.3.4
  start_after: dns:set-weight</code></pre></td></tr>
    <tr><td><code>dns:set-weight</code></td><td>Custom</td><td>Update Route 53 weighted-routing record weights by set identifier for component or region workflows.</td><td></td><td><pre><code class="language-yaml">- name: dns
  action: set-weight
  target:
    hosted_zone: logicgate.biz
    record_name: weighted.logicgate.biz
    record_type: A
  value: primary=0,secondary=100</code></pre></td><td><pre><code class="language-yaml">- name: dns
  action: set-weight
  target:
    hosted_zone: logicgate.biz
    record_name: weighted.logicgate.biz
    record_type: A
  value: primary=10,secondary=90
  start_after: dns:set-value</code></pre></td></tr>
    <tr><th colspan="6" align="left">EC2</th></tr>
    <tr><td><code>ec2:pause-launch</code></td><td>FIS</td><td>Simulate insufficient EC2 capacity for instance launches in a site/AZ-scoped test.</td><td><code>aws:ec2:api-insufficient-instance-capacity-error</code></td><td><pre><code class="language-yaml">- name: ec2
  action: pause-launch
  duration: PT5M
  zone: ap-southeast-1a</code></pre></td><td><pre><code class="language-yaml">- name: ec2
  action: pause-launch
  duration: PT5M
  zone: ap-southeast-1a
  iam_roles: BAU,Admin
  start_after: common:wait</code></pre></td></tr>
    <tr><td><code>ec2:stop</code></td><td>FIS or custom</td><td>Stop selected EC2 instances. Uses FIS by default, or boto3 when <code>service.use_fis: false</code>. If <code>service.duration</code> is provided, the framework maps it to auto-restart behavior; if it is omitted, the instances remain stopped.</td><td><code>aws:ec2:stop-instances</code></td><td><pre><code class="language-yaml">- name: ec2
  action: stop
  region: ap-southeast-1
  tags: environment=development,project=clouddash</code></pre></td><td><pre><code class="language-yaml">- name: ec2
  action: stop
  region: ap-southeast-1
  zone: ap-southeast-1a
  tags: environment=development,project=clouddash
  duration: PT5M
  use_fis: false</code></pre></td></tr>
    <tr><td><code>ec2:reboot</code></td><td>FIS or custom</td><td>Reboot selected EC2 instances. Uses FIS by default, or boto3 when <code>service.use_fis: false</code>. <code>service.duration</code> is optional and ignored in both paths.</td><td><code>aws:ec2:reboot-instances</code></td><td><pre><code class="language-yaml">- name: ec2
  action: reboot
  region: ap-southeast-1
  tags: environment=development,project=clouddash</code></pre></td><td><pre><code class="language-yaml">- name: ec2
  action: reboot
  region: ap-southeast-1
  zone: ap-southeast-1a
  tags: environment=development,project=clouddash
  use_fis: false</code></pre></td></tr>
    <tr><td><code>ec2:terminate</code></td><td>FIS or custom</td><td>Terminate selected EC2 instances. Uses FIS by default, or boto3 when <code>service.use_fis: false</code>. Terminated instances are not restarted.</td><td><code>aws:ec2:terminate-instances</code></td><td><pre><code class="language-yaml">- name: ec2
  action: terminate
  region: ap-southeast-1
  tags: environment=development,project=clouddash</code></pre></td><td><pre><code class="language-yaml">- name: ec2
  action: terminate
  region: ap-southeast-1
  zone: ap-southeast-1a
  tags: environment=development,project=clouddash
  use_fis: false</code></pre></td></tr>
    <tr><th colspan="6" align="left">RDS</th></tr>
    <tr><td><code>rds:reboot</code></td><td>FIS or custom</td><td>Reboot selected RDS DB instances. Uses FIS by default, or boto3 when <code>service.use_fis: false</code>.</td><td><code>aws:rds:reboot-db-instances</code></td><td><pre><code class="language-yaml">- name: rds
  action: reboot
  region: ap-southeast-1
  identifier: database-1</code></pre></td><td><pre><code class="language-yaml">- name: rds
  action: reboot
  region: ap-southeast-1
  identifier: database-1
  tags: environment=development,project=clouddash
  use_fis: false</code></pre></td></tr>
    <tr><td><code>rds:failover</code></td><td>FIS or custom</td><td>Fail over a selected RDS or Aurora DB cluster to a replica. Uses FIS by default, or boto3 when <code>service.use_fis: false</code>.</td><td><code>aws:rds:failover-db-cluster</code></td><td><pre><code class="language-yaml">- name: rds
  action: failover
  region: ap-southeast-1
  identifier: database-cluster-1</code></pre></td><td><pre><code class="language-yaml">- name: rds
  action: failover
  region: ap-southeast-1
  identifier: database-cluster-1
  tags: environment=development,project=clouddash
  use_fis: false</code></pre></td></tr>
    <tr><td><code>rds:failover-global-db</code></td><td>ARC or custom</td><td>Fail over an Aurora Global Database across Regions. Uses ARC when <code>use_arc: true</code>; otherwise uses a custom boto3 RDS implementation.</td><td><code>AuroraGlobalDatabase</code></td><td><pre><code class="language-yaml">- name: rds
  action: failover-global-db
  identifier: resilience-aurora-global
  target_region: ap-southeast-2
  use_arc: false</code></pre></td><td><pre><code class="language-yaml">- name: rds
  action: failover-global-db
  identifier: resilience-aurora-global
  primary_region: ap-southeast-1
  secondary_region: ap-southeast-2
  from: ap-southeast-1
  use_arc: true</code></pre></td></tr>
    <tr><td><code>rds:switchover-global-db</code></td><td>ARC or custom</td><td>Switchover an Aurora Global Database across Regions. Uses ARC when <code>use_arc: true</code>; otherwise uses a custom boto3 RDS implementation.</td><td><code>AuroraGlobalDatabase</code></td><td><pre><code class="language-yaml">- name: rds
  action: switchover-global-db
  identifier: resilience-aurora-global
  target_region: ap-southeast-1
  use_arc: false</code></pre></td><td><pre><code class="language-yaml">- name: rds
  action: switchover-global-db
  identifier: resilience-aurora-global
  primary_region: ap-southeast-1
  secondary_region: ap-southeast-2
  from: ap-southeast-2
  use_arc: true</code></pre></td></tr>
    <tr><th colspan="6" align="left">ASG</th></tr>
    <tr><td><code>asg:pause-launch</code></td><td>FIS</td><td>Simulate insufficient capacity for Auto Scaling launches in a site/AZ-scoped test.</td><td><code>aws:ec2:asg-insufficient-instance-capacity-error</code></td><td><pre><code class="language-yaml">- name: asg
  action: pause-launch
  duration: PT5M
  zone: ap-southeast-1a
  tags: environment=development,project=clouddash</code></pre></td><td><pre><code class="language-yaml">- name: asg
  action: pause-launch
  duration: PT5M
  zone: ap-southeast-1a
  tags: environment=development,project=clouddash
  start_after: common:wait</code></pre></td></tr>
    <tr><td><code>asg:scale</code></td><td>Custom</td><td>Scale Auto Scaling Groups by updating min, max, and desired capacity through the Auto Scaling API.</td><td></td><td><pre><code class="language-yaml">- name: asg
  action: scale
  region: ap-southeast-1
  tags: environment=development,project=clouddash
  parameters:
    max: 2</code></pre></td><td><pre><code class="language-yaml">- name: asg
  action: scale
  region: ap-southeast-1
  tags: environment=development,project=clouddash
  parameters:
    min: 0
    max: 2
    desired: 2
    wait_for_ready: true</code></pre></td></tr>
    <tr><th colspan="6" align="left">Network</th></tr>
    <tr><td><code>network:disrupt-connectivity</code></td><td>FIS</td><td>Disrupt connectivity for selected subnets.</td><td><code>aws:network:disrupt-connectivity</code></td><td><pre><code class="language-yaml">- name: network
  action: disrupt-connectivity
  region: ap-southeast-1
  duration: PT5M
  tags: environment=development,project=clouddash</code></pre></td><td><pre><code class="language-yaml">- name: network
  action: disrupt-connectivity
  region: ap-southeast-1
  duration: PT5M
  zone: ap-southeast-1a
  tags: environment=development,project=clouddash</code></pre></td></tr>
    <tr><td><code>network:disrupt-vpc-endpoint</code></td><td>FIS</td><td>Disrupt traffic through selected VPC endpoints.</td><td><code>aws:network:disrupt-vpc-endpoint</code></td><td><pre><code class="language-yaml">- name: network
  action: disrupt-vpc-endpoint
  region: ap-southeast-1
  duration: PT5M
  tags: environment=development,project=clouddash</code></pre></td><td><pre><code class="language-yaml">- name: network
  action: disrupt-vpc-endpoint
  region: ap-southeast-1
  duration: PT5M
  tags: environment=development,project=clouddash
  target:
    vpc_endpoint_type: Interface
    service_name: com.amazonaws.ap-southeast-1.s3</code></pre></td></tr>
    <tr><th colspan="6" align="left">S3</th></tr>
    <tr><td><code>s3:pause-replication</code></td><td>FIS</td><td>Pause replication from source S3 buckets to destination buckets.</td><td><code>aws:s3:bucket-pause-replication</code></td><td><pre><code class="language-yaml">- name: s3
  action: pause-replication
  region: ap-southeast-1
  duration: PT5M
  destination_region: ap-southeast-2
  tags: environment=development,project=clouddash</code></pre></td><td><pre><code class="language-yaml">- name: s3
  action: pause-replication
  region: ap-southeast-1
  duration: PT5M
  destination_region: ap-southeast-2
  destination_buckets:
    - my-dr-bucket
  prefixes:
    - critical/
  tags: environment=development,project=clouddash</code></pre></td></tr>
    <tr><td><code>s3:failover</code></td><td>Custom</td><td>Fail over an S3 Multi-Region Access Point by making one target Region active and all other configured MRAP regions passive.</td><td></td><td><pre><code class="language-yaml">- name: s3
  action: failover
  target:
    mrap_name: my-mrap
    target_region: ap-southeast-2</code></pre></td><td><pre><code class="language-yaml">- name: s3
  action: failover
  region: eu-west-1
  target:
    mrap_name: my-mrap
    target_region: ap-southeast-2
  wait_for_ready: true
  timeout_seconds: 300</code></pre></td></tr>
    <tr><th colspan="6" align="left">EFS</th></tr>
    <tr><td><code>efs:failover</code></td><td>Custom</td><td>Delete the EFS replication configuration for the selected file system so the destination becomes writable.</td><td></td><td><pre><code class="language-yaml">- name: efs
  action: failover
  region: ap-southeast-1
  tags: Name=test-efs</code></pre></td><td><pre><code class="language-yaml">- name: efs
  action: failover
  region: ap-southeast-1
  tags: Name=test-efs
  wait_for_ready: true
  start_after: common:wait</code></pre></td></tr>
    <tr><td><code>efs:failback</code></td><td>Custom</td><td>Create reverse EFS replication from the selected source file system back to a specific destination file system in another Region.</td><td></td><td><pre><code class="language-yaml">- name: efs
  action: failback
  region: ap-southeast-2
  identifier: fs-0123456789abcdef0
  target:
    destination_region: ap-southeast-1
    destination_file_system_id: fs-0fedcba9876543210</code></pre></td><td><pre><code class="language-yaml">- name: efs
  action: failback
  region: ap-southeast-2
  tags: Name=test-efs-secondary
  target:
    destination_region: ap-southeast-1
    destination_tags: Name=test-efs-primary
  wait_for_ready: true
  timeout_seconds: 600</code></pre></td></tr>
    <tr><th colspan="6" align="left">EKS</th></tr>
    <tr><td><code>eks:delete-pod</code></td><td>FIS</td><td>Delete selected EKS pods by namespace and selector.</td><td><code>aws:eks:pod-delete</code></td><td><pre><code class="language-yaml">- name: eks
  action: delete-pod
  region: ap-southeast-1
  target:
    cluster_identifier: my-eks-cluster
    namespace: default
    selector_type: labelSelector
    selector_value: app=myapp
  kubernetes_service_account: myserviceaccount</code></pre></td><td><pre><code class="language-yaml">- name: eks
  action: delete-pod
  region: ap-southeast-1
  target:
    cluster_identifier: my-eks-cluster
    namespace: default
    selector_type: labelSelector
    selector_value: app=myapp
  kubernetes_service_account: myserviceaccount
  parameters:
    grace_period_seconds: 30</code></pre></td></tr>
    <tr><td><code>eks:pod-cpu-stress</code></td><td>FIS</td><td>Run CPU stress against selected EKS pods.</td><td><code>aws:eks:pod-cpu-stress</code></td><td><pre><code class="language-yaml">- name: eks
  action: pod-cpu-stress
  region: ap-southeast-1
  duration: PT2M
  target:
    cluster_identifier: my-eks-cluster
    namespace: default
    selector_type: labelSelector
    selector_value: app=myapp
  kubernetes_service_account: myserviceaccount</code></pre></td><td><pre><code class="language-yaml">- name: eks
  action: pod-cpu-stress
  region: ap-southeast-1
  duration: PT2M
  target:
    cluster_identifier: my-eks-cluster
    namespace: default
    selector_type: labelSelector
    selector_value: app=myapp
  kubernetes_service_account: myserviceaccount
  parameters:
    workers: 2
    percent: 80</code></pre></td></tr>
    <tr><td><code>eks:pod-io-stress</code></td><td>FIS</td><td>Run I/O stress against selected EKS pods.</td><td><code>aws:eks:pod-io-stress</code></td><td><pre><code class="language-yaml">- name: eks
  action: pod-io-stress
  region: ap-southeast-1
  duration: PT2M
  target:
    cluster_identifier: my-eks-cluster
    namespace: default
    selector_type: labelSelector
    selector_value: app=myapp
  kubernetes_service_account: myserviceaccount</code></pre></td><td><pre><code class="language-yaml">- name: eks
  action: pod-io-stress
  region: ap-southeast-1
  duration: PT2M
  target:
    cluster_identifier: my-eks-cluster
    namespace: default
    selector_type: labelSelector
    selector_value: app=myapp
  kubernetes_service_account: myserviceaccount
  parameters:
    workers: 2</code></pre></td></tr>
    <tr><td><code>eks:pod-memory-stress</code></td><td>FIS</td><td>Run memory stress against selected EKS pods.</td><td><code>aws:eks:pod-memory-stress</code></td><td><pre><code class="language-yaml">- name: eks
  action: pod-memory-stress
  region: ap-southeast-1
  duration: PT2M
  target:
    cluster_identifier: my-eks-cluster
    namespace: default
    selector_type: labelSelector
    selector_value: app=myapp
  kubernetes_service_account: myserviceaccount</code></pre></td><td><pre><code class="language-yaml">- name: eks
  action: pod-memory-stress
  region: ap-southeast-1
  duration: PT2M
  target:
    cluster_identifier: my-eks-cluster
    namespace: default
    selector_type: labelSelector
    selector_value: app=myapp
  kubernetes_service_account: myserviceaccount
  parameters:
    percent: 80</code></pre></td></tr>
    <tr><td><code>eks:terminate-nodegroup-instances</code></td><td>FIS</td><td>Terminate a percentage of instances in an Amazon EKS managed node group.</td><td><code>aws:eks:terminate-nodegroup-instances</code></td><td><pre><code class="language-yaml">- name: eks
  action: terminate-nodegroup-instances
  region: ap-southeast-1
  target:
    nodegroup_arn: arn:aws:eks:...
  parameters:
    instance_termination_percentage: 50</code></pre></td><td><pre><code class="language-yaml">- name: eks
  action: terminate-nodegroup-instances
  region: ap-southeast-1
  target:
    nodegroup_arn: arn:aws:eks:...
  parameters:
    instance_termination_percentage: 50
  start_after: eks:delete-pod</code></pre></td></tr>
    <tr><td><code>eks:scale-deployment</code></td><td>Custom</td><td>Scale a Kubernetes Deployment in an EKS cluster through the Kubernetes API for component or region workflows.</td><td></td><td><pre><code class="language-yaml">- name: eks
  action: scale-deployment
  region: ap-southeast-1
  target:
    cluster_identifier: my-eks-cluster
    namespace: default
    deployment_name: my-service
  parameters:
    replicas: 0</code></pre></td><td><pre><code class="language-yaml">- name: eks
  action: scale-deployment
  region: ap-southeast-1
  target:
    cluster_identifier: my-eks-cluster
    namespace: default
    deployment_name: my-service
  parameters:
    replicas: 3
    wait_for_ready: true
    timeout_seconds: 600</code></pre></td></tr>
  </tbody>
</table>

## Manifest Design

This section is split into:

- top-level manifest defaults
- shared service-block fields
- service and action specific fields
- observability fields

### Top-Level Fields

| Field | Required | Description |
| --- | --- | --- |
| `region` | Optional default | Default AWS Region for execution, resource discovery, and observability. `service.region` overrides it. For `s3:failover`, this is the MRAP failover-control Region and defaults to `eu-west-1` when omitted. |
| `zone` | Optional default | Default Availability Zone scope. `service.zone` overrides it. |
| `primary_region` | Optional default | Default active Region for Aurora Global Database actions. `service.primary_region` overrides it. |
| `secondary_region` | Optional default | Default standby Region for Aurora Global Database actions. `service.secondary_region` overrides it. |
| `services` | Yes | List of service/action blocks to execute. |
| `observability` | No | Optional health-check and CloudWatch configuration around the experiment window. |

### Shared Service Fields

| Field | Required | Description |
| --- | --- | --- |
| `service.name` | Yes | Service name such as `common`, `ec2`, `rds`, `asg`, `network`, `s3`, `efs`, `dns`, or `eks`. |
| `service.action` | Yes | Action name for that service, such as `stop`, `failover`, or `delete-pod`. |
| `service.start_after` | No | Dependency list for ordered execution. When omitted, actions run in parallel by default. Use `<service>:<action>` when unique in the manifest, or `<service>:<action>#<n>` when repeated. |
| `service.region` | No | Per-action Region override. It takes precedence over top-level `region`. |
| `service.zone` | No | Per-action Availability Zone override. It takes precedence over top-level `zone`. |
| `service.primary_region` | No | Per-action active Region override for Aurora Global Database actions. |
| `service.secondary_region` | No | Per-action standby Region override for Aurora Global Database actions. |
| `service.tags` | Usually yes | Comma-separated `key=value` filters used for tag-based AWS resource discovery. AND semantics are used across tags. |
| `service.identifier` | No | Optional exact resource identifier used by supported RDS actions. If both `service.identifier` and `service.tags` are present, both must match. |
| `service.duration` | Depends on action | ISO-8601 duration such as `PT5M` or `PT30M`. |
| `service.use_fis` | No | Selects FIS or custom execution for supported dual-path actions. Defaults to `true`. |
| `service.use_arc` | No | Selects ARC or custom execution for supported Aurora Global Database actions. |
| `service.wait_for_ready` | No | Waits until the custom action reaches its steady state before completing. |
| `service.timeout_seconds` | No | Per-action timeout for custom readiness polling. |
| `service.value` | Yes for some actions | Action-specific string payload used by DNS actions. |

### Service Fields by Action

<table>
  <thead>
    <tr>
      <th>Action</th>
      <th>Required Fields</th>
      <th>Optional Fields</th>
      <th>Notes</th>
    </tr>
  </thead>
  <tbody>
    <tr><th colspan="4" align="left">Common</th></tr>
    <tr><td><code>common:wait</code></td><td><code>service.duration</code></td><td><code>service.use_fis</code>, <code>service.start_after</code></td><td>Uses <code>aws:fis:wait</code> by default, or Python sleep when <code>service.use_fis: false</code>.</td></tr>
    <tr><th colspan="4" align="left">DNS</th></tr>
    <tr><td><code>dns:set-value</code></td><td><code>service.target.hosted_zone</code>, <code>service.target.record_name</code>, <code>service.target.record_type</code>, <code>service.value</code></td><td><code>service.start_after</code></td><td>Targets exactly one simple non-alias Route 53 record.</td></tr>
    <tr><td><code>dns:set-weight</code></td><td><code>service.target.hosted_zone</code>, <code>service.target.record_name</code>, <code>service.target.record_type</code>, <code>service.value</code></td><td><code>service.start_after</code></td><td><code>service.value</code> is a comma-separated list like <code>primary=0, secondary=100</code>.</td></tr>
    <tr><th colspan="4" align="left">EC2</th></tr>
    <tr><td><code>ec2:pause-launch</code></td><td><code>service.duration</code>, zone, and one of <code>service.iam_roles</code> or <code>service.iam_role_arns</code></td><td><code>service.start_after</code></td><td>AZ scope is passed through <code>availabilityZoneIdentifiers</code>.</td></tr>
    <tr><td><code>ec2:stop</code></td><td><code>service.tags</code></td><td><code>service.duration</code>, <code>service.instance_count</code>, <code>service.use_fis</code>, <code>service.zone</code>, <code>service.start_after</code></td><td>If <code>service.duration</code> is omitted, instances stay stopped.</td></tr>
    <tr><td><code>ec2:reboot</code></td><td><code>service.tags</code></td><td><code>service.instance_count</code>, <code>service.use_fis</code>, <code>service.duration</code>, <code>service.zone</code>, <code>service.start_after</code></td><td><code>service.duration</code> is optional and ignored.</td></tr>
    <tr><td><code>ec2:terminate</code></td><td><code>service.tags</code></td><td><code>service.instance_count</code>, <code>service.use_fis</code>, <code>service.zone</code>, <code>service.start_after</code></td><td>Uses FIS by default or boto3 when <code>service.use_fis: false</code>.</td></tr>
    <tr><th colspan="4" align="left">RDS</th></tr>
    <tr><td><code>rds:reboot</code></td><td>One of <code>service.tags</code>, <code>service.identifier</code>, or both</td><td><code>service.use_fis</code>, <code>service.start_after</code></td><td><code>service.identifier</code> is the DB instance identifier.</td></tr>
    <tr><td><code>rds:failover</code></td><td>One of <code>service.tags</code>, <code>service.identifier</code>, or both</td><td><code>service.use_fis</code>, <code>service.start_after</code></td><td><code>service.identifier</code> is the DB cluster identifier.</td></tr>
    <tr><td><code>rds:failover-global-db</code></td><td>ARC path: selector plus <code>service.from</code> and Region pair. Custom path: selector plus <code>service.target_region</code>.</td><td><code>service.use_arc</code>, <code>service.primary_region</code>, <code>service.secondary_region</code>, <code>service.start_after</code></td><td><code>service.identifier</code> is the global cluster identifier.</td></tr>
    <tr><td><code>rds:switchover-global-db</code></td><td>ARC path: selector plus <code>service.from</code> and Region pair. Custom path: selector plus <code>service.target_region</code>.</td><td><code>service.use_arc</code>, <code>service.primary_region</code>, <code>service.secondary_region</code>, <code>service.start_after</code></td><td>Uses the same selector rules as <code>rds:failover-global-db</code>.</td></tr>
    <tr><th colspan="4" align="left">ASG</th></tr>
    <tr><td><code>asg:pause-launch</code></td><td><code>service.duration</code>, zone, <code>service.tags</code></td><td><code>service.start_after</code></td><td>Zone is passed through <code>availabilityZoneIdentifiers</code>.</td></tr>
    <tr><td><code>asg:scale</code></td><td><code>service.tags</code>, <code>service.parameters.max</code></td><td><code>service.parameters.min</code>, <code>service.parameters.desired</code>, <code>service.parameters.wait_for_ready</code>, <code>service.parameters.timeout_seconds</code>, <code>service.start_after</code></td><td><code>min</code> defaults to <code>0</code>; <code>desired</code> defaults to <code>max</code>.</td></tr>
    <tr><th colspan="4" align="left">Network</th></tr>
    <tr><td><code>network:disrupt-connectivity</code></td><td><code>service.duration</code>, <code>service.tags</code></td><td><code>service.zone</code>, <code>service.start_after</code></td><td>If <code>service.zone</code> is present, scope becomes availability-zone.</td></tr>
    <tr><td><code>network:disrupt-vpc-endpoint</code></td><td><code>service.duration</code> and at least one selector such as <code>service.tags</code> or <code>service.target.vpc_endpoint_id</code></td><td><code>service.target.vpc_endpoint_type</code>, <code>service.target.service_name</code>, <code>service.start_after</code></td><td>Targets interface VPC endpoints through the FIS binding expected by <code>aws:network:disrupt-vpc-endpoint</code>.</td></tr>
    <tr><th colspan="4" align="left">S3</th></tr>
    <tr><td><code>s3:pause-replication</code></td><td><code>service.tags</code>, <code>service.duration</code>, <code>service.destination_region</code></td><td><code>service.destination_buckets</code>, <code>service.prefixes</code>, <code>service.start_after</code></td><td>Narrows paused replication rules by destination bucket and prefix when supplied.</td></tr>
    <tr><td><code>s3:failover</code></td><td>Exactly one of <code>service.target.mrap_name</code>, <code>service.target.mrap_alias</code>, or <code>service.target.mrap_arn</code>, plus <code>service.target.target_region</code></td><td><code>service.region</code>, <code>service.wait_for_ready</code>, <code>service.timeout_seconds</code>, <code>service.start_after</code></td><td><code>service.region</code> defaults to <code>eu-west-1</code>.</td></tr>
    <tr><th colspan="4" align="left">EFS</th></tr>
    <tr><td><code>efs:failover</code></td><td>At least one source selector using <code>service.tags</code> or <code>service.identifier</code></td><td><code>service.wait_for_ready</code>, <code>service.start_after</code></td><td>Deletes the replication configuration for the selected file system.</td></tr>
    <tr><td><code>efs:failback</code></td><td>At least one source selector using <code>service.tags</code> or <code>service.identifier</code>, plus <code>service.target.destination_region</code> and at least one destination selector using <code>service.target.destination_file_system_id</code> or <code>service.target.destination_tags</code></td><td><code>service.wait_for_ready</code>, <code>service.timeout_seconds</code>, <code>service.start_after</code></td><td>Creates reverse replication back to the destination file system and automatically disables destination replication overwrite protection first when needed.</td></tr>
    <tr><th colspan="4" align="left">EKS</th></tr>
    <tr><td><code>eks:delete-pod</code></td><td>Target fields for cluster, namespace, selector, and a Kubernetes service account value</td><td><code>service.target.count</code>, <code>service.target.selection_mode</code>, <code>service.parameters.grace_period_seconds</code>, <code>service.parameters.max_errors_percent</code>, <code>service.start_after</code></td><td>Pod-targeted EKS FIS actions share the same target shape.</td></tr>
    <tr><td><code>eks:pod-cpu-stress</code></td><td><code>service.duration</code>, the same target fields as <code>eks:delete-pod</code>, and a Kubernetes service account value</td><td><code>service.parameters.workers</code>, <code>service.parameters.percent</code>, <code>service.parameters.max_errors_percent</code>, FIS pod overrides, <code>service.start_after</code></td><td>Uses the native FIS pod CPU stress action.</td></tr>
    <tr><td><code>eks:pod-io-stress</code></td><td><code>service.duration</code>, the same target fields as <code>eks:delete-pod</code>, and a Kubernetes service account value</td><td><code>service.parameters.workers</code>, <code>service.parameters.percent</code>, <code>service.parameters.max_errors_percent</code>, FIS pod overrides, <code>service.start_after</code></td><td>Uses the native FIS pod I/O stress action.</td></tr>
    <tr><td><code>eks:pod-memory-stress</code></td><td><code>service.duration</code>, the same target fields as <code>eks:delete-pod</code>, and a Kubernetes service account value</td><td><code>service.parameters.workers</code>, <code>service.parameters.percent</code>, <code>service.parameters.max_errors_percent</code>, FIS pod overrides, <code>service.start_after</code></td><td>Uses the native FIS pod memory stress action.</td></tr>
    <tr><td><code>eks:terminate-nodegroup-instances</code></td><td><code>service.parameters.instance_termination_percentage</code> and one selector path using either <code>service.tags</code>, <code>service.target.nodegroup_arn</code>, or <code>service.target.nodegroup_arns</code></td><td><code>service.start_after</code></td><td>Targets Amazon EKS managed node groups.</td></tr>
    <tr><td><code>eks:scale-deployment</code></td><td><code>service.target.cluster_identifier</code>, <code>service.target.namespace</code>, <code>service.target.deployment_name</code>, <code>service.parameters.replicas</code></td><td><code>service.region</code>, <code>service.parameters.wait_for_ready</code>, <code>service.parameters.timeout_seconds</code>, <code>service.start_after</code></td><td>Custom action that uses the Kubernetes API, not FIS.</td></tr>
  </tbody>
</table>

### Observability Fields

| Field | Required | Description |
| --- | --- | --- |
| `observability.start_before` | No | Number of minutes to collect observability before starting the action. |
| `observability.stop_after` | No | Number of minutes to continue collecting observability after the action completes. |
| `observability.health_check.endpoint` | Yes when used | HTTP endpoint to probe periodically. |
| `observability.health_check.http_method` | No | HTTP method, typically `get`. |
| `observability.health_check.healthy_status_code` | No | Expected healthy HTTP status code, typically `200`. |
| `observability.health_check.interval` | No | Polling interval in seconds. |
| `observability.cloudwatch.load_balancer.type` | Yes when used | Load balancer type such as `alb`. |
| `observability.cloudwatch.load_balancer.name` | No | Explicit load balancer name. |
| `observability.cloudwatch.load_balancer.tags` | No | Tag filters used when `name` is not supplied. |
| `observability.cloudwatch.load_balancer.metrics` | Yes when used | List of CloudWatch metric names to collect. |

Important sequencing note:

- Actions now run in parallel by default when `service.start_after` is omitted.
- For native FIS actions, that matches FIS behavior when no `startAfter` is set.
- For custom-only manifests, the framework also uses dependency-driven execution and only waits when `service.start_after` is declared.

## Implemented Validations

Only actions listed in `scripts/validations/actions.yml` currently run pre-execution validations. If a validation fails, the framework stops before starting the action and returns a descriptive error.

You can bypass these pre-execution checks with the CLI flag `--skip-validation`. That flag skips the validation layer only; manifest loading, engine-family resolution, and the downstream AWS or Kubernetes calls still run and can still fail later.

| Service | Action | Validation | What It Checks |
| --- | --- | --- | --- |
| `asg` | `asg:scale` | `verify_resource_existence` | At least one Auto Scaling Group matches the selector. |
| `asg` | `asg:scale` | `verify_scale_values` | `service.parameters.max` exists and is a non-negative integer; `min` and `desired`, when present, are integers within valid bounds; `min <= desired <= max`. |
| `dns` | `dns:set-value` | `verify_record_exists` | The target hosted zone, record name, and record type resolve to at least one Route 53 record set. |
| `dns` | `dns:set-value` | `verify_value_present` | `service.value` is present and non-empty. |
| `dns` | `dns:set-value` | `verify_simple_record_target` | Exactly one record set matches, and it is a simple non-alias record rather than a policy or alias record. |
| `dns` | `dns:set-weight` | `verify_record_exists` | The target hosted zone, record name, and record type resolve to at least one Route 53 record set. |
| `dns` | `dns:set-weight` | `verify_value_present` | `service.value` is present and non-empty. |
| `dns` | `dns:set-weight` | `verify_weight_targets` | The value parses as `set_identifier=weight` assignments, all referenced set identifiers exist, and the matching records are true weighted records with a `Weight` field. |
| `ec2` | `ec2:stop` | `verify_resource_existence` | At least one EC2 instance matches the selector. |
| `ec2` | `ec2:reboot` | `verify_resource_existence` | At least one EC2 instance matches the selector. |
| `ec2` | `ec2:terminate` | `verify_resource_existence` | At least one EC2 instance matches the selector. |
| `efs` | `efs:failover` | `verify_resource_existence` | At least one EFS file system matches the selector. |
| `efs` | `efs:failover` | `verify_replication_configuration_exists` | Each selected EFS file system has an existing replication configuration that can be deleted. |
| `efs` | `efs:failback` | `verify_resource_existence` | Exactly one source EFS file system must match the selector. |
| `efs` | `efs:failback` | `verify_failback_target` | `service.target.destination_region` must be present and different from the source Region, and the destination selector must resolve to exactly one different EFS file system. |
| `efs` | `efs:failback` | `verify_failback_state` | The source and destination file systems must not already be part of another replication configuration, and the destination must not already be in `REPLICATING` overwrite-protection state. |
| `eks` | `eks:scale-deployment` | `verify_deployment_existence` | `service.target.cluster_identifier`, `namespace`, and `deployment_name` are present, the Kubernetes API is reachable, and the target Deployment exists. |
| `eks` | `eks:scale-deployment` | `verify_replicas_value` | `service.parameters.replicas` exists, is an integer, and is greater than or equal to zero. |
| `network` | `network:disrupt-vpc-endpoint` | `verify_resource_existence` | At least one VPC endpoint matches the selector. |
| `rds` | `rds:reboot` | `verify_resource_existence` | At least one DB instance matches the selector. |
| `rds` | `rds:reboot` | `verify_replica` | Each selected DB instance has Multi-AZ enabled or has at least one read replica. |
| `rds` | `rds:failover` | `verify_resource_existence` | At least one DB cluster matches the selector. |
| `rds` | `rds:failover` | `verify_replica` | Each selected DB cluster has at least one non-writer replica/reader member available for failover. |
| `s3` | `s3:pause-replication` | `verify_resource_existence` | At least one S3 bucket matches the selector. |
| `s3` | `s3:pause-relication` | `verify_resource_existence` | Legacy alias for `s3:pause-replication`; the same bucket-existence validation is applied. |
| `s3` | `s3:failover` | `verify_mrap_selector` | Exactly one MRAP selector is provided, `service.target.target_region` is present, and the control Region is one of the AWS-supported MRAP failover-control endpoints. |
| `s3` | `s3:failover` | `verify_mrap_failover_state` | The selected MRAP exists and is `READY`, has at least two configured regions, includes the target Region, is currently active/passive with route dials of only `0` or `100`, and the target Region is not already active. |

### FIS Example

See `manifests/component-ec2.yml`.

Example shape:

```yaml
region: ap-southeast-1
services:
- name: ec2
  action: terminate
  tags: environment=development,project=clouddash
  instance_count: 1
- name: common
  action: wait
  duration: PT2M
  start_after: ec2:terminate
- name: rds
  action: reboot
  tags: environment=development,project=clouddash
  start_after: common:wait
- name: eks
  action: delete-pod
  start_after: rds:reboot
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

### AZ-Scoped Example

Expected additions relative to a plain single-Region manifest:

- `zone: <availability-zone>`

AZ scoping is applied to targets where supported. The code already contains special handling for:

- EC2 instances
- subnets
- RDS clusters
- RDS DB instances
- Auto Scaling groups

### ARC Example

See `manifests/geo-rds.yml`, `manifests/geo-eks.yml`, and `manifests/geo-dns.yml`.

Example shape:

```yaml
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

Non-ARC shape:

```yaml
services:
- name: rds
  action: failover-global-db
  identifier: resilience-aurora-global
  target_region: ap-southeast-2
  use_arc: false
```

Notes on Aurora Global Database manifests:

- `from` can be either:
  - `primary` / `secondary`
  - or the actual Region name from `primary_region` / `secondary_region`
- `from: primary` or `from: <primary_region>` means switch away from the current primary Region into the secondary Region
- `from: secondary` or `from: <secondary_region>` means switch back into the primary Region
- when `use_arc = false`, prefer `target_region` instead of `from`
- tags or identifier can be used for discovery; the user does not need to provide the Aurora Global Database ARN or member cluster ARNs
- `use_arc` only applies to Aurora Global Database actions
- if `use_arc` is omitted, the current default behavior is `true`
- for `eks:scale-deployment`, use `service.region` plus an explicit `cluster_identifier`

## ARC Region Switch Design

The ARC path is intentionally separate from the FIS path.

### Why

Aurora Global Database failover and switchover are orchestrated using ARC Region switch rather than a native FIS action in this codebase. That keeps ARC orchestration separate from FIS experiment-template generation and makes it easier to grow into more sophisticated region-recovery workflows later.

### Current Region Execution Implementation

The current ARC path in `scripts/region_switch.py` does the following:

1. validates the ARC manifest
2. resolves any action-specific targets needed by the selected services
3. builds an ARC execution plan
4. selects the engine per service block:
   - ARC when `use_arc = true`
   - non-ARC custom execution when `use_arc = false`
5. applies `service.start_after` dependencies
6. runs ARC actions in parallel by default when `service.start_after` is omitted, and writes a unified summary for reporting

### ARC Path

When `use_arc = true`, the code:

1. creates an ARC plan with:
   - `recoveryApproach = activePassive`
   - one `activate` workflow for each Region in the plan
   - one Aurora Global Database step in each workflow
2. starts execution with:
   - plan creation through the control Region endpoint
   - execution start and execution polling through the target Region endpoint
   - `activate`
   - `ungraceful` for `failover-global-db`
   - `graceful` for `switchover-global-db`
   - a short retry loop after `CreatePlan` so `StartPlanExecution` waits for the new plan ARN to become executable

### Non-ARC Path

When `use_arc = false`, the code currently uses direct boto3 RDS APIs:

- `rds.failover_global_cluster()` for `failover-global-db`
- `rds.switchover_global_cluster()` for `switchover-global-db`
- `service.target_region` selects which member cluster Region should be promoted
- `service.identifier` or `service.tags` selects the Aurora Global Database to operate on

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

For region tests, `scripts/main.py` currently passes an empty impacted-resource list into `start_observability_collectors()`.

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

## Custom Action Design

Custom actions are used when the framework needs to execute an action that is not available as a native FIS action, or when a supported action explicitly opts out of FIS or ARC with `service.use_fis: false` or `service.use_arc: false`.

Current implementation:

- `common:wait` when `service.use_fis = false`
- `ec2:stop` when `service.use_fis = false`
- `ec2:reboot` when `service.use_fis = false`
- `ec2:terminate` when `service.use_fis = false`
- `rds:reboot` when `service.use_fis = false`
- `rds:failover` when `service.use_fis = false`
- `rds:failover-global-db` when `service.use_arc = false`
- `rds:switchover-global-db` when `service.use_arc = false`
- `efs:failover`
- `dns:set-value`
- `dns:set-weight`
- `asg:scale`
- `eks:scale-deployment`

Design rules:

1. Native FIS actions stay in `scripts/template_generator/`.
2. Custom-only component actions live under `scripts/component_actions/`.
3. `scripts/main.py` routes a manifest to one engine family: FIS, ARC, or custom.
4. Mixing FIS, ARC, and custom actions in the same manifest is intentionally not supported.

### Current `ec2` boto3 Fallback Behavior

When `service.use_fis = false` for `ec2:stop`, `ec2:reboot`, or `ec2:terminate`, the framework uses the EC2 API directly instead of creating a FIS experiment.

Current behavior:

- `ec2:stop`
  - stops the selected instances
  - waits until they are fully stopped
  - if `service.duration` is present:
    - sleeps for `service.duration`
    - starts the instances again
    - waits until they are running
  - if `service.duration` is absent:
    - leaves the instances stopped
- `ec2:reboot`
  - reboots the selected instances
  - waits until EC2 instance status checks return `ok`
  - ignores `service.duration`
- `ec2:terminate`
  - terminates the selected instances
  - waits until they are terminated
  - does not restart them

### Current `rds` boto3 Fallback Behavior

When `service.use_fis = false` for `rds:reboot` or `rds:failover`, or when `service.use_arc = false` for Aurora Global Database actions, the framework uses the RDS API directly instead of creating a FIS experiment or ARC plan.

Current behavior:

- `rds:reboot`
  - reboots the selected DB instances with `RebootDBInstance`
  - waits until each DB instance returns to `available`
- `rds:failover`
  - forces failover on the selected DB clusters with `FailoverDBCluster`
  - waits until each cluster returns to `available`
  - when the original writer can be determined, waits until the cluster writer changes
- `rds:failover-global-db`
  - calls `FailoverGlobalCluster`
  - waits until the new writer is promoted and both Regions report a synchronized, `available` Aurora Global Database state
- `rds:switchover-global-db`
  - calls `SwitchoverGlobalCluster`
  - waits until the new writer is promoted and both Regions report a synchronized, `available` Aurora Global Database state

### Current `efs:failover` Behavior

The custom EFS failover action:

1. resolves EFS file systems from `service.tags` and/or `service.identifier`
2. verifies that each selected file system has a replication configuration
3. deletes the replication configuration by calling `DeleteReplicationConfiguration` on the source file system
4. either:
   - completes immediately when `service.wait_for_ready = false`
   - or polls until the replication configuration is no longer returned by `DescribeReplicationConfigurations`
   - a `ReplicationNotFound` response during that wait is treated as success, because it means the replication configuration is already gone

If your tag selection matches multiple file systems, the action operates on all of them. It fails fast when any selected file system does not have replication configured.

### Current `efs:failback` Behavior

The custom EFS failback action:

1. resolves exactly one source EFS file system from `service.tags` and/or `service.identifier`
2. resolves exactly one destination EFS file system from:
   - `service.target.destination_file_system_id`
   - and/or `service.target.destination_tags`
   - in `service.target.destination_region`
3. verifies that neither the source nor the destination is already part of another replication configuration
4. inspects the destination file system's replication overwrite protection and, when needed, updates it to `DISABLED`
5. creates reverse replication by calling `CreateReplicationConfiguration`
6. either:
   - completes immediately when `service.wait_for_ready = false`
   - or polls until the new replication configuration is returned from `DescribeReplicationConfigurations` with status `ENABLED`

This action is intentionally strict: it requires exactly one source and one destination file system so failback does not happen against the wrong target.

### Current `s3:failover` Behavior

The custom S3 MRAP failover action:

1. resolves the Multi-Region Access Point from exactly one selector:
   - `service.target.mrap_name`
   - `service.target.mrap_alias`
   - `service.target.mrap_arn`
2. reads the current MRAP route state through S3 Control
3. validates that:
   - the MRAP is `READY`
   - the requested `service.target.target_region` exists in the MRAP
   - the MRAP is currently active/passive, not active/active
   - the target Region is not already active
4. submits new route updates so the target Region becomes active with traffic dial `100` and all other configured MRAP Regions become passive with traffic dial `0`
5. either:
   - completes immediately when `service.wait_for_ready = false`
   - or polls until the MRAP route state reflects the requested failover

The action uses S3 MRAP failover-control endpoints. If `service.region` is omitted, it defaults to `eu-west-1`.

### Current `asg:scale` Behavior

The custom ASG scaler:

1. resolves matching Auto Scaling Groups from the manifest tags
2. updates each matched group with:
   - `MinSize`
   - `MaxSize`
   - `DesiredCapacity`
3. optionally waits until each group reports the requested min, max, and desired values and the active/in-service instance count reaches the target desired capacity

Default behavior:

- `max` is required
- `min` defaults to `0`
- `desired` defaults to `max`

The impacted resources written for this action are the resolved Auto Scaling Group ARNs.

### Current `eks:scale-deployment` Behavior

The custom scaler:

1. reads the EKS cluster endpoint and CA from `DescribeCluster`
2. generates an IAM-authenticated EKS bearer token
3. patches the target Deployment replica count through the Kubernetes API
4. optionally waits until the Deployment is reconciled and ready

Proxy behavior:

- the Kubernetes client honors valid proxy environment variables such as `HTTPS_PROXY`
- malformed proxy values are ignored
- if the EKS cluster endpoint matches `NO_PROXY`, the client connects directly

Readiness is considered complete when:

- `status.observedGeneration >= metadata.generation`
- `spec.replicas == desired replicas`
- if desired replicas is greater than `0`:
  - `status.replicas == desired replicas`
  - `status.updatedReplicas == desired replicas`
  - `status.readyReplicas == desired replicas`
  - `status.availableReplicas == desired replicas`
- if desired replicas is `0`:
  - `status.replicas == 0`
  - `status.readyReplicas == 0`
  - `status.availableReplicas == 0`

The impacted resource written for this action is a synthetic identifier in this form:

- `eks://<cluster>/<namespace>/deployment/<deployment>`

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

### How Observability Is Configured

`scripts/observability.py` reads the `observability:` block through `parse_observability()`.

Current behavior:

- `start_before` and `stop_after` are interpreted as minutes
- `health_check.http_method` supports `get` and `post`
- `health_check.healthy_status_code` can be a single value, a comma-separated string, or a list
- if `health_check.healthy_status_code` is omitted, the collector defaults to `[200]`
- if `health_check.interval` is omitted, the collector defaults to `10` seconds
- load balancer CloudWatch collection is enabled only when `observability.cloudwatch.load_balancer.type` and a non-empty `metrics` list are provided

### Health Check Collection

The health-check collector:

- runs in its own thread
- writes `health_check.csv`
- records the columns:
  - `time`
  - `http_status_code`
  - `error`
- marks a sample healthy in memory when the returned status code is in the configured `healthy_status_code` list
- treats request exceptions as unhealthy and records the error text

The current CSV output does not include a `healthy` column. The healthy/unhealthy decision is derived later by the report code from the HTTP status code values in `health_check.csv`.

### CloudWatch Collection

There are two CloudWatch collection modes:

1. Load balancer metrics from `observability.cloudwatch.load_balancer`
2. Automatic impacted-resource metrics for supported ASG and RDS resources

Current behavior:

- one CSV file is written per metric
- each metric CSV contains:
  - `time`
  - `value`
- the CloudWatch query period is rounded to a minimum of `60` seconds and then to the next multiple of `60`
- each poll queries a recent sliding window of `max(period * 5, 300)` seconds
- the collector always uses CloudWatch `Stat = Sum`

### Automatic Impacted-Resource Metrics

Automatic metric collection is derived from `impacted_resources.json` using `SERVICE_CLOUDWATCH_METRICS_MAP`.

Current mappings:

- `asg`
  - `GroupDesiredCapacity`
  - `GroupInServiceInstances`
  - `GroupPendingInstances`
  - `GroupTerminatingInstances`
- `rds:db`
  - `CPUUtilization`
  - `DatabaseConnections`
  - `FreeableMemory`
  - `FreeStorageSpace`
- `rds:cluster`
  - `DatabaseConnections`
  - `VolumeReadIOPs`
  - `VolumeWriteIOPs`
  - `AuroraReplicaLagMaximum`

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

### How the Report Calculates SLO Summary

The report writes an `SLO Summary (Approx.)` block by scanning the generated CSV files in the output directory.

#### Health Check SLO

For `health_check.csv`, the report currently classifies a sample as healthy when:

- `http_status_code` is between `200` and `399` inclusive of `200` and exclusive of `400`

Important nuance:

- this report logic does not currently reuse the manifest’s `healthy_status_code` setting
- the collector can be configured with a narrower healthy-code list, but the report still treats any `2xx` or `3xx` as healthy

The current health-check SLO fields are:

- `samples`: number of valid health-check rows
- `healthy_samples`: number of rows classified as healthy
- `availability`: `healthy_samples / samples`
- `sample_interval_seconds`: median delta between consecutive samples
- `total_outage_seconds_approx`: `unhealthy_samples * sample_interval_seconds`
- `longest_outage_seconds_approx`: longest consecutive unhealthy run in samples multiplied by the sample interval
- `first_failure_after_experiment_start`: first unhealthy sample at or after experiment start
- `recovery_seconds_approx`: elapsed seconds from the first unhealthy sample after experiment start to the first subsequent healthy sample

This is intentionally approximate. It is sample-based rather than request-volume-based.

#### Metric Summary

For every non-health-check CSV, the report currently calculates:

- `min`
- `max`
- `avg`
- `points`

If the report can determine `startTime` and `endTime` from `result_*.json`, it also computes:

- `during_experiment.min`
- `during_experiment.max`
- `during_experiment.avg`
- `during_experiment.points`

This is a descriptive summary only. It is not a threshold-based SLO evaluation.

### Experiment Timeline Overlay

Each chart uses the experiment metadata from `result_*.json` to add:

- `Experiment Start`
- `Experiment End`
- a shaded experiment window

This makes the charts and the approximate SLO summary relative to the same recorded execution window.

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

The canonical env file is `scripts/.env`, which contains default values for `scripts/main.py`.

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
- if a CLI argument is omitted, `main.py` falls back to `.env`
- if the key is not present in `.env`, `main.py` falls back to its hardcoded default

### Install Dependencies

Dependencies are listed in `scripts/requirements.txt`.

Typical install:

```powershell
pip install -r scripts/requirements.txt
```

## Helper Scripts

The repository also contains helper provisioning scripts under `commands/`.

As a convention, every new `create_*.sh` helper under `commands/` should be paired with a corresponding `destroy_*.sh` helper so test resources can be cleaned up predictably.

### Network Helper

- `commands/network/create_vpc_endpoint.sh` creates or reuses a VPC endpoint helper resource for network resilience testing.
- `commands/network/destroy_vpc_endpoint.sh` deletes the current helper VPC endpoint and its helper security group using the saved state file by default.

Current behavior:

- defaults to Region `ap-southeast-1`
- defaults to an `Interface` endpoint for service `com.amazonaws.<region>.s3`
- uses the Region's default VPC and default subnets unless overrides are supplied
- creates or reuses a security group that allows TCP/443 from the VPC CIDR
- tags the VPC endpoint and security group with:
  - `environment=development`
  - `project=clouddash`
- writes local state to `commands/network/.state/current_vpc_endpoint.txt`
- the destroy helper reads that state file, deletes the endpoint first, then removes the helper security group when it is no longer referenced

### EKS Helpers

- `commands/eks/` contains cluster creation, teardown, access-grant, and sample workload scripts for EKS testing.

### EC2 / ASG Helper

- `commands/ec2/create_asg_alb_stack.sh` creates or updates a small web stack for ASG-based resilience testing.
- `commands/ec2/destroy_asg_alb_stack.sh` tears down that ASG web stack using the saved local state by default.

Current behavior:

- uses `t3.micro`
- resolves the latest Amazon Linux 2023 AMI from the public SSM parameter
- installs and starts `nginx` with EC2 user data
- creates an internet-facing ALB on port `80`
- creates an EC2 security group that only allows port `80` from the ALB security group
- tags launched instances with:
  - `environment=development`
  - `project=clouddash`
- defaults to the Region's default VPC and its default subnets unless overrides are supplied
- writes stack state to `commands/ec2/.state/current_asg_stack.txt`
- writes the current ALB DNS name to `commands/ec2/.state/current_asg_alb_dns.txt`

Example:

```bash
./commands/ec2/create_asg_alb_stack.sh
./commands/ec2/create_asg_alb_stack.sh --region ap-southeast-1 --max 2 --desired 2
./commands/ec2/destroy_asg_alb_stack.sh
```

### RDS / Aurora Global Database Helper

- `commands/rds/create_aurora_global_db.sh` creates or reuses a minimal Aurora Global Database test stack with:
  - a primary cluster in `ap-southeast-1`
  - a secondary cluster in `ap-southeast-2`
  - one `db.r6g.large` instance in each Region
- `commands/rds/destroy_aurora_global_db.sh` tears down that Aurora Global Database stack using the saved local state by default.

Current behavior:

- uses `aurora-mysql`
- uses the Region's default VPC and default subnets unless VPC overrides are supplied
- creates one security group per Region with no inbound rules
- tags the Aurora clusters and instances with:
  - `environment=development`
  - `project=clouddash`
- writes stack state to `commands/rds/.state/current_aurora_global_db.txt`
- generates a master password when one is not provided and stores it in the state file

### S3 MRAP Helper

- `commands/s3/create_s3_mrap_stack.sh` creates or reuses a minimal S3 Multi-Region Access Point test stack with:
  - one bucket in `ap-southeast-1`
  - one bucket in `ap-southeast-2`
  - bidirectional replication between the two buckets
  - one MRAP spanning both buckets
  - MRAP route state updated after creation so it becomes active/passive with `ap-southeast-1` active
  - one sample text object uploaded to the primary bucket after replication is configured
- `commands/s3/destroy_s3_mrap_stack.sh` tears down that S3 MRAP stack using the saved local state by default, waits for the MRAP itself to disappear before deleting the buckets, and handles already-empty versioned buckets correctly.

Current behavior:

- uses `eu-west-1` as the default MRAP failover-control Region
- uses `us-west-2` for MRAP management APIs
- creates deterministic bucket names based on the base name, account ID, and Region
- creates one IAM role plus one inline policy for S3 replication
- tags the buckets with:
  - `environment=development`
  - `project=clouddash`
- writes stack state to `commands/s3/.state/current_s3_mrap_stack.txt`
- writes a sample text file to `commands/s3/.state/sample_replication_object.txt`

### S3 Replication Helper

- `commands/s3/create_s3_replication_stack.sh` creates or reuses a minimal one-way S3 replication test stack with:
  - one versioned source bucket in `ap-southeast-1`
  - one versioned destination bucket in `ap-southeast-2`
  - one-way replication from the primary bucket to the secondary bucket
  - one sample text object uploaded to the primary bucket after replication is configured
- `commands/s3/destroy_s3_replication_stack.sh` tears down that one-way replication stack using the saved local state by default, removes the replication rule from the source bucket, empties both versioned buckets, deletes them, and removes the IAM role and inline policy used for replication.

Current behavior:

- creates deterministic bucket names based on the base name, account ID, and Region
- creates one IAM role plus one inline policy for one-way S3 replication
- tags the buckets with:
  - `environment=development`
  - `project=clouddash`
- writes stack state to `commands/s3/.state/current_s3_replication_stack.txt`
- writes a sample text file to `commands/s3/.state/sample_pause_replication_object.txt`

### EFS Replication Helper

- `commands/efs/create_efs_replication_stack.sh` creates or reuses a minimal one-way EFS replication test stack with:
  - one EFS file system in `ap-southeast-1`
  - one EFS file system in `ap-southeast-2`
  - one-way replication from the primary file system to the secondary file system
  - the secondary file system reused as an existing destination for replication
- `commands/efs/destroy_efs_replication_stack.sh` tears down that EFS replication stack using the saved local state by default, deletes the replication configuration first, waits for it to be removed, then deletes both file systems.

Current behavior:

- uses deterministic `Name` tags based on the base name with `-primary` and `-secondary` suffixes
- tags both file systems with:
  - `environment=development`
  - `project=clouddash`
- creates file systems without mount targets, which is sufficient for testing `efs:failover` and `efs:failback`
- writes stack state to `commands/efs/.state/current_efs_replication_stack.txt`

Example:

```bash
./commands/efs/create_efs_replication_stack.sh
./commands/efs/create_efs_replication_stack.sh --name clouddash-efs --primary-region ap-southeast-1 --secondary-region ap-southeast-2
./commands/efs/destroy_efs_replication_stack.sh
```

### Run a Component or Site Test

From `scripts/`:

```powershell
python main.py --manifest ..\manifests\component-ec2.yml --fis-role-arn <fis-role-arn>
```

To persist the run into PostgreSQL at the same time:

```powershell
python main.py --manifest ..\manifests\component-ec2.yml --fis-role-arn <fis-role-arn> --db-dsn "host=<host> dbname=<db> user=<user> password=<password> sslmode=require"
```

To bypass pre-execution validations for a one-off run:

```powershell
python main.py --manifest ..\manifests\component-ec2.yml --fis-role-arn <fis-role-arn> --skip-validation
```

### Run a Region Test

From `scripts/`:

```powershell
python main.py --manifest ..\manifests\geo-rds.yml --arc-role-arn <arc-role-arn>
```

### Dry Run

Dry run writes payload and discovery artifacts but does not create or execute the remote action:

```powershell
python main.py --manifest ..\manifests\geo-rds.yml --arc-role-arn <arc-role-arn> --dry-run
```

### Runtime Logging

During execution, the framework now prints lightweight progress logs:

- custom actions log when each action starts, finishes, or is skipped
- FIS runs log generic `FIS is running` progress messages while the experiment is active
- ARC runs log generic `ARC is running` progress messages while the region action is active

## IAM Policy Template

The repository now includes a shared FIS experiment-role policy template at [iam/fis-experiment-role-policy.json](c:/OneDrive/Documents/Codes/resilience/iam/fis-experiment-role-policy.json).

Before using it, replace the placeholders:

- `123456789012` with your AWS account ID
- the example IAM role ARNs under `ec2:FisTargetArns` with the real roles you allow for `ec2:pause-launch`
- the wildcard Auto Scaling scope with a narrower ARN pattern if you know the target ASG names
- `your-source-bucket-1` and `your-source-bucket-2` with the real S3 source buckets used by `s3:pause-replication`
- the example destination Regions under `s3:DestinationRegion` with the real replication target Regions for your buckets

Optional tightening:

- add `kms:CreateGrant` only if you use `ec2:stop` with restart-after-duration on encrypted EBS volumes
- add `vpce:AllowMultiRegion` only if you use `network:disrupt-vpc-endpoint` against cross-Region VPC endpoints
- split this shared policy into smaller action-family policies if you want stricter least privilege than one shared FIS role

The repository also includes an ARC Region switch execution-role policy template at [iam/arc-region-switch-execution-role-policy.json](c:/OneDrive/Documents/Codes/resilience/iam/arc-region-switch-execution-role-policy.json).

That template is scoped to the ARC features currently implemented in this repo:

- Aurora Global Database failover
- Aurora Global Database switchover

Before using it, replace the placeholders:

- `123456789012` with your AWS account ID
- `your-global-cluster-id` with the Aurora Global Database identifier
- `your-primary-cluster-id` and `your-secondary-cluster-id` with the member cluster identifiers in each Region
- `ap-southeast-1` and `ap-southeast-2` with your real primary and secondary Regions if different

If your Aurora Global Database has more than two member Regions, add the additional cluster ARNs to the same statement.

For the EC2 instance profile that runs `python main.py`, the repository now also includes:

- [iam/ec2-runner-role-policy.json](c:/OneDrive/Documents/Codes/resilience/iam/ec2-runner-role-policy.json)
- [iam/ec2-runner-role-trust-policy.json](c:/OneDrive/Documents/Codes/resilience/iam/ec2-runner-role-trust-policy.json)

The runner-role policy covers what the current codebase needs to do from the EC2 host:

- create and start FIS experiments
- create and start ARC Region switch plans
- pass the FIS experiment role and ARC execution role
- run current custom actions for EC2, RDS, Aurora Global Database, ASG, EFS, EKS, Route 53 DNS, and S3 MRAP
- perform resource discovery, validation, and CloudWatch / load balancer observability lookups

Before using it, replace the placeholders:

- `123456789012` with your AWS account ID
- `arn:aws:iam::123456789012:role/your-fis-experiment-role` with the real FIS experiment role ARN
- `arn:aws:iam::123456789012:role/your-arc-region-switch-execution-role` with the real ARC execution role ARN

Optional additions that are intentionally not in the shared runner policy template:

- `ssm:GetParameters` if you use `--upload-artifactory`, because the current upload helper reads an SSM parameter for the Artifactory credential
- tighter resource scopes for specific EC2 instances, RDS resources, EFS file systems, Route 53 hosted zones, or EKS clusters once your production target set is known

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
5. There is no dedicated test suite in the repository yet.

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




