import csv
import json
import os
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import boto3
import requests
import yaml


def utc_ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def log_message(level: str, message: str) -> None:
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
    print(f"[{timestamp}] [{str(level or '').strip().upper()}] {message}", flush=True)


def ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def pretty(obj: Any) -> str:
    return json.dumps(obj, indent=2, sort_keys=True, default=str)


def normalize_service_name(name: str) -> str:
    n = (name or "").strip().lower()
    # allow "network (vpc)" style
    if "network" in n or "vpc" in n:
        return "network"
    return n


def parse_tags(tags_str: Optional[str]) -> Dict[str, str]:
    """
    Convert "k1=v1,k2=v2" into {"k1":"v1","k2":"v2"} (AND semantics).
    """
    if not tags_str:
        return {}
    out: Dict[str, str] = {}
    parts = [p.strip() for p in tags_str.split(",") if p.strip()]
    for p in parts:
        if "=" not in p:
            raise ValueError(f"Invalid tag '{p}'. Expected format key=value.")
        k, v = p.split("=", 1)
        k, v = k.strip(), v.strip()
        if not k or not v:
            raise ValueError(f"Invalid tag '{p}'. Expected format key=value.")
        out[k] = v
    return out


def load_manifest(path: str) -> Dict[str, Any]:
    if not os.path.exists(path):
        raise FileNotFoundError(f"Manifest not found: {path}")
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        raise ValueError("manifest.yml must be a YAML mapping/object at top level.")
    return data


def load_env_file(path: str) -> Dict[str, str]:
    if not path or not os.path.exists(path):
        return {}

    out: Dict[str, str] = {}
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            raw = line.strip()
            if not raw or raw.startswith("#") or "=" not in raw:
                continue
            key, value = raw.split("=", 1)
            key = key.strip()
            value = value.strip()
            if not key:
                continue
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                value = value[1:-1]
            out[key] = value
    return out


def parse_bool(value: Optional[str], default: bool = False) -> bool:
    if value is None:
        return default
    v = str(value).strip().lower()
    if v in ("1", "true", "yes", "y", "on"):
        return True
    if v in ("0", "false", "no", "n", "off"):
        return False
    return default


def coerce_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return parse_bool(str(value), default=default)


def resolve_service_field(
    manifest: Dict[str, Any],
    svc: Optional[Dict[str, Any]],
    field_name: str,
    default: Optional[Any] = None,
) -> Optional[Any]:
    if isinstance(svc, dict):
        value = svc.get(field_name)
        if value is not None:
            if not isinstance(value, str) or value.strip() != "":
                return value

    value = manifest.get(field_name) if isinstance(manifest, dict) else None
    if value is not None:
        if not isinstance(value, str) or value.strip() != "":
            return value

    return default


def resolve_service_region(
    manifest: Dict[str, Any],
    svc: Optional[Dict[str, Any]],
    default: Optional[str] = None,
) -> Optional[str]:
    value = resolve_service_field(manifest, svc, "region", default=default)
    return str(value).strip() if isinstance(value, str) else value


def resolve_service_zone(
    manifest: Dict[str, Any],
    svc: Optional[Dict[str, Any]],
    default: Optional[str] = None,
) -> Optional[str]:
    value = resolve_service_field(manifest, svc, "zone", default=default)
    return str(value).strip() if isinstance(value, str) else value


def resolve_service_primary_region(
    manifest: Dict[str, Any],
    svc: Optional[Dict[str, Any]],
    default: Optional[str] = None,
) -> Optional[str]:
    value = resolve_service_field(manifest, svc, "primary_region", default=default)
    return str(value).strip() if isinstance(value, str) else value


def resolve_service_secondary_region(
    manifest: Dict[str, Any],
    svc: Optional[Dict[str, Any]],
    default: Optional[str] = None,
) -> Optional[str]:
    value = resolve_service_field(manifest, svc, "secondary_region", default=default)
    return str(value).strip() if isinstance(value, str) else value


def get_account_id(sts_client) -> str:
    return sts_client.get_caller_identity()["Account"]


def sanitize_filename(s: str, max_len: int = 120) -> str:
    s = (s or "").strip()
    if not s:
        return "unknown"
    s = re.sub(r"[^A-Za-z0-9._-]+", "_", s)
    return s[:max_len].strip("._-") or "unknown"


def append_csv_row(path: str, header: List[str], row: Dict[str, Any]) -> None:
    file_exists = os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=header)
        if not file_exists:
            w.writeheader()
        w.writerow(row)


def resolve_iam_role_arns_from_names(role_names_csv: str) -> List[str]:
    """
    Resolve IAM role names -> role ARNs via boto3 IAM get_role.
    role_names_csv: "BAU,Admin,scb-user-instance-role"
    """
    names = [x.strip() for x in (role_names_csv or "").split(",") if x.strip()]
    if not names:
        return []
    iam = boto3.client("iam")  # global
    arns: List[str] = []
    for n in names:
        resp = iam.get_role(RoleName=n)
        arns.append(resp["Role"]["Arn"])
    return arns


def apply_site_scope_to_target(target: Dict[str, Any], resource_type: str, zone: str) -> None:
    """
    Add filters or parameters to scope supported target resources to a specific AZ.
    """
    if resource_type == "aws:ec2:instance":
        target.setdefault("filters", [])
        target["filters"].append({"path": "Placement.AvailabilityZone", "values": [zone]})
    elif resource_type == "aws:ec2:subnet":
        target.setdefault("parameters", {})
        target["parameters"]["availabilityZoneIdentifier"] = zone
    elif resource_type == "aws:rds:cluster":
        target.setdefault("parameters", {})
        target["parameters"]["writerAvailabilityZoneIdentifiers"] = zone
    elif resource_type == "aws:rds:db":
        target.setdefault("parameters", {})
        target["parameters"]["availabilityZoneIdentifiers"] = zone
    elif resource_type == "aws:ec2:autoscaling-group":
        target.setdefault("filters", [])
        target["filters"].append({"path": "AvailabilityZones", "values": [zone]})


def get_ssm_parameter_str(region, name):
    ssm_client = boto3.client('ssm', region_name=region)
    response = ssm_client.get_parameters(Names=[name], WithDecryption=True)
    return response['Parameters'][0]['Value']


def upload_files_to_artifactory(filenames):
    # configure API credentials
    artifactory_api_key = get_ssm_parameter_str('eu-west-1', '/app/azure_cmdb/development/azure_cmdb/artifactory_api_key')
    artifactory_username, api_key = artifactory_api_key.split(':')

    # upload files to Artifactory
    print('===== uploading files to Artifactory', flush=True)
    auth = (artifactory_username, api_key)
    artifactory_url = 'https://artifactory.global.standardchartered.com/artifactory/generic-cloud/com/sc/cloud/deploy/release/inventory/monthly-summary-report'

    for filename in filenames:
        artifactory_filename = filename.replace('/tmp/', '')
        if artifactory_filename == filename:
            artifactory_filename = os.path.basename(filename)
        url = artifactory_url + '/' + artifactory_filename
        print(f'===== artifactory_filename: {artifactory_filename}', flush=True)
        print(f'===== url: {url}', flush=True)

        with open(filename, 'rb') as fobj:
            res = requests.put(url, auth=auth, data=fobj)
            print(f'===== res: {res}', flush=True)

            if res.ok:
                print(f'===== SUCCESS!! File has been uploaded successfully to Artifactory: {url}', flush=True)
            else:
                print(f'===== FAIL!! There was an error while uploading the file to Artifactory: {url}', flush=True)
