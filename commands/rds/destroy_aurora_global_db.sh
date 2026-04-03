#!/usr/bin/env bash

set -euo pipefail

DEFAULT_PRIMARY_REGION="ap-southeast-1"
DEFAULT_SECONDARY_REGION="ap-southeast-2"
DEFAULT_NAME="resilience-aurora-global"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE_DIR="${SCRIPT_DIR}/.state"
STATE_FILE="${STATE_DIR}/current_aurora_global_db.txt"

PRIMARY_REGION="${DEFAULT_PRIMARY_REGION}"
SECONDARY_REGION="${DEFAULT_SECONDARY_REGION}"
NAME="${DEFAULT_NAME}"

GLOBAL_CLUSTER_ID=""
PRIMARY_CLUSTER_ID=""
SECONDARY_CLUSTER_ID=""
PRIMARY_INSTANCE_ID=""
SECONDARY_INSTANCE_ID=""
PRIMARY_SUBNET_GROUP=""
SECONDARY_SUBNET_GROUP=""
PRIMARY_SECURITY_GROUP_ID=""
SECONDARY_SECURITY_GROUP_ID=""

PRIMARY_REGION_FROM_ARG="false"
SECONDARY_REGION_FROM_ARG="false"
NAME_FROM_ARG="false"

usage() {
  cat <<'EOF'
Usage:
  ./commands/rds/destroy_aurora_global_db.sh [--name <base-name>] [--primary-region <aws-region>] [--secondary-region <aws-region>]

Defaults:
  name: resilience-aurora-global
  primary-region: ap-southeast-1
  secondary-region: ap-southeast-2

Notes:
  - If commands/rds/.state/current_aurora_global_db.txt exists, the script reads names and IDs from it.
  - CLI arguments override the name and Regions from the state file.
  - The script removes the secondary cluster from the global database first, then the primary, then deletes the instances, clusters, global cluster, subnet groups, security groups, and local state file.
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
    normalized="aurora-global"
  fi
  echo "${normalized}"
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

describe_global_cluster_status() {
  aws rds describe-global-clusters \
    --region "${PRIMARY_REGION}" \
    --global-cluster-identifier "${GLOBAL_CLUSTER_ID}" \
    --query 'GlobalClusters[0].Status' \
    --output text 2>/dev/null || true
}

global_cluster_exists() {
  local status
  status="$(describe_global_cluster_status)"
  [[ -n "${status}" && "${status}" != "None" ]]
}

describe_db_cluster_arn() {
  local region="$1"
  local cluster_id="$2"
  aws rds describe-db-clusters \
    --region "${region}" \
    --db-cluster-identifier "${cluster_id}" \
    --query 'DBClusters[0].DBClusterArn' \
    --output text 2>/dev/null || true
}

db_cluster_exists() {
  local region="$1"
  local cluster_id="$2"
  local arn
  arn="$(describe_db_cluster_arn "${region}" "${cluster_id}")"
  [[ -n "${arn}" && "${arn}" != "None" ]]
}

db_instance_exists() {
  local region="$1"
  local instance_id="$2"
  local value
  value="$(aws rds describe-db-instances \
    --region "${region}" \
    --db-instance-identifier "${instance_id}" \
    --query 'DBInstances[0].DBInstanceIdentifier' \
    --output text 2>/dev/null || true)"
  [[ -n "${value}" && "${value}" != "None" ]]
}

lookup_security_group_id_by_name() {
  local region="$1"
  local group_name="$2"
  local group_id
  group_id="$(aws ec2 describe-security-groups \
    --region "${region}" \
    --filters Name=group-name,Values="${group_name}" \
    --query 'SecurityGroups[0].GroupId' \
    --output text 2>/dev/null || true)"
  if [[ "${group_id}" == "None" ]]; then
    group_id=""
  fi
  echo "${group_id}"
}

wait_for_global_without_member() {
  local cluster_arn="$1"
  local deadline members
  deadline=$((SECONDS + 1800))

  while (( SECONDS < deadline )); do
    if ! global_cluster_exists; then
      return 0
    fi

    members="$(aws rds describe-global-clusters \
      --region "${PRIMARY_REGION}" \
      --global-cluster-identifier "${GLOBAL_CLUSTER_ID}" \
      --query "GlobalClusters[0].GlobalClusterMembers[?DBClusterArn=='${cluster_arn}'] | length(@)" \
      --output text 2>/dev/null || true)"
    if [[ "${members}" == "0" ]]; then
      return 0
    fi
    sleep 20
  done

  echo "WARNING: Timed out waiting for cluster '${cluster_arn}' to detach from global cluster '${GLOBAL_CLUSTER_ID}'." >&2
}

remove_cluster_from_global() {
  local region="$1"
  local cluster_id="$2"
  local cluster_arn

  if ! global_cluster_exists; then
    return 0
  fi

  cluster_arn="$(describe_db_cluster_arn "${region}" "${cluster_id}")"
  if [[ -z "${cluster_arn}" || "${cluster_arn}" == "None" ]]; then
    return 0
  fi

  if aws rds remove-from-global-cluster \
    --region "${region}" \
    --global-cluster-identifier "${GLOBAL_CLUSTER_ID}" \
    --db-cluster-identifier "${cluster_arn}" \
    >/dev/null 2>&1; then
    wait_for_global_without_member "${cluster_arn}"
  fi
}

delete_db_instance() {
  local region="$1"
  local instance_id="$2"

  if ! db_instance_exists "${region}" "${instance_id}"; then
    echo "DB instance not found: ${instance_id} (${region})"
    return 0
  fi

  echo "Deleting DB instance '${instance_id}' in ${region}..."
  aws rds delete-db-instance \
    --region "${region}" \
    --db-instance-identifier "${instance_id}" \
    >/dev/null

  aws rds wait db-instance-deleted \
    --region "${region}" \
    --db-instance-identifier "${instance_id}" || true
}

delete_db_cluster() {
  local region="$1"
  local cluster_id="$2"

  if ! db_cluster_exists "${region}" "${cluster_id}"; then
    echo "DB cluster not found: ${cluster_id} (${region})"
    return 0
  fi

  echo "Deleting DB cluster '${cluster_id}' in ${region}..."
  if aws rds delete-db-cluster \
    --region "${region}" \
    --db-cluster-identifier "${cluster_id}" \
    --skip-final-snapshot \
    >/dev/null 2>&1; then
    aws rds wait db-cluster-deleted \
      --region "${region}" \
      --db-cluster-identifier "${cluster_id}" || true
  fi
}

delete_global_cluster() {
  if ! global_cluster_exists; then
    echo "Global cluster not found: ${GLOBAL_CLUSTER_ID}"
    return 0
  fi

  echo "Deleting global cluster '${GLOBAL_CLUSTER_ID}'..."
  aws rds delete-global-cluster \
    --region "${PRIMARY_REGION}" \
    --global-cluster-identifier "${GLOBAL_CLUSTER_ID}" \
    >/dev/null

  local deadline status
  deadline=$((SECONDS + 1800))
  while (( SECONDS < deadline )); do
    status="$(describe_global_cluster_status)"
    if [[ -z "${status}" || "${status}" == "None" ]]; then
      return 0
    fi
    sleep 15
  done

  echo "WARNING: Timed out waiting for global cluster '${GLOBAL_CLUSTER_ID}' to delete." >&2
}

delete_db_subnet_group() {
  local region="$1"
  local subnet_group_name="$2"
  if [[ -z "${subnet_group_name}" ]]; then
    return 0
  fi

  if ! aws rds describe-db-subnet-groups \
    --region "${region}" \
    --db-subnet-group-name "${subnet_group_name}" \
    >/dev/null 2>&1; then
    echo "DB subnet group not found: ${subnet_group_name} (${region})"
    return 0
  fi

  echo "Deleting DB subnet group '${subnet_group_name}' in ${region}..."
  aws rds delete-db-subnet-group \
    --region "${region}" \
    --db-subnet-group-name "${subnet_group_name}" \
    >/dev/null || true
}

delete_security_group() {
  local region="$1"
  local group_id="$2"
  local group_name="$3"

  if [[ -z "${group_id}" ]]; then
    group_id="$(lookup_security_group_id_by_name "${region}" "${group_name}")"
  fi

  if [[ -z "${group_id}" ]]; then
    echo "Security group not found: ${group_name} (${region})"
    return 0
  fi

  echo "Deleting security group '${group_name}' in ${region}..."
  for _ in $(seq 1 20); do
    if aws ec2 delete-security-group \
      --region "${region}" \
      --group-id "${group_id}" \
      >/dev/null 2>&1; then
      return 0
    fi
    sleep 10
  done

  echo "WARNING: Unable to delete security group '${group_name}' in ${region}." >&2
}

remove_state_files() {
  rm -f "${STATE_FILE}"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --name)
      NAME="${2:?Missing value for --name}"
      NAME_FROM_ARG="true"
      shift 2
      ;;
    --primary-region)
      PRIMARY_REGION="${2:?Missing value for --primary-region}"
      PRIMARY_REGION_FROM_ARG="true"
      shift 2
      ;;
    --secondary-region)
      SECONDARY_REGION="${2:?Missing value for --secondary-region}"
      SECONDARY_REGION_FROM_ARG="true"
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

