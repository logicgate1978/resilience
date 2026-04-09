#!/usr/bin/env bash

set -euo pipefail

DEFAULT_PRIMARY_REGION="ap-southeast-1"
DEFAULT_SECONDARY_REGION="ap-southeast-2"
DEFAULT_CONTROL_REGION="eu-west-1"
DEFAULT_NAME="resilience-s3-mrap"
MANAGEMENT_REGION="us-west-2"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE_DIR="${SCRIPT_DIR}/.state"
STATE_FILE="${STATE_DIR}/current_s3_mrap_stack.txt"
SAMPLE_FILE="${STATE_DIR}/sample_replication_object.txt"

PRIMARY_REGION="${DEFAULT_PRIMARY_REGION}"
SECONDARY_REGION="${DEFAULT_SECONDARY_REGION}"
CONTROL_REGION="${DEFAULT_CONTROL_REGION}"
NAME="${DEFAULT_NAME}"

ACCOUNT_ID=""
PRIMARY_BUCKET=""
SECONDARY_BUCKET=""
MRAP_NAME=""
MRAP_ALIAS=""
MRAP_ARN=""
REPLICATION_ROLE_NAME=""
REPLICATION_POLICY_NAME=""

usage() {
  cat <<'EOF'
Usage:
  ./commands/s3/destroy_s3_mrap_stack.sh [--name <base-name>] [--primary-region <aws-region>] [--secondary-region <aws-region>] [--control-region <aws-region>]

Notes:
  - If commands/s3/.state/current_s3_mrap_stack.txt exists, the script reads names from it.
  - CLI arguments override the values from the state file when supplied.
  - The script deletes the MRAP first, removes replication from both buckets, empties the buckets, deletes them, removes the IAM role policy and role, and then deletes the local state file.
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
    normalized="s3-mrap"
  fi
  echo "${normalized}"
}

