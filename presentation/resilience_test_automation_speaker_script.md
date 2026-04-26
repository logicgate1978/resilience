# Resilience Test Automation

Full speaker script for a 20 to 25 minute internal presentation to the cloud team.

Suggested audience:
- internal cloud platform team
- operations / SRE stakeholders
- security / governance stakeholders who care about control and auditability

Suggested duration:
- 20 to 25 minutes total
- around 12 to 14 minutes of slides
- around 8 to 10 minutes of demo

---

## Slide 1: Title

Title:
- `Resilience Test Automation for AWS Workloads`

Speaker script:

"Hi everyone. Today I want to walk through the resilience test automation framework that we have been building for AWS workloads.

The goal of the session is to show three things.

First, what problem the framework is solving.

Second, how the framework works at a practical level.

And third, why it is safe and usable enough for teams to adopt as part of controlled resilience testing rather than as a collection of one-off scripts.

I will keep the architecture explanation reasonably high level, and I will spend more time on the operational flow and the demo, because that is where the value is easiest to see."

Transition:

"Let me start with the problem we are trying to solve."

---

## Slide 2: Why This Is Needed

Title:
- `Why Resilience Testing Is Still Hard`

Speaker script:

"In most environments, resilience testing is still harder than it should be.

Usually the challenges are not just technical. They are operational.

The first problem is that tests are often manual. Someone has to remember the exact commands, exact target resources, and exact recovery expectations.

The second problem is inconsistency. Two teams may want to test the same type of failure, but they do it in completely different ways.

The third problem is approvals. Before a test runs, approvers want to know exactly what is going to be impacted. In many cases that visibility is weak.

The fourth problem is that resilience actions span different AWS control planes. Some actions are available in FIS, some are better handled through ARC, and some are not supported natively and need custom handling.

And finally, evidence is often poor. Even if the test runs successfully, it can be difficult to answer basic questions later, like what was targeted, what was executed, and what the outcome was.

So the problem is not simply how to inject a failure.

The problem is how to do resilience testing in a controlled, repeatable, and auditable way."

Transition:

"That is exactly the gap this framework is trying to close."

---

## Slide 3: What The Framework Gives Us

Title:
- `What the Framework Delivers`

Speaker script:

"At a high level, this framework gives us a manifest-driven way to run resilience tests.

Instead of building one script per scenario, we define the desired actions in a YAML manifest.

From there, the framework handles target resolution, validation, planning, execution, observability, artifact generation, and optional database persistence.

There are five main benefits I want to highlight.

One, the test definition becomes declarative and reviewable.

Two, pre-execution validation reduces avoidable mistakes.

Three, the dry-run mode gives approvers a clear view of what will happen before any live action is executed.

Four, the framework supports multiple execution models, so we are not limited to one AWS service.

And five, every run produces artifacts that can be reviewed later, including reports and optional Postgres records.

So in short, the framework is designed to turn resilience testing into an operational workflow rather than an ad hoc engineering task."

Transition:

"To understand that a bit better, let me show the execution models it supports."

---

## Slide 4: Execution Models

Title:
- `Execution Models: FIS, ARC, and Custom`

Speaker script:

"The framework supports three execution models.

The first is AWS Fault Injection Service, or FIS.

Where AWS already provides a native fault action, we prefer to use it. That gives us standard AWS behavior and integrates well with AWS service roles and controls.

The second is AWS ARC Region switch.

We use that for Aurora Global Database actions where ARC is the right regional control plane.

The third is custom execution.

This is for cases where AWS does not provide a native FIS or ARC action, or where we want a supported fallback path, such as `use_fis: false`.

This means the framework is not tied to a single AWS capability.

It can use the best control plane for the action in question while still giving the operator a consistent manifest format and execution experience."

Transition:

"Now I will show the high-level lifecycle of a single test run."

---

## Slide 5: High-Level Flow

Title:
- `How a Test Run Works`

Speaker script:

"Every run follows the same broad lifecycle.

Step one, the framework loads the manifest.

Step two, it resolves the action engine family. That means deciding whether the manifest is a FIS run, an ARC run, or a custom run.

Step three, it resolves the targeted resources using the selectors in the manifest, such as tags, identifiers, region, or zone.

Step four, it runs pre-execution validations unless the operator explicitly bypasses them with `--skip-validation`.

Step five, it generates the execution plan. For FIS, that means an experiment template payload. For ARC and custom actions, that means the corresponding execution plan structure.

Step six, if the operator is in dry-run mode, the framework stops there and shows the approval summary.

Step seven, if it is a real run, the framework executes the action and starts observability collectors around the run window.

Step eight, it writes artifacts such as impacted resources, execution plan, result summary, and HTML report.

Step nine, if configured, it also persists the run metadata and artifacts into PostgreSQL.

That end-to-end lifecycle is what gives us consistency."

Transition:

"But consistency alone is not enough. The cloud team will rightly ask: what are the safety controls?"

---

