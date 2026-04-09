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

<table>
  <thead>
    <tr>
      <th>Action</th>
      <th>Implementation</th>
      <th>Description</th>
      <th>FIS/ARC action</th>
    </tr>
  </thead>
  <tbody>
    <tr><th colspan="4" align="left">Common</th></tr>
    <tr><td><code>common:wait</code></td><td>FIS or custom</td><td>Pause execution for a fixed duration between other actions. Uses FIS by default, or Python sleep when <code>service.use_fis: false</code>.</td><td><code>aws:fis:wait</code></td></tr>
    <tr><th colspan="4" align="left">DNS</th></tr>
    <tr><td><code>dns:set-value</code></td><td>Custom</td><td>Update the value of a simple Route 53 DNS record for component or region workflows.</td><td></td></tr>
    <tr><td><code>dns:set-weight</code></td><td>Custom</td><td>Update Route 53 weighted-routing record weights by set identifier for component or region workflows.</td><td></td></tr>
    <tr><th colspan="4" align="left">EC2</th></tr>
    <tr><td><code>ec2:pause-launch</code></td><td>FIS</td><td>Simulate insufficient EC2 capacity for instance launches in a site/AZ-scoped test.</td><td><code>aws:ec2:api-insufficient-instance-capacity-error</code></td></tr>
    <tr><td><code>ec2:stop</code></td><td>FIS or custom</td><td>Stop selected EC2 instances. Uses FIS by default, or boto3 when <code>service.use_fis: false</code>. If <code>service.duration</code> is provided, the framework maps it to auto-restart behavior; if it is omitted, the instances remain stopped.</td><td><code>aws:ec2:stop-instances</code></td></tr>
    <tr><td><code>ec2:reboot</code></td><td>FIS or custom</td><td>Reboot selected EC2 instances. Uses FIS by default, or boto3 when <code>service.use_fis: false</code>. <code>service.duration</code> is optional and ignored in both paths.</td><td><code>aws:ec2:reboot-instances</code></td></tr>
    <tr><td><code>ec2:terminate</code></td><td>FIS or custom</td><td>Terminate selected EC2 instances. Uses FIS by default, or boto3 when <code>service.use_fis: false</code>. Terminated instances are not restarted.</td><td><code>aws:ec2:terminate-instances</code></td></tr>
    <tr><th colspan="4" align="left">RDS</th></tr>
    <tr><td><code>rds:reboot</code></td><td>FIS or custom</td><td>Reboot selected RDS DB instances. Uses FIS by default, or boto3 when <code>service.use_fis: false</code>.</td><td><code>aws:rds:reboot-db-instances</code></td></tr>
    <tr><td><code>rds:failover</code></td><td>FIS or custom</td><td>Fail over a selected RDS or Aurora DB cluster to a replica. Uses FIS by default, or boto3 when <code>service.use_fis: false</code>.</td><td><code>aws:rds:failover-db-cluster</code></td></tr>
    <tr><td><code>rds:failover-global-db</code></td><td>ARC or custom</td><td>Fail over an Aurora Global Database across Regions. Uses ARC when <code>use_arc: true</code>; otherwise uses a custom boto3 RDS implementation.</td><td><code>AuroraGlobalDatabase</code></td></tr>
    <tr><td><code>rds:switchover-global-db</code></td><td>ARC or custom</td><td>Switchover an Aurora Global Database across Regions. Uses ARC when <code>use_arc: true</code>; otherwise uses a custom boto3 RDS implementation.</td><td><code>AuroraGlobalDatabase</code></td></tr>
    <tr><th colspan="4" align="left">ASG</th></tr>
    <tr><td><code>asg:pause-launch</code></td><td>FIS</td><td>Simulate insufficient capacity for Auto Scaling launches in a site/AZ-scoped test.</td><td><code>aws:ec2:asg-insufficient-instance-capacity-error</code></td></tr>
    <tr><td><code>asg:scale</code></td><td>Custom</td><td>Scale Auto Scaling Groups by updating min, max, and desired capacity through the Auto Scaling API.</td><td></td></tr>
    <tr><th colspan="4" align="left">Network</th></tr>
    <tr><td><code>network:disrupt-connectivity</code></td><td>FIS</td><td>Disrupt connectivity for selected subnets.</td><td><code>aws:network:disrupt-connectivity</code></td></tr>
    <tr><th colspan="4" align="left">S3</th></tr>
    <tr><td><code>s3:pause-replication</code></td><td>FIS</td><td>Pause replication from source S3 buckets to destination buckets.</td><td><code>aws:s3:bucket-pause-replication</code></td></tr>
    <tr><td><code>s3:failover</code></td><td>Custom</td><td>Fail over an S3 Multi-Region Access Point by making one target Region active and all other configured MRAP regions passive.</td><td></td></tr>
    <tr><th colspan="4" align="left">EFS</th></tr>
    <tr><td><code>efs:failover</code></td><td>Custom</td><td>Delete the EFS replication configuration for the selected file system so the destination becomes writable.</td><td></td></tr>
    <tr><th colspan="4" align="left">EKS</th></tr>
    <tr><td><code>eks:delete-pod</code></td><td>FIS</td><td>Delete selected EKS pods by namespace and selector.</td><td><code>aws:eks:pod-delete</code></td></tr>
    <tr><td><code>eks:pod-cpu-stress</code></td><td>FIS</td><td>Run CPU stress against selected EKS pods.</td><td><code>aws:eks:pod-cpu-stress</code></td></tr>
    <tr><td><code>eks:pod-io-stress</code></td><td>FIS</td><td>Run I/O stress against selected EKS pods.</td><td><code>aws:eks:pod-io-stress</code></td></tr>
    <tr><td><code>eks:pod-memory-stress</code></td><td>FIS</td><td>Run memory stress against selected EKS pods.</td><td><code>aws:eks:pod-memory-stress</code></td></tr>
    <tr><td><code>eks:terminate-nodegroup-instances</code></td><td>FIS</td><td>Terminate a percentage of instances in an Amazon EKS managed node group.</td><td><code>aws:eks:terminate-nodegroup-instances</code></td></tr>
    <tr><td><code>eks:scale-deployment</code></td><td>Custom</td><td>Scale a Kubernetes Deployment in an EKS cluster through the Kubernetes API for component or region workflows.</td><td></td></tr>
  </tbody>
