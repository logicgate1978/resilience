import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import botocore

from utility import (
    apply_site_scope_to_target,
    normalize_service_name,
    parse_tags,
    resolve_iam_role_arns_from_names,
    utc_ts,
)


# -----------------------------
# Mapping: name:action -> FIS actionId
# -----------------------------
ACTION_ID_MAP: Dict[Tuple[str, str], str] = {
    ("rds", "reboot"): "aws:rds:reboot-db-instances",
    ("rds", "failover"): "aws:rds:failover-db-cluster",
    ("asg", "pause-launch"): "aws:ec2:asg-insufficient-instance-capacity-error",
    ("ec2", "pause-launch"): "aws:ec2:api-insufficient-instance-capacity-error",
    ("ec2", "stop"): "aws:ec2:stop-instances",
    ("ec2", "reboot"): "aws:ec2:reboot-instances",
    ("ec2", "terminate"): "aws:ec2:terminate-instances",
    ("network", "disrupt-connectivity"): "aws:network:disrupt-connectivity",
}

# For each (name, action), define FIS target resourceType and action target key
TARGET_SPEC_MAP: Dict[Tuple[str, str], Dict[str, str]] = {
    ("rds", "reboot"): {"resourceType": "aws:rds:db", "target_key": "DBInstances"},
    ("rds", "failover"): {"resourceType": "aws:rds:cluster", "target_key": "Clusters"},
    ("asg", "pause-launch"): {"resourceType": "aws:ec2:autoscaling-group", "target_key": "AutoScalingGroups"},
    ("ec2", "stop"): {"resourceType": "aws:ec2:instance", "target_key": "Instances"},
    ("ec2", "reboot"): {"resourceType": "aws:ec2:instance", "target_key": "Instances"},
    ("ec2", "terminate"): {"resourceType": "aws:ec2:instance", "target_key": "Instances"},
    ("network", "disrupt-connectivity"): {"resourceType": "aws:ec2:subnet", "target_key": "Subnets"},
    ("ec2", "pause-launch"): {"resourceType": "aws:iam:role", "target_key": "Roles"},
}


@dataclass
class ManifestService:
    name: str
    action: str
    duration: Optional[str] = None
    tags: Optional[str] = None
    iam_role_arns: Optional[List[str]] = None
    iam_roles: Optional[str] = None
    instance_count: Optional[int] = None