## Slide 6: Safety and Governance

Title:
- `Safety and Governance Controls`

Speaker script:

"This is probably the most important slide from an operational perspective.

The first control is validation. For supported actions, the framework verifies the target state before the action is executed.

The second control is dry-run. Dry-run does not just generate JSON. It now produces an approver-friendly table that shows the planned actions, engine type, dependencies, and impacted resources.

The third control is explicit target resolution. We do not rely on implicit guesses; selectors such as tags and identifiers are resolved up front.

The fourth control is engine-family separation. One manifest cannot mix FIS, ARC, and custom engines. That keeps the run model simpler and more predictable.

The fifth control is IAM separation. The execution path can use the appropriate role for FIS, ARC, and the runner host.

And the sixth control is evidence. Each run produces machine-readable artifacts, HTML output, and now optional database persistence.

So the framework is intentionally opinionated. It is designed to make the safe path the default path."

Transition:

"Now let me show what the manifest looks like in practice."

---

## Slide 7: Manifest Example

Title:
- `Example Manifest`

Suggested example:
- a simple EC2 stop or reboot manifest

Speaker script:

"This is the kind of manifest an operator would use.

The key point is that it is readable and reviewable.

You can see the service, the action, the target selector, and the relevant parameters in one place.

For example, an EC2 stop action might define the region, the tags that identify the instances, and optionally the duration.

That becomes the contract for the run.

This is much easier to review in a change or approval context than trying to reason about a long shell command or a custom script.

And because the manifest is the input contract, we can build validation and dry-run behavior around it in a standard way."

Transition:

"This leads directly to one of the most useful operator features, which is the dry-run approval view."

---

## Slide 8: Dry-Run Approval View

Title:
- `What the Approver Sees`

Speaker script:

"When we run with `--dry-run`, the framework now prints an approval summary table in the terminal and also writes the same summary to a text file.

This is intentionally designed for approvers.

It shows the action number, the action name, the engine, the execution type, region, zone, dependencies, impacted resource count, example resolved resources, and key parameters.

So before anyone performs the actual test, they can review what will happen in a structured way.

This is one of the strongest operational improvements in the framework, because it turns approval from a leap of faith into a concrete review step.

Instead of asking approvers to read raw JSON or infer impact from code, we are giving them a direct impact summary."

Transition:

"Let me move into the demo and show how that looks end to end."

---

## Slide 9: Demo Setup

Title:
- `Demo Scenario`

Recommended demo:
- main live demo: simple FIS-backed EC2 scenario
- optional dry-run-only second showcase: global DB or S3 MRAP

Speaker script:

"For the live demo, I will use a simple scenario so the flow is easy to follow.

I will first show a manifest.

Then I will run a dry-run so you can see the approval table.

Then I will run the actual test.

Then I will show the generated artifacts and report.

If time permits, I will also show a second dry-run-only example for a more advanced scenario like Aurora Global Database or S3 MRAP failover.

The reason I prefer this format is that it demonstrates both the operational safety path and the actual execution path."

Transition:

"I’ll switch to the terminal now."

---

## Live Demo Script

### Demo Step 1: Show the manifest

Speaker script:

"First, I will show the manifest that defines the test."

Command example:

```powershell
Get-Content ..\manifests\component-ec2.yml
```

Narration:

"Here we can see the service block, the target selector, and the action parameters. This is the full contract for the run."

### Demo Step 2: Run dry-run

Speaker script:

"Now I will run it in dry-run mode first. This will resolve the targets and show the approval summary without executing the fault."

Command example:

```powershell
python main.py --manifest ..\manifests\component-ec2.yml --fis-role-arn <fis-role-arn> --dry-run
```

Narration:

"This is the dry-run approval table.

For each planned action, we can see the engine, the exact execution type, the region, any dependency information, and the impacted resources.

This is the view I would want an approver to see before authorizing the live test."

### Demo Step 3: Call out key approval fields

Speaker script:

"The fields I want to highlight are:

- the action itself
- the engine that will execute it
- the impacted resource count
- the resolved resource examples
- and the key parameters

This gives enough information to understand the operational blast radius before the run."

### Demo Step 4: Run the actual test

Speaker script:

"Once the dry-run looks correct, I can run the real test."

Command example:

```powershell
python main.py --manifest ..\manifests\component-ec2.yml --fis-role-arn <fis-role-arn>
```

Narration:

"Now the framework is doing the real run.

You can see validation logs, action progress logs, and then the result summary."

### Demo Step 5: Show output artifacts

Speaker script:

"After the run, the framework keeps the artifacts in the output directory."

Command example:

```powershell
Get-ChildItem .\fis_out
```

Narration:

"The key outputs are the impacted resources file, the execution plan or FIS payload, the result summary, the approval summary from dry-run if we used it, and the HTML report."

### Demo Step 6: Mention HTML report

Speaker script:

"The HTML report is helpful because it gives a readable post-run summary that can be shared after the exercise."