if [[ "${PRIMARY_REGION_FROM_ARG}" != "true" ]]; then
  PRIMARY_REGION="${PRIMARY_REGION:-${DEFAULT_PRIMARY_REGION}}"
fi

if [[ "${SECONDARY_REGION_FROM_ARG}" != "true" ]]; then
  SECONDARY_REGION="${SECONDARY_REGION:-${DEFAULT_SECONDARY_REGION}}"
fi

if [[ "${NAME_FROM_ARG}" != "true" ]]; then
  NAME="${NAME:-${DEFAULT_NAME}}"
fi

NAME="$(normalize_name "${NAME}")"
GLOBAL_CLUSTER_ID="${GLOBAL_CLUSTER_ID:-$(build_name "${NAME}" "" 63)}"
PRIMARY_CLUSTER_ID="${PRIMARY_CLUSTER_ID:-$(build_name "${NAME}" "-primary-cluster" 63)}"
SECONDARY_CLUSTER_ID="${SECONDARY_CLUSTER_ID:-$(build_name "${NAME}" "-secondary-cluster" 63)}"
PRIMARY_INSTANCE_ID="${PRIMARY_INSTANCE_ID:-$(build_name "${NAME}" "-primary-1" 63)}"
SECONDARY_INSTANCE_ID="${SECONDARY_INSTANCE_ID:-$(build_name "${NAME}" "-secondary-1" 63)}"
PRIMARY_SUBNET_GROUP="${PRIMARY_SUBNET_GROUP:-$(build_name "${NAME}" "-primary-subnet" 255)}"
SECONDARY_SUBNET_GROUP="${SECONDARY_SUBNET_GROUP:-$(build_name "${NAME}" "-secondary-subnet" 255)}"
PRIMARY_SECURITY_GROUP_NAME="$(build_name "${NAME}" "-primary-sg" 255)"
SECONDARY_SECURITY_GROUP_NAME="$(build_name "${NAME}" "-secondary-sg" 255)"

