#!/usr/bin/env bash

set -euo pipefail

DEFAULT_REGION="ap-southeast-1"
DEFAULT_NAME="resilience-vpce-s3"
DEFAULT_ENDPOINT_TYPE="Interface"
ENV_TAG_VALUE="development"
PROJECT_TAG_VALUE="clouddash"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE_DIR="${SCRIPT_DIR}/.state"
STATE_FILE="${STATE_DIR}/current_vpc_endpoint.txt"

REGION="${DEFAULT_REGION}"
NAME="${DEFAULT_NAME}"
VPC_ID=""
SUBNET_IDS_CSV=""
SERVICE_NAME=""
ENDPOINT_TYPE="${DEFAULT_ENDPOINT_TYPE}"

SECURITY_GROUP_NAME=""
SECURITY_GROUP_ID=""
VPC_CIDR_BLOCK=""
VPC_ENDPOINT_ID=""

usage() {
  cat <<'EOF'
Usage:
  ./commands/network/create_vpc_endpoint.sh [--region <aws-region>] [--name <base-name>] [--vpc-id <vpc-id>] [--subnet-ids <subnet-1,subnet-2,...>] [--service-name <service-name>]

Defaults:
  region: ap-southeast-1
  name: resilience-vpce-s3
  endpoint type: Interface
  service name: com.amazonaws.<region>.s3

Notes:
  - The script creates or reuses an interface VPC endpoint for S3 by default.
  - The script uses the default VPC and its default subnets unless overrides are supplied.
  - A security group is created or reused for the endpoint and allows TCP/443 from the VPC CIDR.
  - The endpoint and security group are tagged with:
      - environment=development
      - project=clouddash
  - Local state is written to commands/network/.state/current_vpc_endpoint.txt.
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

write_state() {
  mkdir -p "${STATE_DIR}"
  cat > "${STATE_FILE}" <<EOF
REGION=${REGION}
NAME=${NAME}
VPC_ID=${VPC_ID}
SERVICE_NAME=${SERVICE_NAME}
ENDPOINT_TYPE=${ENDPOINT_TYPE}
SUBNET_IDS=${SUBNET_IDS_CSV}
SECURITY_GROUP_NAME=${SECURITY_GROUP_NAME}
SECURITY_GROUP_ID=${SECURITY_GROUP_ID}
VPC_ENDPOINT_ID=${VPC_ENDPOINT_ID}
EOF
}

find_default_vpc() {
  local vpc_id
  vpc_id="$(aws ec2 describe-vpcs \
    --region "${REGION}" \
    --filters Name=isDefault,Values=true \
    --query 'Vpcs[0].VpcId' \
    --output text)"
  if [[ -z "${vpc_id}" || "${vpc_id}" == "None" ]]; then
    echo "ERROR: No default VPC found in region '${REGION}'. Pass --vpc-id explicitly." >&2
    exit 1
  fi
  echo "${vpc_id}"
}

load_default_subnets() {
  local subnets_text
  subnets_text="$(aws ec2 describe-subnets \
    --region "${REGION}" \
    --filters Name=vpc-id,Values="${VPC_ID}" Name=default-for-az,Values=true \
    --query 'sort_by(Subnets,&AvailabilityZone)[].SubnetId' \
    --output text)"

  if [[ -z "${subnets_text}" || "${subnets_text}" == "None" ]]; then
    subnets_text="$(aws ec2 describe-subnets \
      --region "${REGION}" \
      --filters Name=vpc-id,Values="${VPC_ID}" \
      --query 'sort_by(Subnets,&AvailabilityZone)[].SubnetId' \
      --output text)"
  fi

  if [[ -z "${subnets_text}" || "${subnets_text}" == "None" ]]; then
    echo "ERROR: No subnets found in VPC '${VPC_ID}'." >&2
    exit 1
  fi

  read -r -a SUBNET_IDS <<< "${subnets_text}"
  if [[ "${#SUBNET_IDS[@]}" -lt 1 ]]; then
    echo "ERROR: No usable subnets found in VPC '${VPC_ID}'." >&2
    exit 1
  fi

  SUBNET_IDS_CSV="$(IFS=,; echo "${SUBNET_IDS[*]}")"
}

load_vpc_cidr() {
  VPC_CIDR_BLOCK="$(aws ec2 describe-vpcs \
    --region "${REGION}" \
    --vpc-ids "${VPC_ID}" \
    --query 'Vpcs[0].CidrBlock' \
    --output text)"

  if [[ -z "${VPC_CIDR_BLOCK}" || "${VPC_CIDR_BLOCK}" == "None" ]]; then
    echo "ERROR: Unable to determine CIDR block for VPC '${VPC_ID}'." >&2
    exit 1
  fi
}

ensure_security_group() {
  local group_name="$1"
  local description="$2"
  local group_id

  group_id="$(aws ec2 describe-security-groups \
    --region "${REGION}" \
    --filters Name=vpc-id,Values="${VPC_ID}" Name=group-name,Values="${group_name}" \
    --query 'SecurityGroups[0].GroupId' \
    --output text 2>/dev/null || true)"

  if [[ -z "${group_id}" || "${group_id}" == "None" ]]; then
    group_id="$(aws ec2 create-security-group \
      --region "${REGION}" \
      --group-name "${group_name}" \
      --description "${description}" \
      --vpc-id "${VPC_ID}" \
      --tag-specifications "ResourceType=security-group,Tags=[{Key=Name,Value=${group_name}},{Key=environment,Value=${ENV_TAG_VALUE}},{Key=project,Value=${PROJECT_TAG_VALUE}}]" \
      --query 'GroupId' \
      --output text)"
  fi

  aws ec2 create-tags \
    --region "${REGION}" \
    --resources "${group_id}" \
    --tags "Key=Name,Value=${group_name}" "Key=environment,Value=${ENV_TAG_VALUE}" "Key=project,Value=${PROJECT_TAG_VALUE}" \
    >/dev/null

  local stderr_file
  stderr_file="$(mktemp)"
  if ! aws ec2 authorize-security-group-ingress \
    --region "${REGION}" \
    --group-id "${group_id}" \
    --ip-permissions "IpProtocol=tcp,FromPort=443,ToPort=443,IpRanges=[{CidrIp=${VPC_CIDR_BLOCK},Description=Allow HTTPS from VPC CIDR}]" \
    >/dev/null \
    2>"${stderr_file}"; then
    if ! grep -q "InvalidPermission.Duplicate" "${stderr_file}"; then
      cat "${stderr_file}" >&2
      rm -f "${stderr_file}"
      exit 1
    fi
  fi
  rm -f "${stderr_file}"

  echo "${group_id}"
}

find_existing_endpoint() {
  aws ec2 describe-vpc-endpoints \
    --region "${REGION}" \
    --filters \
      Name=vpc-id,Values="${VPC_ID}" \
      Name=service-name,Values="${SERVICE_NAME}" \
      Name=vpc-endpoint-type,Values="${ENDPOINT_TYPE}" \
    --query 'VpcEndpoints[0].VpcEndpointId' \
    --output text 2>/dev/null || true
}

wait_for_endpoint_available() {
  local deadline state
  deadline=$((SECONDS + 900))

  while (( SECONDS < deadline )); do
    state="$(aws ec2 describe-vpc-endpoints \
      --region "${REGION}" \
      --vpc-endpoint-ids "${VPC_ENDPOINT_ID}" \
      --query 'VpcEndpoints[0].State' \
      --output text 2>/dev/null || true)"
    if [[ "${state}" == "available" ]]; then
      return 0
    fi
    sleep 10
  done

  echo "ERROR: Timed out waiting for VPC endpoint '${VPC_ENDPOINT_ID}' to become available." >&2
  exit 1
}

ensure_endpoint_tags() {
  aws ec2 create-tags \
    --region "${REGION}" \
    --resources "${VPC_ENDPOINT_ID}" \
    --tags "Key=Name,Value=${NAME}" "Key=environment,Value=${ENV_TAG_VALUE}" "Key=project,Value=${PROJECT_TAG_VALUE}" \
    >/dev/null
}

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
    --subnet-ids)
      SUBNET_IDS_CSV="${2:-}"
      shift 2
      ;;
    --service-name)
      SERVICE_NAME="${2:-}"
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