Optional command:

```powershell
Get-Content .\fis_out\result_*.json
```

### Demo Step 7: Mention optional DB persistence

Speaker script:

"If database persistence is configured, the same run metadata is also stored in PostgreSQL for audit and later analysis."

Optional command if you want to mention it only verbally:

"I will not go deep into SQL in this demo, but the run, actions, impacted resources, artifacts, validations, and metrics can all be persisted into the Postgres schema."

---

## Slide 10: Outputs and Auditability

Title:
- `What We Keep After Each Run`

Speaker script:

"Let me summarize the evidence model.

After a run, the framework can keep:

- the manifest
- the impacted resources
- the FIS template or execution plan
- the result summary
- the HTML report
- and optionally the Postgres records

This matters because resilience testing should leave an audit trail.

If someone asks later what was tested, when it ran, what it touched, and what happened, we should be able to answer from artifacts rather than memory."

Transition:

"That brings me to the design choices behind the framework."

---

## Slide 11: Key Design Decisions

Title:
- `Why It Was Designed This Way`

Speaker script:

"There are a few deliberate design decisions behind the framework.

First, manifest-driven input. That makes tests reviewable and reusable.

Second, support for multiple control planes. We use FIS where AWS provides native actions, ARC where regional control is appropriate, and custom logic where AWS has gaps.

Third, dry-run before execution. This is a key operational safety feature.

Fourth, validation before execution. This helps catch bad selectors or invalid action state early.

Fifth, standardized outputs and optional persistence. This makes the framework more than just an executor. It becomes a source of evidence and history.

The overall idea is to make resilience testing something teams can operationalize, not just experiment with."

Transition:

"Finally, let me close with where this can go next."

---

## Slide 12: Roadmap and Ask

Title:
- `Next Steps`

Speaker script:

"There are several natural next steps from here.

One is broader action coverage across more AWS services and application patterns.

Another is tighter approval workflow integration, for example integrating dry-run output with a change or approval process.

Another is building dashboards on top of the Postgres persistence layer.

And another is onboarding more teams through reusable manifest templates and standard IAM patterns.

My main ask from the cloud team is feedback in three areas.

First, which resilience scenarios are most valuable to automate next.

Second, what additional governance controls you would want before broader adoption.

And third, where this would fit best operationally, whether as a team-owned tool, a platform capability, or part of a broader reliability workflow."

---

## Closing Script

"So to summarize, the framework gives us a safer and more repeatable way to run resilience tests across AWS workloads.

It standardizes how tests are defined, reviewed, executed, and recorded.

It supports multiple AWS execution models rather than forcing everything into one tool.

And it improves governance through validation, dry-run approval, and persistent artifacts.

That is the end of the walkthrough. I’m happy to take questions, and I’d also like feedback on which services or test scenarios the cloud team would want to prioritize next."

---

## Demo Backup Plan

Use this if the live run becomes risky or unstable.

### Backup option 1: Dry-run only

Say:

"To keep the session safe and predictable, I’ll stay in dry-run mode. The dry-run view still demonstrates the most important operational value, which is impact visibility and approval readiness."

### Backup option 2: Secondary showcase

If the main EC2 example is too simple, show a dry-run for a more advanced manifest:

```powershell
python main.py --manifest ..\manifests\geo-rds-failover-arc.yml --arc-role-arn <arc-role-arn> --dry-run
```

or

```powershell
python main.py --manifest ..\manifests\geo-s3-failover-custom.yml --dry-run
```

Narration:

"Even when we do not execute the live action, the dry-run still shows the same planning, target resolution, and approval model."

---

## Likely Questions and Suggested Answers

### Q: Can this be trusted in production-like environments?

Suggested answer:

"The framework is built with validation, dry-run approval, explicit selectors, and artifact generation specifically to make it safer to use in controlled environments. The goal is not unbounded fault injection. The goal is controlled resilience testing."

### Q: What happens if AWS does not support a native action?

Suggested answer:

"That is where the custom execution model comes in. The framework can still give the operator a consistent manifest and reporting experience, even when the underlying execution is custom."

### Q: Can we audit what happened later?

Suggested answer:

"Yes. The framework writes structured artifacts for every run, and it now also supports optional Postgres persistence for run metadata, actions, resources, artifacts, validations, and metrics."

### Q: Do we have to use the database?

Suggested answer:

"No. The file-based artifact flow works on its own. The database is an optional persistence layer for run history and reporting."

### Q: Can teams bypass validation?

Suggested answer:

"Yes, there is a `--skip-validation` flag, but the normal path is to keep validation enabled. The framework is designed so the safe path is the default path."

---

## Presenter Notes

- Keep the tone practical and operational.
- Avoid going too deep into module-level code structure unless asked.
- Emphasize:
  - repeatability
  - dry-run approval
  - validation
  - auditability
  - support for FIS, ARC, and custom actions
- If time is tight, shorten the architecture sections and spend more time on the dry-run and demo.