echo "Primary Region:          ${PRIMARY_REGION}"
echo "Secondary Region:        ${SECONDARY_REGION}"
echo "Base name:               ${NAME}"
echo "Global cluster ID:       ${GLOBAL_CLUSTER_ID}"
echo "Primary cluster ID:      ${PRIMARY_CLUSTER_ID}"
echo "Secondary cluster ID:    ${SECONDARY_CLUSTER_ID}"
echo
echo "Destroying Aurora Global Database resources..."

remove_cluster_from_global "${SECONDARY_REGION}" "${SECONDARY_CLUSTER_ID}"
remove_cluster_from_global "${PRIMARY_REGION}" "${PRIMARY_CLUSTER_ID}"

delete_db_instance "${SECONDARY_REGION}" "${SECONDARY_INSTANCE_ID}"
delete_db_cluster "${SECONDARY_REGION}" "${SECONDARY_CLUSTER_ID}"

delete_db_instance "${PRIMARY_REGION}" "${PRIMARY_INSTANCE_ID}"
delete_db_cluster "${PRIMARY_REGION}" "${PRIMARY_CLUSTER_ID}"

delete_global_cluster
delete_db_subnet_group "${SECONDARY_REGION}" "${SECONDARY_SUBNET_GROUP}"
delete_db_subnet_group "${PRIMARY_REGION}" "${PRIMARY_SUBNET_GROUP}"
delete_security_group "${SECONDARY_REGION}" "${SECONDARY_SECURITY_GROUP_ID}" "${SECONDARY_SECURITY_GROUP_NAME}"
delete_security_group "${PRIMARY_REGION}" "${PRIMARY_SECURITY_GROUP_ID}" "${PRIMARY_SECURITY_GROUP_NAME}"
remove_state_files

echo
echo "Aurora Global Database cleanup finished."
echo "Removed state file: ${STATE_FILE}"
