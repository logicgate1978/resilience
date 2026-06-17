# Resilience Automation Tool - Control Overview

## Purpose

This document explains the key controls implemented around the resilience automation tool. The tool is used by application teams to run controlled resilience tests against cloud resources through the bank's Azure DevOps (ADO) pipeline.

The controls are designed to make sure that:

- application teams can only operate AWS accounts or Azure subscriptions that belong to their own application;
- the environment selected in the pipeline matches the actual environment of the AWS account or Azure subscription;
- resilience tests are reviewed before execution;
- the proposed changes are visible to the approving Production Service Support (PSS) team member;
- execution evidence is retained through generated artifacts and ADO pipeline artifacts.

## Control 1: Dry-Run Plan and PSS Review

### Control Objective

The resilience test must not execute immediately after an application team starts the ADO pipeline. The proposed actions must first be shown to the application support team for review and approval.

### How the Control Works

The ADO pipeline runs the resilience tool in three controlled steps.

Step 1: Dry-run planning

The pipeline first runs the script with the selected manifest file in `--dry-run` mode. In this mode, the tool does not make the actual resilience change. Instead, it prepares and prints an execution plan.

The dry-run plan shows:

- the actions that will be performed;
- the execution engine used for each action;
- the order of execution, including any dependencies;
- the cloud resources that may be impacted;
- the planned changes or key parameters for each action.

Step 2: PSS review and approval

After the dry-run plan is generated, a PSS team member must review and approve the plan in ADO before the pipeline can continue.

The PSS approver must belong to the same application team as the pipeline request. The application team is identified by ITAM. For example, a PSS member for ITAM `50705` must not approve a resilience test for ITAM `14147`.

This approval step gives the application support team an opportunity to confirm that the planned test is expected, safe, and aligned with the application team's operational responsibility.

Step 3: Actual execution

Only after the PSS approval is completed does the ADO pipeline run the script again without `--dry-run`. This second run performs the actual resilience test.

### Failure or Stop Condition

If the PSS team member does not approve the dry-run plan, the pipeline does not continue to the execution step.

If the dry-run plan is incorrect, unclear, or not approved, the resilience test is not executed.

### Evidence Produced

The dry-run execution produces an approval artifact containing the execution plan and impacted resource details. This artifact can be attached to the ADO pipeline run and retained as evidence of the review.

The ADO approval record provides evidence of who approved the run and when approval was given.

## Control 2: ITAM and Cloud Account Validation

### Control Objective

An application team must only be able to run resilience tests against cloud accounts or subscriptions that belong to its own application.

For example, ITAM `50705` must not be allowed to run resilience tests against AWS accounts or Azure subscriptions owned by ITAM `14147`.

### How the Control Works

When database controls are enabled, the tool validates the relationship between the ITAM value and the target cloud account or subscription before the resilience test proceeds.

The ADO pipeline passes the ITAM and target cloud account identifier into the script. For AWS this is the AWS account ID. For Azure this is the Azure subscription ID. The script checks the bank's control database to confirm that the account or subscription is mapped to the same application ID.

The validation uses the account mapping table:

```text
resilience.account_environments
```

The tool checks the `app_id` associated with the account or subscription and compares it with the ITAM value supplied to the pipeline.

### Failure or Stop Condition

If the target account or subscription is not found in the control database, the tool stops.

If the target account or subscription belongs to a different ITAM, the tool stops.

The resilience test does not proceed until the ITAM and cloud account mapping is valid.

### Evidence Produced

The pipeline log records whether the ITAM and account validation passed or failed.

When database persistence is enabled, the run metadata and validation state can also be retained in the PostgreSQL database.

## Control 3: Cloud Account and Environment Validation

### Control Objective

The environment selected in the ADO pipeline must match the actual environment of the AWS account or Azure subscription.

This reduces the risk of a user accidentally selecting the wrong environment, such as running a production-style test against a non-production account or selecting non-production while targeting a production account or subscription.

### How the Control Works

