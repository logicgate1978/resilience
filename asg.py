#!/usr/bin/env python3
import argparse
import base64
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import boto3
import botocore.exceptions


ACTIVE_LIFECYCLE_STATES = {
    "Pending",
    "Pending:Wait",
    "Pending:Proceed",
    "InService",
    "Standby",
    "Terminating",
    "Terminating:Wait",
    "Terminating:Proceed",
    "Warmed:Pending",
    "Warmed:Running",
}


def log(message: str) -> None:
    print(message, flush=True)


def fail(message: str) -> None:
    raise RuntimeError(message)


def asg_name_from_identifier(identifier: str) -> str:
    marker = "autoScalingGroupName/"
    if marker in identifier:
        return identifier.split(marker, 1)[1]
    return identifier


def read_user_data(path: Path) -> str:
    if not path.exists():
        fail(f"User data file does not exist: {path}")
    if not path.is_file():
        fail(f"User data path is not a file: {path}")
    return base64.b64encode(path.read_bytes()).decode("ascii")


def get_asg(autoscaling, asg_identifier: str) -> Dict[str, Any]:
    asg_name = asg_name_from_identifier(asg_identifier)
    response = autoscaling.describe_auto_scaling_groups(AutoScalingGroupNames=[asg_name])
    groups = response.get("AutoScalingGroups") or []
    if not groups:
        fail(f"Auto Scaling Group was not found: {asg_identifier}")
    return groups[0]


def get_launch_template_spec(asg: Dict[str, Any]) -> Tuple[Dict[str, str], bool]:
    launch_template = asg.get("LaunchTemplate")
    if launch_template:
        return launch_template, False

    mixed_policy = asg.get("MixedInstancesPolicy") or {}
    mixed_launch_template = (mixed_policy.get("LaunchTemplate") or {}).get("LaunchTemplateSpecification")
    if mixed_launch_template:
        return mixed_launch_template, True

    fail(
        "The ASG does not use a Launch Template. "
        "This script does not support Launch Configurations."
    )


def resolve_source_version(ec2, launch_template_spec: Dict[str, str]) -> str:
    version = str(launch_template_spec.get("Version") or "$Default")
    if version not in {"$Latest", "$Default"}:
        return version

    kwargs = launch_template_lookup_kwargs(launch_template_spec)
    response = ec2.describe_launch_templates(**kwargs)
    templates = response.get("LaunchTemplates") or []
    if not templates:
        fail("Launch Template was not found.")

    template = templates[0]
    if version == "$Latest":
        return str(template["LatestVersionNumber"])
    return str(template["DefaultVersionNumber"])


def launch_template_lookup_kwargs(launch_template_spec: Dict[str, str]) -> Dict[str, Any]:
    if launch_template_spec.get("LaunchTemplateId"):
        return {"LaunchTemplateIds": [launch_template_spec["LaunchTemplateId"]]}
    if launch_template_spec.get("LaunchTemplateName"):
        return {"LaunchTemplateNames": [launch_template_spec["LaunchTemplateName"]]}
    fail("Launch Template specification has neither LaunchTemplateId nor LaunchTemplateName.")


def launch_template_update_spec(launch_template_spec: Dict[str, str], version: str) -> Dict[str, str]:
    if launch_template_spec.get("LaunchTemplateId"):
        return {
            "LaunchTemplateId": str(launch_template_spec["LaunchTemplateId"]),
            "Version": str(version),
        }
    if launch_template_spec.get("LaunchTemplateName"):
        return {
            "LaunchTemplateName": str(launch_template_spec["LaunchTemplateName"]),
            "Version": str(version),
        }
    fail("Launch Template specification has neither LaunchTemplateId nor LaunchTemplateName.")


def create_launch_template_version(
    ec2,
    launch_template_spec: Dict[str, str],
    source_version: str,
    encoded_user_data: str,
) -> str:
    kwargs = launch_template_lookup_kwargs(launch_template_spec)
    create_kwargs: Dict[str, Any] = {
        "SourceVersion": source_version,
        "LaunchTemplateData": {
            "UserData": encoded_user_data,
        },
    }

    if "LaunchTemplateIds" in kwargs:
        create_kwargs["LaunchTemplateId"] = kwargs["LaunchTemplateIds"][0]
    else:
        create_kwargs["LaunchTemplateName"] = kwargs["LaunchTemplateNames"][0]

    response = ec2.create_launch_template_version(**create_kwargs)
    version_number = response["LaunchTemplateVersion"]["VersionNumber"]
    return str(version_number)


def update_asg_launch_template(
    autoscaling,
    asg_name: str,
    launch_template_spec: Dict[str, str],
    new_version: str,
    uses_mixed_instances_policy: bool,
) -> None:
    updated_spec = launch_template_update_spec(launch_template_spec, new_version)
    log(f"[INFO] Updating ASG Launch Template reference: {updated_spec}")

    if not uses_mixed_instances_policy:
        autoscaling.update_auto_scaling_group(
            AutoScalingGroupName=asg_name,
            LaunchTemplate=updated_spec,
        )
        return

    asg = get_asg(autoscaling, asg_name)
    mixed_policy = asg.get("MixedInstancesPolicy") or {}
    launch_template = dict(mixed_policy.get("LaunchTemplate") or {})
    launch_template["LaunchTemplateSpecification"] = updated_spec

    update_payload = {
        "LaunchTemplate": launch_template,
    }
    if mixed_policy.get("InstancesDistribution"):
        update_payload["InstancesDistribution"] = mixed_policy["InstancesDistribution"]

    autoscaling.update_auto_scaling_group(
        AutoScalingGroupName=asg_name,
        MixedInstancesPolicy=update_payload,
    )


