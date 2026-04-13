#!/usr/bin/env bash

set -euo pipefail

DEFAULT_PRIMARY_REGION="ap-southeast-1"
DEFAULT_SECONDARY_REGION="ap-southeast-2"
DEFAULT_NAME="resilience-s3-replication"
ENV_TAG_VALUE="development"
PROJECT_TAG_VALUE="clouddash"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE_DIR="${SCRIPT_DIR}/.state"
STATE_FILE="${STATE_DIR}/current_s3_replication_stack.txt"
SAMPLE_FILE="${STATE_DIR}/sample_pause_replication_object.txt"
SAMPLE_OBJECT_KEY="sample_pause_replication_object.txt"

PRIMARY_REGION="${DEFAULT_PRIMARY_REGION}"
SECONDARY_REGION="${DEFAULT_SECONDARY_REGION}"
NAME="${DEFAULT_NAME}"

ACCOUNT_ID=""
PRIMARY_BUCKET=""
SECONDARY_BUCKET=""
REPLICATION_ROLE_NAME=""
REPLICATION_POLICY_NAME=""

usage() {
  cat <<'EOF'
Usage:
  ./commands/s3/create_s3_replication_stack.sh [--name <base-name>] [--primary-region <aws-region>] [--secondary-region <aws-region>]

Defaults:
  name: resilience-s3-replication
  primary-region: ap-southeast-1
  secondary-region: ap-southeast-2

Notes:
  - Creates one versioned source bucket in ap-southeast-1 by default.
  - Creates one versioned destination bucket in ap-southeast-2 by default.
  - Configures one-way replication from primary to secondary.
  - Uploads one sample text object to the primary bucket after replication is configured.
  - Writes local state to commands/s3/.state/current_s3_replication_stack.txt.
EOF
}

require_command() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "ERROR: Required command not found: $1" >&2
    exit 1
  fi
}

normalize_name() {
  local raw="$1"
  local normalized
  normalized="$(echo "${raw}" | tr '[:upper:]' '[:lower:]' | tr -cs 'a-z0-9-' '-')"
  normalized="${normalized#-}"
  normalized="${normalized%-}"
  if [[ -z "${normalized}" ]]; then
    normalized="s3-replication"
  fi
  echo "${normalized}"
}

build_bucket_name() {
  local base="$1"
  local account_id="$2"
  local region="$3"
  local suffix budget
  suffix="-${account_id}-${region}"
  budget=$((63 - ${#suffix}))
  if (( budget < 3 )); then
    echo "ERROR: Invalid bucket name budget." >&2
    exit 1
  fi
  base="${base:0:${budget}}"
  base="${base%-}"
  echo "${base}${suffix}"
}

build_name() {
  local base="$1"
  local suffix="$2"
  local max_len="$3"
  local base_max_len
  base_max_len=$((max_len - ${#suffix}))
  if (( base_max_len < 1 )); then
    echo "ERROR: Invalid name budget for suffix '${suffix}'." >&2
    exit 1
  fi
  base="${base:0:${base_max_len}}"
  base="${base%-}"
  echo "${base}${suffix}"
}

write_state() {
  mkdir -p "${STATE_DIR}"
  cat > "${STATE_FILE}" <<EOF
ACCOUNT_ID=${ACCOUNT_ID}
PRIMARY_REGION=${PRIMARY_REGION}
SECONDARY_REGION=${SECONDARY_REGION}
NAME=${NAME}
PRIMARY_BUCKET=${PRIMARY_BUCKET}
SECONDARY_BUCKET=${SECONDARY_BUCKET}
REPLICATION_ROLE_NAME=${REPLICATION_ROLE_NAME}
REPLICATION_POLICY_NAME=${REPLICATION_POLICY_NAME}
SAMPLE_FILE=${SAMPLE_FILE}
SAMPLE_OBJECT_KEY=${SAMPLE_OBJECT_KEY}
EOF
}

create_sample_file() {
  mkdir -p "${STATE_DIR}"
  cat > "${SAMPLE_FILE}" <<EOF
This is a sample replication object for the resilience S3 pause-replication test stack.
Primary region: ${PRIMARY_REGION}
Secondary region: ${SECONDARY_REGION}
Created at: $(date -u +"%Y-%m-%dT%H:%M:%SZ")
EOF
}

upload_sample_file() {
  echo "Uploading sample object '${SAMPLE_OBJECT_KEY}' to primary bucket '${PRIMARY_BUCKET}'..."
  aws s3 cp "${SAMPLE_FILE}" "s3://${PRIMARY_BUCKET}/${SAMPLE_OBJECT_KEY}" \
    --region "${PRIMARY_REGION}" \
    >/dev/null
}

bucket_exists() {
  local bucket="$1"
  local region="$2"
  aws s3api head-bucket --bucket "${bucket}" --region "${region}" >/dev/null 2>&1
}

ensure_bucket() {
  local bucket="$1"
  local region="$2"

  if ! bucket_exists "${bucket}" "${region}"; then
    echo "Creating bucket '${bucket}' in ${region}..."
    aws s3api create-bucket \
      --bucket "${bucket}" \
      --region "${region}" \
      --create-bucket-configuration "LocationConstraint=${region}" \
      >/dev/null
  else
    echo "Bucket already exists: ${bucket} (${region})"
  fi

  aws s3api put-bucket-versioning \
    --bucket "${bucket}" \
    --region "${region}" \
    --versioning-configuration Status=Enabled \
    >/dev/null

  aws s3api put-bucket-tagging \
    --bucket "${bucket}" \
    --region "${region}" \
    --tagging "TagSet=[{Key=Name,Value=${bucket}},{Key=environment,Value=${ENV_TAG_VALUE}},{Key=project,Value=${PROJECT_TAG_VALUE}}]" \
    >/dev/null
}

ensure_replication_role() {
  local trust_file policy_file role_arn

  trust_file="$(mktemp)"
  policy_file="$(mktemp)"

  cat > "${trust_file}" <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": {
        "Service": "s3.amazonaws.com"
      },
      "Action": "sts:AssumeRole"
    }
  ]
}
EOF

  cat > "${policy_file}" <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "SourceBucketRead",
      "Effect": "Allow",
      "Action": [
        "s3:GetReplicationConfiguration",
        "s3:ListBucket"
      ],
      "Resource": "arn:aws:s3:::${PRIMARY_BUCKET}"
    },
    {
      "Sid": "SourceObjectRead",
      "Effect": "Allow",
      "Action": [
        "s3:GetObjectVersionForReplication",
        "s3:GetObjectVersionAcl",
        "s3:GetObjectVersionTagging"
      ],
      "Resource": "arn:aws:s3:::${PRIMARY_BUCKET}/*"
    },
    {
      "Sid": "DestinationWrite",
      "Effect": "Allow",
      "Action": [
        "s3:ReplicateObject",
        "s3:ReplicateDelete",
        "s3:ReplicateTags"
      ],
      "Resource": "arn:aws:s3:::${SECONDARY_BUCKET}/*"
    }
  ]
}
EOF

  if aws iam get-role --role-name "${REPLICATION_ROLE_NAME}" >/dev/null 2>&1; then
    echo "Replication role already exists: ${REPLICATION_ROLE_NAME}" >&2
  else
    echo "Creating replication IAM role '${REPLICATION_ROLE_NAME}'..." >&2
    aws iam create-role \
      --role-name "${REPLICATION_ROLE_NAME}" \
      --assume-role-policy-document "file://${trust_file}" \
      --tags "Key=environment,Value=${ENV_TAG_VALUE}" "Key=project,Value=${PROJECT_TAG_VALUE}" \
      >/dev/null
  fi

  aws iam put-role-policy \
    --role-name "${REPLICATION_ROLE_NAME}" \
    --policy-name "${REPLICATION_POLICY_NAME}" \
    --policy-document "file://${policy_file}" \
    >/dev/null

  rm -f "${trust_file}" "${policy_file}"

  role_arn="$(aws iam get-role --role-name "${REPLICATION_ROLE_NAME}" --query 'Role.Arn' --output text)"
  echo "${role_arn}"
}