build_bucket_name() {
  local base="$1"
  local account_id="$2"
  local region="$3"
  local suffix
  local budget
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

load_state() {
  if [[ -f "${STATE_FILE}" ]]; then
    # shellcheck disable=SC1090
    source "${STATE_FILE}"
  fi
}

bucket_exists() {
  local bucket="$1"
  local region="$2"
  aws s3api head-bucket --bucket "${bucket}" --region "${region}" >/dev/null 2>&1
}

empty_bucket() {
  local bucket="$1"
  local region="$2"
  local versions markers key version_id

  if ! bucket_exists "${bucket}" "${region}"; then
    echo "Bucket not found: ${bucket} (${region})"
    return 0
  fi

  echo "Emptying bucket '${bucket}' in ${region}..."
  while true; do
    versions="$(aws s3api list-object-versions \
      --bucket "${bucket}" \
      --region "${region}" \
      --query 'Versions[].[Key,VersionId]' \
      --output text 2>/dev/null || true)"
    markers="$(aws s3api list-object-versions \
      --bucket "${bucket}" \
      --region "${region}" \
      --query 'DeleteMarkers[].[Key,VersionId]' \
      --output text 2>/dev/null || true)"

    if [[ -z "${versions}" && -z "${markers}" ]]; then
      break
    fi

    while IFS=$'\t' read -r key version_id; do
      [[ -z "${key}" || -z "${version_id}" ]] && continue
      aws s3api delete-object \
        --bucket "${bucket}" \
        --key "${key}" \
        --version-id "${version_id}" \
        --region "${region}" \
        >/dev/null
    done <<< "${versions}"

    while IFS=$'\t' read -r key version_id; do
      [[ -z "${key}" || -z "${version_id}" ]] && continue
      aws s3api delete-object \
        --bucket "${bucket}" \
        --key "${key}" \
        --version-id "${version_id}" \
        --region "${region}" \
        >/dev/null
    done <<< "${markers}"
  done
}

delete_bucket() {
  local bucket="$1"
  local region="$2"

  if ! bucket_exists "${bucket}" "${region}"; then
    echo "Bucket not found: ${bucket} (${region})"
    return 0
  fi

  empty_bucket "${bucket}" "${region}"
  echo "Deleting bucket '${bucket}' in ${region}..."
  aws s3api delete-bucket \
    --bucket "${bucket}" \
    --region "${region}" \
    >/dev/null
}

delete_bucket_replication() {
  local bucket="$1"
  local region="$2"

  if ! bucket_exists "${bucket}" "${region}"; then
    return 0
  fi

  aws s3api delete-bucket-replication \
    --bucket "${bucket}" \
    --region "${region}" \
    >/dev/null 2>&1 || true
}

wait_for_mrap_deleted() {
  local deadline current_name
  deadline=$((SECONDS + 1800))

  while (( SECONDS < deadline )); do
    current_name="$(aws s3control get-multi-region-access-point \
      --account-id "${ACCOUNT_ID}" \
      --name "${MRAP_NAME}" \
      --region "${MANAGEMENT_REGION}" \
      --query 'AccessPoint.Name' \
      --output text 2>/dev/null || true)"

    if [[ -z "${current_name}" || "${current_name}" == "None" ]]; then
      return 0
    fi

    sleep 15
  done

  echo "WARNING: Timed out waiting for MRAP '${MRAP_NAME}' to be deleted." >&2
  return 1
}

delete_mrap() {
  local current_name request_token_arn

  current_name="$(aws s3control get-multi-region-access-point \
    --account-id "${ACCOUNT_ID}" \
    --name "${MRAP_NAME}" \
    --region "${MANAGEMENT_REGION}" \
    --query 'AccessPoint.Name' \
    --output text 2>/dev/null || true)"

  if [[ -z "${current_name}" || "${current_name}" == "None" ]]; then
    echo "MRAP not found: ${MRAP_NAME}"
    return 0
  fi

  echo "Deleting MRAP '${MRAP_NAME}'..."
  request_token_arn="$(aws s3control delete-multi-region-access-point \
    --account-id "${ACCOUNT_ID}" \
    --details "{\"Name\":\"${MRAP_NAME}\"}" \
    --region "${MANAGEMENT_REGION}" \
    --query 'RequestTokenARN' \
    --output text)"
  echo "MRAP delete request token: ${request_token_arn}"
  echo "Waiting for MRAP '${MRAP_NAME}' to be deleted..."
  wait_for_mrap_deleted || true
}

delete_replication_role() {
  if ! aws iam get-role --role-name "${REPLICATION_ROLE_NAME}" >/dev/null 2>&1; then
    echo "IAM role not found: ${REPLICATION_ROLE_NAME}"
    return 0
  fi

  aws iam delete-role-policy \
    --role-name "${REPLICATION_ROLE_NAME}" \
    --policy-name "${REPLICATION_POLICY_NAME}" \
    >/dev/null 2>&1 || true

  echo "Deleting IAM role '${REPLICATION_ROLE_NAME}'..."
  aws iam delete-role \
    --role-name "${REPLICATION_ROLE_NAME}" \
    >/dev/null
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
    --control-region)
      CONTROL_REGION="${2:-}"
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
load_state

if [[ -z "${ACCOUNT_ID}" || "${ACCOUNT_ID}" == "None" ]]; then
  ACCOUNT_ID="$(aws sts get-caller-identity --query 'Account' --output text)"
fi

if [[ -z "${PRIMARY_BUCKET}" ]]; then
  PRIMARY_BUCKET="$(build_bucket_name "${NAME}" "${ACCOUNT_ID}" "${PRIMARY_REGION}")"
fi
if [[ -z "${SECONDARY_BUCKET}" ]]; then
  SECONDARY_BUCKET="$(build_bucket_name "${NAME}" "${ACCOUNT_ID}" "${SECONDARY_REGION}")"
fi
if [[ -z "${MRAP_NAME}" ]]; then
  MRAP_NAME="$(build_name "${NAME}" "-mrap" 50)"
fi
if [[ -z "${REPLICATION_ROLE_NAME}" ]]; then
  REPLICATION_ROLE_NAME="$(build_name "${NAME}" "-replication-role" 64)"
fi
if [[ -z "${REPLICATION_POLICY_NAME}" ]]; then
  REPLICATION_POLICY_NAME="$(build_name "${NAME}" "-replication-policy" 128)"
fi

echo "Primary Region:         ${PRIMARY_REGION}"
echo "Secondary Region:       ${SECONDARY_REGION}"
echo "Control Region:         ${CONTROL_REGION}"
echo "Base name:              ${NAME}"
echo "Primary bucket:         ${PRIMARY_BUCKET}"
echo "Secondary bucket:       ${SECONDARY_BUCKET}"
echo "MRAP name:              ${MRAP_NAME}"
echo "Replication role name:  ${REPLICATION_ROLE_NAME}"
echo
echo "Destroying S3 MRAP resources..."
echo

delete_mrap
delete_bucket_replication "${PRIMARY_BUCKET}" "${PRIMARY_REGION}"
delete_bucket_replication "${SECONDARY_BUCKET}" "${SECONDARY_REGION}"
delete_bucket "${PRIMARY_BUCKET}" "${PRIMARY_REGION}"
delete_bucket "${SECONDARY_BUCKET}" "${SECONDARY_REGION}"
delete_replication_role

rm -f "${STATE_FILE}"
rm -f "${SAMPLE_FILE}"

echo
echo "S3 MRAP stack has been removed."