if [[ -z "${REGION}" ]]; then
  echo "ERROR: region cannot be empty." >&2
  exit 1
fi

if [[ -z "${SERVICE_NAME}" ]]; then
  SERVICE_NAME="com.amazonaws.${REGION}.s3"
fi

if [[ -z "${VPC_ID}" ]]; then
  VPC_ID="$(find_default_vpc)"
fi

if [[ -z "${SUBNET_IDS_CSV}" ]]; then
  load_default_subnets
fi

load_vpc_cidr

SECURITY_GROUP_NAME="$(normalize_name "${NAME}-sg")"
SECURITY_GROUP_ID="$(ensure_security_group "${SECURITY_GROUP_NAME}" "VPC endpoint security group for ${NAME}")"

existing_endpoint_id="$(find_existing_endpoint)"
if [[ -n "${existing_endpoint_id}" && "${existing_endpoint_id}" != "None" ]]; then
  VPC_ENDPOINT_ID="${existing_endpoint_id}"
  echo "VPC endpoint already exists: ${VPC_ENDPOINT_ID}"
else
  echo "Creating ${ENDPOINT_TYPE} VPC endpoint for service '${SERVICE_NAME}' in ${REGION}..."
  read -r -a SUBNET_IDS <<< "$(echo "${SUBNET_IDS_CSV}" | tr ',' ' ')"
  VPC_ENDPOINT_ID="$(aws ec2 create-vpc-endpoint \
    --region "${REGION}" \
    --vpc-id "${VPC_ID}" \
    --vpc-endpoint-type "${ENDPOINT_TYPE}" \
    --service-name "${SERVICE_NAME}" \
    --subnet-ids "${SUBNET_IDS[@]}" \
    --security-group-ids "${SECURITY_GROUP_ID}" \
    --tag-specifications "ResourceType=vpc-endpoint,Tags=[{Key=Name,Value=${NAME}},{Key=environment,Value=${ENV_TAG_VALUE}},{Key=project,Value=${PROJECT_TAG_VALUE}}]" \
    --query 'VpcEndpoint.VpcEndpointId' \
    --output text)"
fi

wait_for_endpoint_available
ensure_endpoint_tags
write_state

echo
echo "VPC endpoint is ready."
echo "Region:             ${REGION}"
echo "VPC ID:             ${VPC_ID}"
echo "Service:            ${SERVICE_NAME}"
echo "Endpoint type:      ${ENDPOINT_TYPE}"
echo "Endpoint ID:        ${VPC_ENDPOINT_ID}"
echo "Security Group ID:  ${SECURITY_GROUP_ID}"
echo "State file:         ${STATE_FILE}"
