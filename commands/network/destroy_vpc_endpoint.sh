#!/usr/bin/env bash

set -euo pipefail

DEFAULT_REGION="ap-southeast-1"
DEFAULT_NAME="resilience-vpce-s3"
DEFAULT_ENDPOINT_TYPE="Interface"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE_DIR="${SCRIPT_DIR}/.state"
STATE_FILE="${STATE_DIR}/current_vpc_endpoint.txt"

REGION="${DEFAULT_REGION}"
NAME="${DEFAULT_NAME}"
VPC_ID=""
SERVICE_NAME=""
ENDPOINT_TYPE="${DEFAULT_ENDPOINT_TYPE}"
SECURITY_GROUP_NAME=""
SECURITY_GROUP_ID=""
VPC_ENDPOINT_ID=""

usage() {
  cat <<'EOF'
Usage:
  ./commands/network/destroy_vpc_endpoint.sh [--region <aws-region>] [--name <base-name>] [--vpc-id <vpc-id>] [--service-name <service-name>] [--vpc-endpoint-id <vpce-id>] [--security-group-id <sg-id>]

Defaults:
  region: ap-southeast-1
  name: resilience-vpce-s3
  endpoint type: Interface
  service name: com.amazonaws.<region>.s3

Notes:
  - If commands/network/.state/current_vpc_endpoint.txt exists, the script reads the endpoint and security group from it.
  - CLI arguments override values from the state file.
  - The script deletes the VPC endpoint first, waits for it to disappear, then deletes the helper security group if present.
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
    normalized="vpce"
  fi
  echo "${normalized}"
}

load_state() {
  if [[ -f "${STATE_FILE}" ]]; then
    # shellcheck disable=SC1090
    source "${STATE_FILE}"
  fi
}

lookup_endpoint_id() {
  local -a filters
  local matched_ids

  filters=("Name=tag:Name,Values=${NAME}" "Name=service-name,Values=${SERVICE_NAME}" "Name=vpc-endpoint-type,Values=${ENDPOINT_TYPE}")
  if [[ -n "${VPC_ID}" ]]; then
    filters+=("Name=vpc-id,Values=${VPC_ID}")
  fi

  matched_ids="$(aws ec2 describe-vpc-endpoints \
    --region "${REGION}" \
    --filters "${filters[@]}" \
    --query 'VpcEndpoints[].VpcEndpointId' \
    --output text 2>/dev/null || true)"

  if [[ -z "${matched_ids}" || "${matched_ids}" == "None" ]]; then
    echo ""
    return 0
  fi

  read -r -a ids <<< "${matched_ids}"
  if [[ "${#ids[@]}" -gt 1 ]]; then
    echo "ERROR: Multiple VPC endpoints matched name '${NAME}' in ${REGION}. Pass --vpc-endpoint-id explicitly." >&2
    exit 1
  fi

  echo "${ids[0]}"
}

lookup_security_group_id() {
  local group_id

  group_id="$(aws ec2 describe-security-groups \
    --region "${REGION}" \
    --filters Name=group-name,Values="${SECURITY_GROUP_NAME}" \
    --query 'SecurityGroups[0].GroupId' \
    --output text 2>/dev/null || true)"

  if [[ "${group_id}" == "None" ]]; then
    group_id=""
  fi

  echo "${group_id}"
}

wait_for_endpoint_deletion() {
  local deadline state
  deadline=$((SECONDS + 900))

  while (( SECONDS < deadline )); do
    state="$(aws ec2 describe-vpc-endpoints \
      --region "${REGION}" \
      --vpc-endpoint-ids "${VPC_ENDPOINT_ID}" \
      --query 'VpcEndpoints[0].State' \
      --output text 2>/dev/null || true)"

    if [[ -z "${state}" || "${state}" == "None" || "${state}" == "deleted" ]]; then
      return 0
    fi

    sleep 10
  done

  echo "WARNING: Timed out waiting for VPC endpoint '${VPC_ENDPOINT_ID}' to delete." >&2
}

delete_endpoint() {
  if [[ -z "${VPC_ENDPOINT_ID}" ]]; then
    VPC_ENDPOINT_ID="$(lookup_endpoint_id)"
  fi

  if [[ -z "${VPC_ENDPOINT_ID}" ]]; then
    echo "VPC endpoint not found for '${NAME}' in ${REGION}."
    return 0
  fi

  echo "Deleting VPC endpoint '${VPC_ENDPOINT_ID}' in ${REGION}..."
  aws ec2 delete-vpc-endpoints \
    --region "${REGION}" \
    --vpc-endpoint-ids "${VPC_ENDPOINT_ID}" \
    >/dev/null

  wait_for_endpoint_deletion
}

delete_security_group() {
  local attempt=1

  if [[ -z "${SECURITY_GROUP_ID}" ]]; then
    SECURITY_GROUP_ID="$(lookup_security_group_id)"
  fi

  if [[ -z "${SECURITY_GROUP_ID}" ]]; then
    echo "Security group not found: ${SECURITY_GROUP_NAME}"
    return 0
  fi

  echo "Deleting security group '${SECURITY_GROUP_ID}'..."
  while (( attempt <= 12 )); do
    if aws ec2 delete-security-group \
      --region "${REGION}" \
      --group-id "${SECURITY_GROUP_ID}" \
      >/dev/null 2>&1; then
      return 0
    fi
    sleep 10
    attempt=$((attempt + 1))
  done

  echo "WARNING: Unable to delete security group '${SECURITY_GROUP_ID}'. It may still be referenced." >&2
}

cleanup_state() {
  rm -f "${STATE_FILE}"
}

load_state

while [[ $# -gt 0 ]]; do
  case "$1" in
    --region)
      REGION="${2:-}"
      shift 2
      ;;
    --name)
      NAME="$(normalize_name "${2:-}")"
      shift 2
      ;;
    --vpc-id)
      VPC_ID="${2:-}"
      shift 2
      ;;
    --service-name)
      SERVICE_NAME="${2:-}"
      shift 2
      ;;
    --vpc-endpoint-id)
      VPC_ENDPOINT_ID="${2:-}"
      shift 2
      ;;
    --security-group-id)
      SECURITY_GROUP_ID="${2:-}"
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

NAME="$(normalize_name "${NAME}")"
if [[ -z "${SERVICE_NAME}" ]]; then
  SERVICE_NAME="com.amazonaws.${REGION}.s3"
fi
if [[ -z "${SECURITY_GROUP_NAME}" ]]; then
  SECURITY_GROUP_NAME="$(normalize_name "${NAME}-sg")"
fi

delete_endpoint
delete_security_group
cleanup_state

echo
echo "VPC endpoint cleanup complete."
echo "Region:             ${REGION}"
echo "Name:               ${NAME}"
echo "Service:            ${SERVICE_NAME}"
echo "State file removed: ${STATE_FILE}"