def count_active_instances(asg: Dict[str, Any]) -> int:
    return sum(
        1
        for instance in asg.get("Instances") or []
        if str(instance.get("LifecycleState") or "") in ACTIVE_LIFECYCLE_STATES
    )


def count_in_service_instances(asg: Dict[str, Any]) -> int:
    return sum(
        1
        for instance in asg.get("Instances") or []
        if str(instance.get("LifecycleState") or "") == "InService"
    )


def wait_for_capacity(
    autoscaling,
    asg_name: str,
    *,
    active_count: Optional[int],
    in_service_count: Optional[int],
    poll_seconds: int,
    timeout_seconds: int,
    label: str,
) -> Dict[str, Any]:
    deadline = time.time() + timeout_seconds
    last_asg: Dict[str, Any] = {}

    while True:
        last_asg = get_asg(autoscaling, asg_name)
        active = count_active_instances(last_asg)
        in_service = count_in_service_instances(last_asg)

        active_ready = active_count is None or active == active_count
        in_service_ready = in_service_count is None or in_service == in_service_count
        if active_ready and in_service_ready:
            log(f"[OK] {label}: active={active}, in_service={in_service}")
            return last_asg

        if time.time() >= deadline:
            fail(
                f"Timed out waiting for {label}. "
                f"Last observed active={active}, in_service={in_service}."
            )

        log(f"[WAIT] {label}: active={active}, in_service={in_service}")
        time.sleep(poll_seconds)


def refresh_instances_by_scaling(
    autoscaling,
    asg_name: str,
    original_min: int,
    original_max: int,
    original_desired: int,
    poll_seconds: int,
    timeout_seconds: int,
) -> None:
    log("[INFO] Scaling ASG down to min=0, desired=0.")
    autoscaling.update_auto_scaling_group(
        AutoScalingGroupName=asg_name,
        MinSize=0,
        MaxSize=original_max,
        DesiredCapacity=0,
    )
    wait_for_capacity(
        autoscaling,
        asg_name,
        active_count=0,
        in_service_count=0,
        poll_seconds=poll_seconds,
        timeout_seconds=timeout_seconds,
        label="scale down",
    )

    log(
        "[INFO] Scaling ASG back to "
        f"min={original_min}, max={original_max}, desired={original_desired}."
    )
    autoscaling.update_auto_scaling_group(
        AutoScalingGroupName=asg_name,
        MinSize=original_min,
        MaxSize=original_max,
        DesiredCapacity=original_desired,
    )
    wait_for_capacity(
        autoscaling,
        asg_name,
        active_count=original_desired,
        in_service_count=original_desired,
        poll_seconds=poll_seconds,
        timeout_seconds=timeout_seconds,
        label="scale up",
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Update an ASG launch template with EC2 user data from a .sh file, "
            "then refresh instances by scaling the ASG down and back up."
        )
    )
    parser.add_argument("userdata_file", help="Path to the EC2 user data .sh file.")
    parser.add_argument("region", help="AWS region, for example ap-southeast-1.")
    parser.add_argument("asg_identifier", help="Auto Scaling Group name or ARN.")
    parser.add_argument(
        "--poll-seconds",
        type=int,
        default=15,
        help="Seconds between ASG readiness checks. Default: 15.",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=1800,
        help="Timeout for each scale operation. Default: 1800.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.poll_seconds < 1:
        fail("--poll-seconds must be at least 1.")
    if args.timeout_seconds < 1:
        fail("--timeout-seconds must be at least 1.")

    encoded_user_data = read_user_data(Path(args.userdata_file))
    session = boto3.Session(region_name=args.region)
    autoscaling = session.client("autoscaling")
    ec2 = session.client("ec2")

    asg = get_asg(autoscaling, args.asg_identifier)
    asg_name = asg["AutoScalingGroupName"]
    original_min = int(asg.get("MinSize") or 0)
    original_max = int(asg.get("MaxSize") or 0)
    original_desired = int(asg.get("DesiredCapacity") or 0)

    launch_template_spec, uses_mixed_instances_policy = get_launch_template_spec(asg)
    source_version = resolve_source_version(ec2, launch_template_spec)

    log(f"[INFO] ASG: {asg_name}")
    log(f"[INFO] Launch Template source version: {source_version}")

    new_version = create_launch_template_version(
        ec2,
        launch_template_spec,
        source_version,
        encoded_user_data,
    )
    log(f"[OK] Created Launch Template version: {new_version}")

    update_asg_launch_template(
        autoscaling,
        asg_name,
        launch_template_spec,
        new_version,
        uses_mixed_instances_policy,
    )
    log(f"[OK] Updated ASG to use Launch Template version: {new_version}")

    refresh_instances_by_scaling(
        autoscaling,
        asg_name,
        original_min,
        original_max,
        original_desired,
        args.poll_seconds,
        args.timeout_seconds,
    )

    log("[OK] ASG user data update and instance refresh completed.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except botocore.exceptions.ClientError as exc:
        print(f"[ERROR] AWS API error: {exc}", file=sys.stderr, flush=True)
        raise SystemExit(1)
    except Exception as exc:
        print(f"[ERROR] {exc}", file=sys.stderr, flush=True)
        raise SystemExit(1)