put_replication_config() {
  local role_arn="$1"
  local cfg_file

  cfg_file="$(mktemp)"
  cat > "${cfg_file}" <<EOF
{
  "Role": "${role_arn}",
  "Rules": [
    {
      "ID": "replicate-all-to-${SECONDARY_REGION}",
      "Status": "Enabled",
      "Priority": 1,
      "DeleteMarkerReplication": {
        "Status": "Disabled"
      },
      "Filter": {},
      "Destination": {
        "Bucket": "arn:aws:s3:::${SECONDARY_BUCKET}"
      }
    }
  ]
}
EOF

  aws s3api put-bucket-replication \
    --bucket "${PRIMARY_BUCKET}" \
    --region "${PRIMARY_REGION}" \
    --replication-configuration "file://${cfg_file}" \
    >/dev/null

  rm -f "${cfg_file}"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --name)
      NAME="$(normalize_name "${2:-}")"
      shift 2
      ;;
    --primary-region)
      PRIMARY_REGION="${2:-}"
      shift 2
      ;;
    --secondary-region)
      SECONDARY_REGION="${2:-}"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "ERROR: Unknown argument: $1" >&2
      usage >&2
      exit 1
      ;;
  esac
done

require_command aws

if [[ "${PRIMARY_REGION}" == "${SECONDARY_REGION}" ]]; then
  echo "ERROR: primary and secondary regions must be different." >&2
  exit 1
fi

ACCOUNT_ID="$(aws sts get-caller-identity --query 'Account' --output text)"
PRIMARY_BUCKET="$(build_bucket_name "${NAME}" "${ACCOUNT_ID}" "${PRIMARY_REGION}")"
SECONDARY_BUCKET="$(build_bucket_name "${NAME}" "${ACCOUNT_ID}" "${SECONDARY_REGION}")"
REPLICATION_ROLE_NAME="$(build_name "${NAME}" "-replication-role" 64)"
REPLICATION_POLICY_NAME="$(build_name "${NAME}" "-replication-policy" 128)"

echo "Primary Region:         ${PRIMARY_REGION}"
echo "Secondary Region:       ${SECONDARY_REGION}"
echo "Base name:              ${NAME}"
echo "Primary bucket:         ${PRIMARY_BUCKET}"
echo "Secondary bucket:       ${SECONDARY_BUCKET}"
echo "Replication role name:  ${REPLICATION_ROLE_NAME}"
echo
echo "Creating or reusing S3 replication resources..."
echo

ensure_bucket "${PRIMARY_BUCKET}" "${PRIMARY_REGION}"
ensure_bucket "${SECONDARY_BUCKET}" "${SECONDARY_REGION}"

ROLE_ARN="$(ensure_replication_role)"

echo "Configuring one-way replication ${PRIMARY_REGION} -> ${SECONDARY_REGION}..."
put_replication_config "${ROLE_ARN}"

create_sample_file
upload_sample_file
write_state

echo
echo "S3 replication stack is ready."
echo "State file: ${STATE_FILE}"