When database controls are enabled, the tool validates the relationship between the target cloud account or subscription and the environment before the resilience test proceeds.

The ADO pipeline passes the cloud account identifier and environment into the script. The script checks the bank's control database to confirm the environment mapped to that account or subscription.

The validation uses the account mapping table:

```text
resilience.account_environments
```

The tool compares the database environment with the environment supplied through the pipeline.

### Failure or Stop Condition

If the target account or subscription is not found in the control database, the tool stops.

If the environment in the database does not match the environment selected in the ADO pipeline, the tool stops.

The resilience test does not proceed until the account and environment mapping is valid.

### Evidence Produced

The pipeline log records whether the account and environment validation passed or failed.

When database persistence is enabled, the validation result can also be retained in the PostgreSQL database.

## Control 4: Pre-Execution Validation Controls

### Control Objective

Before the tool performs a resilience test, it should check that the requested action is valid and that the target resources are suitable for the action.

This helps prevent avoidable execution errors and reduces the risk of running a test against the wrong or unsupported resources.

### How the Control Works

After the identity and account checks pass, the tool performs pre-execution validations based on the actions in the manifest file.

Examples of these validations include:

- checking that selected cloud resources exist;
- checking that DNS records exist before DNS changes are planned;
- checking that DNS values or weighted DNS targets are valid;
- checking that Auto Scaling Group scale values are valid;
- checking that EKS deployments or node groups exist before scaling;
- checking that RDS failover targets are valid;
- checking that EFS replication configuration exists before EFS failover;
- checking that S3 Multi-Region Access Point failover settings are valid;
- checking that VPC endpoint disruption targets are valid.

These validations run before the actual resilience action starts.

### Failure or Stop Condition

If a required validation fails, the tool stops before executing the resilience test.

The user must correct the manifest, target resource, or pipeline input before running the test again.

### Validation Skip Control

The tool has a validation skip option for exceptional cases. If this is used, the pipeline log records that validation was skipped.

This skip option should be governed by the pipeline process and used only where appropriate, because it bypasses the pre-execution validation layer.

### Evidence Produced

Validation progress and results are printed in the pipeline log.

When database persistence is enabled, validation results can also be stored in the PostgreSQL database as passed, failed, or skipped.

## Control 5: Generated Artifacts and Audit Evidence

### Control Objective

Each resilience test should produce evidence that can be reviewed after the pipeline run. This supports operational review, audit review, and post-test analysis.

### How the Control Works

The tool generates artifacts during dry-run and execution. These artifacts are saved by the pipeline and can be attached to the ADO pipeline run.

Typical artifacts include:

- dry-run approval summary;
- impacted resource details;
- generated execution plan;
- result summary;
- validation outcomes;
- observability and metric outputs where configured;
- HTML report generated after execution.

The dry-run approval artifact is especially important because it shows what the PSS approver reviewed before execution.

The execution artifacts show what actually happened during the resilience test.

### Failure or Stop Condition

If the dry-run plan cannot be generated, the pipeline should not proceed to approval or execution.

If execution fails, the result artifacts and logs should still be retained so the application team can review the failure.

### Evidence Produced

The artifacts are attached to the ADO pipeline as pipeline artifacts.

These artifacts provide evidence for:

- what was requested;
- what was reviewed;
- who approved the run in ADO;
- which resources were targeted;
- what changes were planned;
- what actions were executed;
- whether validations passed, failed, or were skipped;
- what the final result of the resilience test was.

## Summary

The main control design is that the resilience test is not a one-step execution. It is a controlled pipeline process:

1. Generate a dry-run plan.
2. Require review and approval by the correct PSS team.
3. Validate that the ITAM owns the cloud account or subscription.
4. Validate that the selected environment matches the cloud account or subscription.
5. Validate that the target resources and requested actions are suitable.
6. Execute the resilience test only after the required controls pass.
7. Retain generated artifacts and ADO approval evidence for audit.

Together, these controls help ensure that resilience testing is authorized, reviewed, traceable, and limited to the correct application-owned cloud accounts and subscriptions.