</table>

## Manifest Design

<table>
  <thead>
    <tr>
      <th>Field</th>
      <th>Required</th>
      <th>Applies To</th>
      <th>Description</th>
    </tr>
  </thead>
  <tbody>
    <tr><th colspan="4" align="left">Top-Level Fields</th></tr>
    <tr><td><code>region</code></td><td>Optional default</td><td>FIS actions, single-Region custom actions</td><td>Default AWS Region for execution, resource discovery, and observability. A service-level <code>service.region</code> overrides it. For <code>s3:failover</code>, this is the S3 MRAP failover-control endpoint Region and defaults to <code>eu-west-1</code> when omitted. For custom-only global actions such as Route 53 DNS, the framework can otherwise fall back to <code>AWS_REGION</code>, <code>AWS_DEFAULT_REGION</code>, or <code>us-east-1</code> when no region is declared.</td></tr>
    <tr><td><code>zone</code></td><td>Optional default</td><td>AZ-scoped actions</td><td>Default Availability Zone scope. A service-level <code>service.zone</code> overrides it.</td></tr>
    <tr><td><code>primary_region</code></td><td>Optional default</td><td>Aurora Global Database actions</td><td>Default current active Region for cross-Region Aurora Global Database actions. A service-level <code>service.primary_region</code> overrides it.</td></tr>
    <tr><td><code>secondary_region</code></td><td>Optional default</td><td>Aurora Global Database actions</td><td>Default standby Region for cross-Region Aurora Global Database actions. A service-level <code>service.secondary_region</code> overrides it.</td></tr>
    <tr><td><code>services</code></td><td>Yes</td><td>All manifests</td><td>List of service/action blocks that describe what resilience action to run.</td></tr>
    <tr><td><code>observability</code></td><td>No</td><td>All manifests</td><td>Optional configuration for health checks and CloudWatch metric collection around the experiment window.</td></tr>
    <tr><th colspan="4" align="left">Service Block Fields</th></tr>
    <tr><td><code>service.name</code></td><td>Yes</td><td>All service blocks</td><td>Logical service name such as <code>common</code>, <code>ec2</code>, <code>rds</code>, <code>asg</code>, <code>network</code>, <code>s3</code>, or <code>eks</code>.</td></tr>
    <tr><td><code>service.action</code></td><td>Yes</td><td>All service blocks</td><td>Action to run for that service, for example <code>terminate</code>, <code>reboot</code>, <code>failover</code>, or <code>delete-pod</code>.</td></tr>
    <tr><td><code>service.region</code></td><td>Optional</td><td>Region-scoped actions</td><td>Action-specific Region override. When present, it takes precedence over top-level <code>region</code>. For <code>s3:failover</code>, the allowed values are the S3 MRAP failover-control Regions, and the default is <code>eu-west-1</code>.</td></tr>
    <tr><td><code>service.zone</code></td><td>Optional</td><td>AZ-scoped actions</td><td>Action-specific Availability Zone override. When present, it takes precedence over top-level <code>zone</code>. For <code>asg:pause-launch</code>, the zone is passed via the FIS action parameter <code>availabilityZoneIdentifiers</code>, not as a target filter.</td></tr>
    <tr><td><code>service.primary_region</code></td><td>Optional</td><td>Aurora Global Database actions</td><td>Action-specific active Region override for Aurora Global Database actions. When present, it takes precedence over top-level <code>primary_region</code>.</td></tr>
    <tr><td><code>service.secondary_region</code></td><td>Optional</td><td>Aurora Global Database actions</td><td>Action-specific standby Region override for Aurora Global Database actions. When present, it takes precedence over top-level <code>secondary_region</code>.</td></tr>
    <tr><td><code>service.start_after</code></td><td>Optional</td><td>All service blocks</td><td>Dependency list for ordered execution. When omitted, actions run in parallel by default. Use <code>&lt;service&gt;:&lt;action&gt;</code> when that action is unique in the manifest, or <code>&lt;service&gt;:&lt;action&gt;#&lt;n&gt;</code> when the same service/action appears multiple times.</td></tr>
    <tr><td><code>service.tags</code></td><td>Usually yes</td><td>Tag-discovered actions</td><td>Comma-separated <code>key=value</code> filters used to discover real AWS resources. Current discovery logic uses AND semantics across all tags. For Aurora Global Database actions, either <code>service.tags</code>, <code>service.identifier</code>, or both may be used.</td></tr>
    <tr><td><code>service.identifier</code></td><td>Optional</td><td><code>rds:reboot</code>, <code>rds:failover</code>, <code>rds:failover-global-db</code>, <code>rds:switchover-global-db</code></td><td>Optional exact identifier used for RDS lookup. For <code>rds:reboot</code> use a DB instance identifier, for <code>rds:failover</code> use a DB cluster identifier, and for Aurora Global Database actions use the global cluster identifier. If both <code>service.identifier</code> and <code>service.tags</code> are present, both filters must match.</td></tr>
    <tr><td><code>service.value</code></td><td>Yes for some actions</td><td><code>dns:set-value</code>, <code>dns:set-weight</code></td><td>Action-specific value payload. For <code>dns:set-value</code>, this is the target record value. For <code>dns:set-weight</code>, this is a comma-separated list like <code>primary=0, secondary=100</code>.</td></tr>
    <tr><td><code>service.duration</code></td><td>Depends on action</td><td>Actions that require a time window</td><td>ISO-8601 duration such as <code>PT30M</code>. Used by actions like <code>common:wait</code>, <code>ec2:pause-launch</code>, <code>asg:pause-launch</code>, and <code>network:disrupt-connectivity</code>. For <code>ec2:stop</code>, it is optional and only controls when the framework starts the instances again after stopping them.</td></tr>
    <tr><td><code>service.instance_count</code></td><td>Optional</td><td><code>ec2</code> instance actions</td><td>Narrows selected EC2 instances to the first N deterministic matches. Used for <code>stop</code>, <code>reboot</code>, and <code>terminate</code>.</td></tr>
    <tr><td><code>service.iam_roles</code></td><td>Optional</td><td><code>ec2:pause-launch</code></td><td>Comma-separated IAM role names to resolve for the EC2 capacity-error action.</td></tr>
    <tr><td><code>service.iam_role_arns</code></td><td>Optional</td><td><code>ec2:pause-launch</code></td><td>Explicit IAM role ARNs to target instead of resolving <code>iam_roles</code>.</td></tr>
    <tr><td><code>service.destination_region</code></td><td>Yes for <code>s3:pause-replication</code></td><td><code>s3:pause-replication</code></td><td>Region where the destination replication buckets are located.</td></tr>
    <tr><td><code>service.destination_buckets</code></td><td>Optional</td><td><code>s3:pause-replication</code></td><td>Optional list of destination S3 bucket names to narrow which replication destinations are paused.</td></tr>
    <tr><td><code>service.prefixes</code></td><td>Optional</td><td><code>s3:pause-replication</code></td><td>Optional list of S3 object key prefixes to narrow replication rules that are paused.</td></tr>
    <tr><td><code>service.timeout_seconds</code></td><td>Optional</td><td><code>s3:failover</code></td><td>Per-action timeout for waiting until the MRAP routing state reflects the requested failover. Defaults to the framework timeout when omitted.</td></tr>
    <tr><td><code>service.from</code></td><td>Yes for ARC Aurora Global Database actions; optional legacy selector for custom actions</td><td><code>rds:failover-global-db</code>, <code>rds:switchover-global-db</code></td><td>Direction selector for ARC-backed Aurora Global Database actions. Accepted values are <code>primary</code>, <code>secondary</code>, the configured <code>primary_region</code>, or the configured <code>secondary_region</code>. When <code>service.use_arc = false</code>, prefer <code>service.target_region</code> instead.</td></tr>
    <tr><td><code>service.target_region</code></td><td>Yes for non-ARC Aurora Global Database actions</td><td><code>rds:failover-global-db</code>, <code>rds:switchover-global-db</code> with <code>use_arc: false</code></td><td>Explicit member Region to promote when using the custom boto3 path for Aurora Global Database actions. This avoids ambiguity when the global database has more than two Regions.</td></tr>
    <tr><td><code>service.use_arc</code></td><td>Optional</td><td><code>rds:failover-global-db</code>, <code>rds:switchover-global-db</code></td><td>Chooses the execution engine for supported Aurora Global Database actions. <code>true</code> uses ARC Region switch; <code>false</code> uses a custom boto3 implementation.</td></tr>
    <tr><td><code>service.use_fis</code></td><td>Optional</td><td><code>common:wait</code>, <code>ec2:stop</code>, <code>ec2:reboot</code>, <code>ec2:terminate</code>, <code>rds:reboot</code>, <code>rds:failover</code></td><td>Chooses the execution engine for supported dual-path actions. Defaults to <code>true</code>. When set to <code>false</code>, the framework uses its custom Python or boto3 implementation instead of FIS.</td></tr>
    <tr><td><code>service.wait_for_ready</code></td><td>Optional</td><td><code>efs:failover</code>, <code>s3:failover</code></td><td>Whether the custom action waits until the target control-plane state is fully applied before completing. For <code>efs:failover</code>, this means the replication configuration has been deleted. For <code>s3:failover</code>, this means the MRAP route state shows the target Region active and all other Regions passive. Defaults to <code>true</code>.</td></tr>
    <tr><td><code>service.target</code></td><td>Required for structured actions</td><td><code>eks</code> structured actions, custom actions</td><td>Nested target object for actions that need more than tag-based selection.</td></tr>
    <tr><td><code>service.parameters</code></td><td>Required for many structured actions</td><td><code>eks</code> structured actions, custom actions</td><td>Nested action-parameter object for actions that require extra runtime parameters.</td></tr>
    <tr><td><code>service.kubernetes_service_account</code></td><td>Optional container for a required value</td><td>Supported EKS pod actions</td><td>Can be supplied directly on the service block instead of under <code>service.parameters.kubernetes_service_account</code>. The service account value itself is still required for supported EKS pod actions.</td></tr>
    <tr><th colspan="4" align="left">Service Target Fields</th></tr>
    <tr><td><code>service.target.cluster_identifier</code></td><td>Yes for EKS actions</td><td>EKS actions</td><td>EKS cluster name used by either the FIS pod target or the custom deployment scaler.</td></tr>
    <tr><td><code>service.target.hosted_zone</code></td><td>Yes for DNS actions</td><td>Route 53 DNS actions</td><td>Hosted zone name used to resolve the Route 53 hosted zone, for example <code>example.com</code>.</td></tr>
    <tr><td><code>service.target.record_name</code></td><td>Yes for DNS actions</td><td>Route 53 DNS actions</td><td>Fully qualified DNS record name, for example <code>dev.example.com</code>.</td></tr>
    <tr><td><code>service.target.record_type</code></td><td>Yes for DNS actions</td><td>Route 53 DNS actions</td><td>Route 53 record type such as <code>A</code>, <code>AAAA</code>, or <code>CNAME</code>.</td></tr>
    <tr><td><code>service.target.mrap_name</code></td><td>Exactly one MRAP selector is required for <code>s3:failover</code></td><td><code>s3:failover</code></td><td>Multi-Region Access Point name used with S3 Control <code>GetMultiRegionAccessPoint</code>.</td></tr>
    <tr><td><code>service.target.mrap_alias</code></td><td>Exactly one MRAP selector is required for <code>s3:failover</code></td><td><code>s3:failover</code></td><td>Multi-Region Access Point alias used to discover the MRAP through the S3 Control listing API.</td></tr>
    <tr><td><code>service.target.mrap_arn</code></td><td>Exactly one MRAP selector is required for <code>s3:failover</code></td><td><code>s3:failover</code></td><td>Multi-Region Access Point ARN used to discover the MRAP through the S3 Control listing API.</td></tr>
    <tr><td><code>service.target.target_region</code></td><td>Yes for <code>s3:failover</code></td><td><code>s3:failover</code></td><td>MRAP member Region that should become active after failover. The action sets this Region to traffic dial <code>100</code> and all other configured MRAP Regions to <code>0</code>.</td></tr>
    <tr><td><code>service.target.namespace</code></td><td>Yes for pod-targeted EKS actions and <code>eks:scale-deployment</code></td><td>EKS actions</td><td>Kubernetes namespace containing the target pods or target Deployment.</td></tr>
    <tr><td><code>service.target.selector_type</code></td><td>Yes for pod-targeted EKS actions</td><td>EKS pod actions</td><td>Selector type passed to FIS. The current manifest examples use <code>labelSelector</code>.</td></tr>
    <tr><td><code>service.target.selector_value</code></td><td>Yes for pod-targeted EKS actions</td><td>EKS pod actions</td><td>Selector expression used to match pods, for example <code>app=my-service</code>.</td></tr>
    <tr><td><code>service.target.count</code></td><td>Optional</td><td>EKS pod actions</td><td>Number of matching resources to target. Converted into FIS <code>COUNT(n)</code> selection mode.</td></tr>
    <tr><td><code>service.target.selection_mode</code></td><td>Optional</td><td>EKS pod actions</td><td>Explicit FIS selection mode. If set, it overrides <code>count</code>.</td></tr>
    <tr><td><code>service.target.nodegroup_arn</code></td><td>Optional</td><td><code>eks:terminate-nodegroup-instances</code></td><td>Explicit managed node group ARN for the EKS node group termination action.</td></tr>
    <tr><td><code>service.target.nodegroup_arns</code></td><td>Optional</td><td><code>eks:terminate-nodegroup-instances</code></td><td>Explicit list of managed node group ARNs for the EKS node group termination action.</td></tr>
    <tr><td><code>service.target.deployment_name</code></td><td>Yes for <code>eks:scale-deployment</code></td><td><code>eks:scale-deployment</code></td><td>Kubernetes Deployment name to scale.</td></tr>
    <tr><th colspan="4" align="left">Service Parameter Fields</th></tr>
    <tr><td><code>service.parameters.max</code></td><td>Yes for <code>asg:scale</code></td><td><code>asg:scale</code></td><td>Target maximum capacity for the Auto Scaling Group.</td></tr>
    <tr><td><code>service.parameters.min</code></td><td>Optional</td><td><code>asg:scale</code></td><td>Target minimum capacity. Defaults to <code>0</code> when omitted.</td></tr>
    <tr><td><code>service.parameters.desired</code></td><td>Optional</td><td><code>asg:scale</code></td><td>Target desired capacity. Defaults to the configured <code>max</code> when omitted.</td></tr>
    <tr><td><code>service.parameters.wait_for_ready</code></td><td>Optional</td><td><code>asg:scale</code>, <code>eks:scale-deployment</code></td><td>Whether the custom scaler waits until the target reaches the requested steady state.</td></tr>
    <tr><td><code>service.parameters.timeout_seconds</code></td><td>Optional</td><td><code>asg:scale</code>, <code>eks:scale-deployment</code></td><td>Per-action timeout for custom readiness polling.</td></tr>
    <tr><td><code>service.parameters.kubernetes_service_account</code></td><td>Yes for supported EKS pod actions</td><td>EKS pod actions</td><td>Kubernetes service account name used by the FIS pod action inside the cluster. The <code>service.parameters</code> block itself is optional, but the service account value is still required either here or as <code>service.kubernetes_service_account</code>.</td></tr>
    <tr><td><code>service.parameters.grace_period_seconds</code></td><td>No</td><td><code>eks:delete-pod</code></td><td>Grace period before pod deletion.</td></tr>
    <tr><td><code>service.parameters.workers</code></td><td>No</td><td><code>eks:pod-cpu-stress</code>, <code>eks:pod-io-stress</code>, <code>eks:pod-memory-stress</code></td><td>Number of stress workers.</td></tr>
    <tr><td><code>service.parameters.percent</code></td><td>No</td><td><code>eks:pod-cpu-stress</code>, <code>eks:pod-io-stress</code>, <code>eks:pod-memory-stress</code></td><td>Stress intensity percentage.</td></tr>
    <tr><td><code>service.parameters.max_errors_percent</code></td><td>No</td><td>EKS pod actions</td><td>Allowed percentage of errors before FIS fails the action.</td></tr>
    <tr><td><code>service.parameters.instance_termination_percentage</code></td><td>Yes for <code>eks:terminate-nodegroup-instances</code></td><td><code>eks:terminate-nodegroup-instances</code></td><td>Percentage of managed node group instances to terminate.</td></tr>
    <tr><td><code>service.parameters.replicas</code></td><td>Yes for <code>eks:scale-deployment</code></td><td><code>eks:scale-deployment</code></td><td>Desired absolute replica count for the target Deployment.</td></tr>
    <tr><td><code>service.parameters.fis_pod_container_image</code></td><td>No</td><td>EKS pod actions</td><td>Optional custom container image for the helper pod used by the FIS action.</td></tr>
    <tr><td><code>service.parameters.fis_pod_labels</code></td><td>No</td><td>EKS pod actions</td><td>Optional labels applied to the FIS orchestration pod.</td></tr>
    <tr><td><code>service.parameters.fis_pod_annotations</code></td><td>No</td><td>EKS pod actions</td><td>Optional annotations applied to the FIS orchestration pod.</td></tr>
    <tr><td><code>service.parameters.fis_pod_security_policy</code></td><td>No</td><td>EKS pod actions</td><td>Optional Kubernetes Security Standards policy for the FIS orchestration pod and ephemeral containers.</td></tr>
    <tr><th colspan="4" align="left">Observability Fields</th></tr>
    <tr><td><code>observability.start_before</code></td><td>No</td><td>All manifests</td><td>Number of minutes to collect observability before starting the action.</td></tr>
    <tr><td><code>observability.stop_after</code></td><td>No</td><td>All manifests</td><td>Number of minutes to continue collecting observability after the action completes.</td></tr>
    <tr><td><code>observability.health_check</code></td><td>No</td><td>All manifests</td><td>Nested HTTP health-check configuration.</td></tr>
    <tr><td><code>observability.cloudwatch</code></td><td>No</td><td>All manifests</td><td>Nested CloudWatch metric collection configuration.</td></tr>
    <tr><th colspan="4" align="left">Observability Health Check Fields</th></tr>
    <tr><td><code>observability.health_check.endpoint</code></td><td>Yes when <code>observability.health_check</code> is used</td><td>Health check</td><td>HTTP endpoint to probe periodically.</td></tr>
    <tr><td><code>observability.health_check.http_method</code></td><td>No</td><td>Health check</td><td>HTTP method to use, typically <code>get</code>.</td></tr>
    <tr><td><code>observability.health_check.healthy_status_code</code></td><td>No</td><td>Health check</td><td>Expected healthy response code, typically <code>200</code>.</td></tr>
    <tr><td><code>observability.health_check.interval</code></td><td>No</td><td>Health check</td><td>Polling interval in seconds.</td></tr>
    <tr><th colspan="4" align="left">Observability CloudWatch Load Balancer Fields</th></tr>
    <tr><td><code>observability.cloudwatch.load_balancer.type</code></td><td>Yes when load balancer metrics are used</td><td>Load balancer metrics</td><td>Load balancer type such as <code>alb</code>.</td></tr>
    <tr><td><code>observability.cloudwatch.load_balancer.name</code></td><td>Optional</td><td>Load balancer metrics</td><td>Explicit load balancer name.</td></tr>
    <tr><td><code>observability.cloudwatch.load_balancer.tags</code></td><td>Optional</td><td>Load balancer metrics</td><td>Tag filters used to discover the load balancer when <code>name</code> is not supplied.</td></tr>
    <tr><td><code>observability.cloudwatch.load_balancer.metrics</code></td><td>Yes when load balancer metrics are used</td><td>Load balancer metrics</td><td>List of CloudWatch metric names to collect for the resolved load balancer.</td></tr>
  </tbody>