# -----------------------------
# Build FIS targets/actions
# -----------------------------
def build_target(
    *,
    name: str,
    resource_type: str,
    selection_mode: str,
    resource_tags: Optional[Dict[str, str]] = None,
    resource_arns: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """
    FIS target: either resourceTags or explicit resourceArns. Refuse unconstrained target.
    """
    t: Dict[str, Any] = {"resourceType": resource_type, "selectionMode": selection_mode}

    if resource_arns is not None:
        t["resourceArns"] = resource_arns
        return t

    if resource_tags:
        t["resourceTags"] = resource_tags
        return t

    raise ValueError(f"Target '{name}' is unconstrained (no tags and no resourceArns).")


def build_action(
    *,
    manifest: Dict[str, Any],
    svc: ManifestService,
    action_id: str,
    target_key: str,
    target_ref_name: str,
    start_after: Optional[str],
) -> Dict[str, Any]:
    """
    Build FIS action object, including required parameters depending on actionId.

    NOTE: Do NOT set "startAfter" unless user explicitly requests sequencing.
    """
    action_obj: Dict[str, Any] = {
        "actionId": action_id,
        "description": f"{svc.name}:{svc.action}",
        "targets": {target_key: target_ref_name},
    }

    params: Dict[str, str] = {}

    # ec2:stop -> startInstancesAfterDuration
    if action_id == "aws:ec2:stop-instances":
        if not svc.duration:
            raise ValueError("ec2:stop requires services[].duration (used as startInstancesAfterDuration, e.g. PT30M).")
        params["startInstancesAfterDuration"] = svc.duration
        params["completeIfInstancesTerminated"] = "true"

    # network disrupt -> needs duration + scope
    if action_id == "aws:network:disrupt-connectivity":
        if not svc.duration:
            raise ValueError("network:disrupt-connectivity requires services[].duration (e.g. PT30M).")
        rtype = (manifest.get("resilience_test_type") or "").strip().lower()
        params["duration"] = svc.duration
        params["scope"] = "availability-zone" if rtype == "site" else "all"

    # pause-launch -> needs duration + AZ identifiers (+ percentage)
    if action_id in ("aws:ec2:api-insufficient-instance-capacity-error", "aws:ec2:asg-insufficient-instance-capacity-error"):
        if not svc.duration:
            raise ValueError(f"{svc.name}:{svc.action} requires services[].duration (e.g. PT30M).")
        zone = manifest.get("zone")
        if not zone or not isinstance(zone, str):
            raise ValueError(f"{svc.name}:{svc.action} requires top-level 'zone' (e.g. eu-west-1a).")
        params["duration"] = svc.duration
        params["availabilityZoneIdentifiers"] = zone
        params["percentage"] = "100"

    if params:
        action_obj["parameters"] = params

    return action_obj


def generate_template_payload(
    manifest: Dict[str, Any],
    fis_role_arn: str,
    selection_mode: str = "ALL",
) -> Dict[str, Any]:
    region = manifest.get("region")
    if not region or not isinstance(region, str):
        raise ValueError("Top-level 'region' is required (e.g. eu-west-1).")

    rtype = (manifest.get("resilience_test_type") or "").strip().lower()
    if rtype not in ("component", "site"):
        raise ValueError("Top-level 'resilience_test_type' must be 'component' or 'site'.")

    zone = manifest.get("zone")
    if rtype == "site":
        if not zone or not isinstance(zone, str):
            raise ValueError("For resilience_test_type: site, top-level 'zone' is required (e.g. eu-west-1a).")

    services_raw = manifest.get("services")
    if not isinstance(services_raw, list) or not services_raw:
        raise ValueError("Top-level 'services' must be a non-empty list.")

    # Parse services
    services: List[ManifestService] = []
    for i, s in enumerate(services_raw):
        if not isinstance(s, dict):
            raise ValueError(f"services[{i}] must be an object.")
        name = normalize_service_name(s.get("name"))
        action = (s.get("action") or "").strip().lower()
        duration = s.get("duration")
        tags = s.get("tags")
        iam_role_arns = s.get("iam_role_arns")
        iam_roles = s.get("iam_roles")
        instance_count = s.get("instance_count")

        if not name or not action:
            raise ValueError(f"services[{i}] must include 'name' and 'action'.")

        if (name, action) not in ACTION_ID_MAP:
            raise ValueError(f"Unsupported service action: {name}:{action}")

        if iam_role_arns is not None and not isinstance(iam_role_arns, list):
            raise ValueError(f"services[{i}].iam_role_arns must be a list if provided.")

        if iam_roles is not None and not isinstance(iam_roles, str):
            raise ValueError(f"services[{i}].iam_roles must be a comma-separated string if provided.")

        if instance_count is not None:
            try:
                instance_count = int(instance_count)
            except Exception:
                raise ValueError(f"services[{i}].instance_count must be an integer if provided.")
            if instance_count <= 0:
                raise ValueError(f"services[{i}].instance_count must be > 0.")

        services.append(
            ManifestService(
                name=name,
                action=action,
                duration=duration,
                tags=tags,
                iam_role_arns=iam_role_arns,
                iam_roles=iam_roles,
                instance_count=instance_count,
            )
        )

    targets: Dict[str, Any] = {}
    actions: Dict[str, Any] = {}

    prev_action_name: Optional[str] = None  # kept for minimal edits; not used for startAfter now

    for idx, svc in enumerate(services, start=1):
        action_id = ACTION_ID_MAP[(svc.name, svc.action)]
        spec = TARGET_SPEC_MAP[(svc.name, svc.action)]
        resource_type = spec["resourceType"]
        target_key = spec["target_key"]

        tag_dict = parse_tags(svc.tags)

        resource_arns: Optional[List[str]] = None

        if action_id == "aws:ec2:api-insufficient-instance-capacity-error":
            if not svc.iam_role_arns:
                role_names = svc.iam_roles or "BAU,Admin,scb-user-instance-role"
                resolved = resolve_iam_role_arns_from_names(role_names)
                if not resolved:
                    raise ValueError(
                        "ec2:pause-launch requires IAM role ARNs. Provide services[].iam_roles (role names) "
                        "or services[].iam_role_arns (list of role ARNs)."
                    )
                resource_arns = resolved
            else:
                resource_arns = svc.iam_role_arns

        # ec2 instance_count selectionMode behavior
        this_selection_mode = selection_mode
        if svc.name == "ec2" and svc.instance_count is not None:
            this_selection_mode = f"COUNT({svc.instance_count})"
        elif svc.name == "ec2" and svc.instance_count is None:
            this_selection_mode = "ALL"

        target_name = f"t_{svc.name}_{svc.action}_{idx}"
        targets[target_name] = build_target(
            name=target_name,
            resource_type=resource_type,
            selection_mode=this_selection_mode,
            resource_tags=tag_dict if (resource_arns is None) else None,
            resource_arns=resource_arns,
        )

        if rtype == "site" and resource_arns is None and isinstance(zone, str):
            apply_site_scope_to_target(targets[target_name], resource_type, zone)

        action_name = f"a_{svc.name}_{svc.action}_{idx}"
        actions[action_name] = build_action(
            manifest=manifest,
            svc=svc,
            action_id=action_id,
            target_key=target_key,
            target_ref_name=target_name,
            start_after=prev_action_name,
        )
        prev_action_name = action_name

    template_name = f"resilience-{rtype}-{utc_ts()}"

    payload = {
        "clientToken": f"{template_name}-{int(time.time())}",
        "description": template_name,
        "roleArn": fis_role_arn,
        "stopConditions": [{"source": "none"}],
        "targets": targets,
        "actions": actions,
        "experimentOptions": {"emptyTargetResolutionMode": "skip"},
        "tags": {"managed-by": "fis.py"},
    }
    return payload


def create_template(fis_client, payload: Dict[str, Any]) -> str:
    resp = fis_client.create_experiment_template(**payload)
    return resp["experimentTemplate"]["id"]