</table>

Important sequencing note:

- Actions now run in parallel by default when `service.start_after` is omitted.
- For native FIS actions, that matches FIS behavior when no `startAfter` is set.
- For custom-only manifests, the framework also uses dependency-driven execution and only waits when `service.start_after` is declared.

## Implemented Validations

Only actions listed in `scripts/validations/actions.yml` currently run pre-execution validations. If a validation fails, the framework stops before starting the action and returns a descriptive error.

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
| `eks` | `eks:scale-deployment` | `verify_deployment_existence` | `service.target.cluster_identifier`, `namespace`, and `deployment_name` are present, the Kubernetes API is reachable, and the target Deployment exists. |
| `eks` | `eks:scale-deployment` | `verify_replicas_value` | `service.parameters.replicas` exists, is an integer, and is greater than or equal to zero. |
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

1. resolves EFS file systems from `service.tags`
2. verifies that each selected file system has a replication configuration
3. deletes the replication configuration by calling `DeleteReplicationConfiguration` on the source file system
4. either:
   - completes immediately when `service.wait_for_ready = false`
   - or polls until the replication configuration is no longer returned by `DescribeReplicationConfigurations`

If your tag selection matches multiple file systems, the action operates on all of them. It fails fast when any selected file system does not have replication configured.

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
- `commands/s3/destroy_s3_mrap_stack.sh` tears down that S3 MRAP stack using the saved local state by default.

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
- the destroy helper empties both buckets before deleting them

Example:

```bash
./commands/rds/create_aurora_global_db.sh
./commands/rds/create_aurora_global_db.sh --name clouddash-global --engine-version <engine-version>
./commands/rds/destroy_aurora_global_db.sh
```

### Run a Component or Site Test

From `scripts/`:

```powershell
python main.py --manifest ..\manifests\component-ec2.yml --fis-role-arn <fis-role-arn>
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
