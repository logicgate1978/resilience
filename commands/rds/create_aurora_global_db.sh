#!/usr/bin/env bash

set -euo pipefail

DEFAULT_PRIMARY_REGION="ap-southeast-1"
DEFAULT_SECONDARY_REGION="ap-southeast-2"
DEFAULT_NAME="resilience-aurora-global"
DEFAULT_ENGINE="aurora-mysql"
DEFAULT_INSTANCE_CLASS="db.t4g.medium"
DEFAULT_MASTER_USERNAME="dbadmin"
DEFAULT_DATABASE_NAME="appdb"
DEFAULT_BACKUP_RETENTION_DAYS="1"
ENV_TAG_VALUE="development"
PROJECT_TAG_VALUE="clouddash"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE_DIR="${SCRIPT_DIR}/.state"
STATE_FILE="${STATE_DIR}/current_aurora_global_db.txt"

PRIMARY_REGION="${DEFAULT_PRIMARY_REGION}"
SECONDARY_REGION="${DEFAULT_SECONDARY_REGION}"
NAME="${DEFAULT_NAME}"
ENGINE="${DEFAULT_ENGINE}"
ENGINE_VERSION=""
INSTANCE_CLASS="${DEFAULT_INSTANCE_CLASS}"
MASTER_USERNAME="${DEFAULT_MASTER_USERNAME}"
MASTER_PASSWORD=""
DATABASE_NAME="${DEFAULT_DATABASE_NAME}"
PRIMARY_VPC_ID=""
SECONDARY_VPC_ID=""

usage() {
  cat <<'EOF'
Usage:
  ./commands/rds/create_aurora_global_db.sh [--name <base-name>] [--primary-region <aws-region>] [--secondary-region <aws-region>] [--engine <aurora-engine>] [--engine-version <version>] [--instance-class <db-class>] [--master-username <name>] [--master-password <password>] [--database-name <name>] [--primary-vpc-id <vpc-id>] [--secondary-vpc-id <vpc-id>]

Defaults:
  name: resilience-aurora-global
  primary-region: ap-southeast-1
  secondary-region: ap-southeast-2
  engine: aurora-mysql
  instance-class: db.t4g.medium
  master-username: dbadmin
  database-name: appdb

Notes:
  - The script creates a minimal Aurora Global Database topology:
      - one primary Aurora cluster with one writer instance
      - one secondary Aurora cluster with one instance
  - Default VPCs and their default subnets are used unless VPC IDs are supplied.
  - The created Aurora clusters and instances are tagged with:
      - environment=development
      - project=clouddash
  - If --master-password is omitted, the script generates one and stores it in the local state file.
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

generate_password() {
  LC_ALL=C dd if=/dev/urandom bs=64 count=1 2>/dev/null | base64 | tr -dc 'A-Za-z0-9' | cut -c1-24
}

find_default_vpc() {
  local region="$1"
  local vpc_id
  vpc_id="$(aws ec2 describe-vpcs \
    --region "${region}" \
    --filters Name=isDefault,Values=true \
    --query 'Vpcs[0].VpcId' \
    --output text)"
  if [[ -z "${vpc_id}" || "${vpc_id}" == "None" ]]; then
    echo "ERROR: No default VPC found in region '${region}'. Pass the VPC ID explicitly." >&2
    exit 1
  fi
  echo "${vpc_id}"
}

get_subnet_csv() {
  local region="$1"
  local vpc_id="$2"
  local subnets_text
  local subnet_ids

  subnets_text="$(aws ec2 describe-subnets \
    --region "${region}" \
    --filters Name=vpc-id,Values="${vpc_id}" Name=default-for-az,Values=true \
    --query 'sort_by(Subnets,&AvailabilityZone)[].SubnetId' \
    --output text)"

  if [[ -z "${subnets_text}" || "${subnets_text}" == "None" ]]; then
    subnets_text="$(aws ec2 describe-subnets \
      --region "${region}" \
      --filters Name=vpc-id,Values="${vpc_id}" \
      --query 'sort_by(Subnets,&AvailabilityZone)[].SubnetId' \
      --output text)"
  fi

  if [[ -z "${subnets_text}" || "${subnets_text}" == "None" ]]; then
    echo "ERROR: No subnets found in VPC '${vpc_id}' for region '${region}'." >&2
    exit 1
  fi

  read -r -a subnet_ids <<< "${subnets_text}"
  if [[ "${#subnet_ids[@]}" -lt 2 ]]; then
    echo "ERROR: Aurora DB subnet groups require at least two subnets in region '${region}'." >&2
    exit 1
  fi

  (IFS=,; echo "${subnet_ids[*]}")
}

ensure_db_subnet_group() {
  local region="$1"
  local subnet_group_name="$2"
  local subnet_csv="$3"
  local existing
  existing="$(aws rds describe-db-subnet-groups \
    --region "${region}" \
    --db-subnet-group-name "${subnet_group_name}" \
    --query 'DBSubnetGroups[0].DBSubnetGroupName' \
    --output text 2>/dev/null || true)"

  if [[ -n "${existing}" && "${existing}" != "None" ]]; then
    echo "${subnet_group_name}"
    return 0
  fi

  read -r -a subnet_ids <<< "$(echo "${subnet_csv}" | tr ',' ' ')"
  aws rds create-db-subnet-group \
    --region "${region}" \
    --db-subnet-group-name "${subnet_group_name}" \
    --db-subnet-group-description "Aurora Global Database subnet group for ${NAME} in ${region}" \
    --subnet-ids "${subnet_ids[@]}" \
    --tags "Key=Name,Value=${subnet_group_name}" "Key=environment,Value=${ENV_TAG_VALUE}" "Key=project,Value=${PROJECT_TAG_VALUE}" \
    >/dev/null

  echo "${subnet_group_name}"
}

ensure_security_group() {
  local region="$1"
  local vpc_id="$2"
  local group_name="$3"
  local group_id

  group_id="$(aws ec2 describe-security-groups \
    --region "${region}" \
    --filters Name=vpc-id,Values="${vpc_id}" Name=group-name,Values="${group_name}" \
    --query 'SecurityGroups[0].GroupId' \
    --output text 2>/dev/null || true)"

  if [[ -z "${group_id}" || "${group_id}" == "None" ]]; then
    group_id="$(aws ec2 create-security-group \
      --region "${region}" \
      --group-name "${group_name}" \
      --description "Aurora Global Database security group for ${NAME} in ${region}" \
      --vpc-id "${vpc_id}" \
      --tag-specifications "ResourceType=security-group,Tags=[{Key=Name,Value=${group_name}},{Key=environment,Value=${ENV_TAG_VALUE}},{Key=project,Value=${PROJECT_TAG_VALUE}}]" \
      --query 'GroupId' \
      --output text)"
  fi

  aws ec2 create-tags \
    --region "${region}" \
    --resources "${group_id}" \
    --tags "Key=Name,Value=${group_name}" "Key=environment,Value=${ENV_TAG_VALUE}" "Key=project,Value=${PROJECT_TAG_VALUE}" \
    >/dev/null

  echo "${group_id}"
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

wait_for_global_cluster_available() {
  local deadline status
  deadline=$((SECONDS + 1800))

  while (( SECONDS < deadline )); do
    status="$(describe_global_cluster_status)"
    if [[ "${status}" == "available" ]]; then
      return 0
    fi
    sleep 15
  done

  echo "ERROR: Timed out waiting for global cluster '${GLOBAL_CLUSTER_ID}' to become available." >&2
  exit 1
}

wait_for_global_member_count() {
  local expected="$1"
  local deadline count
  deadline=$((SECONDS + 1800))

  while (( SECONDS < deadline )); do
    count="$(aws rds describe-global-clusters \
      --region "${PRIMARY_REGION}" \
      --global-cluster-identifier "${GLOBAL_CLUSTER_ID}" \
      --query 'length(GlobalClusters[0].GlobalClusterMembers)' \
      --output text 2>/dev/null || true)"
    if [[ "${count}" == "${expected}" ]]; then
      return 0
    fi
    sleep 20
  done

  echo "ERROR: Timed out waiting for global cluster '${GLOBAL_CLUSTER_ID}' to have ${expected} member(s)." >&2
  exit 1
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

wait_for_cluster_available() {
  local region="$1"
  local cluster_id="$2"
  aws rds wait db-cluster-available \
    --region "${region}" \
    --db-cluster-identifier "${cluster_id}"
}

wait_for_instance_available() {
  local region="$1"
  local instance_id="$2"
  aws rds wait db-instance-available \
    --region "${region}" \
    --db-instance-identifier "${instance_id}"
}

tag_rds_resource() {
  local region="$1"
  local resource_arn="$2"
  local name_tag="$3"
  if [[ -z "${resource_arn}" || "${resource_arn}" == "None" ]]; then
    return 0
  fi
  aws rds add-tags-to-resource \
    --region "${region}" \
    --resource-name "${resource_arn}" \
    --tags "Key=Name,Value=${name_tag}" "Key=environment,Value=${ENV_TAG_VALUE}" "Key=project,Value=${PROJECT_TAG_VALUE}" \
    >/dev/null
}

ensure_global_cluster() {
  local primary_cluster_arn

  if global_cluster_exists; then
    wait_for_global_cluster_available
    return 0
  fi

  primary_cluster_arn="$(describe_db_cluster_arn "${PRIMARY_REGION}" "${PRIMARY_CLUSTER_ID}")"

  if [[ -n "${primary_cluster_arn}" && "${primary_cluster_arn}" != "None" ]]; then
    aws rds create-global-cluster \
      --region "${PRIMARY_REGION}" \
      --global-cluster-identifier "${GLOBAL_CLUSTER_ID}" \
      --source-db-cluster-identifier "${primary_cluster_arn}" \
      >/dev/null
  else
    if [[ -n "${ENGINE_VERSION}" ]]; then
      aws rds create-global-cluster \
        --region "${PRIMARY_REGION}" \
        --global-cluster-identifier "${GLOBAL_CLUSTER_ID}" \
        --engine "${ENGINE}" \
        --engine-version "${ENGINE_VERSION}" \
        >/dev/null
    else
      aws rds create-global-cluster \
        --region "${PRIMARY_REGION}" \
        --global-cluster-identifier "${GLOBAL_CLUSTER_ID}" \
        --engine "${ENGINE}" \
        >/dev/null
    fi
  fi

  wait_for_global_cluster_available
}

ensure_primary_cluster() {
  if db_cluster_exists "${PRIMARY_REGION}" "${PRIMARY_CLUSTER_ID}"; then
    wait_for_cluster_available "${PRIMARY_REGION}" "${PRIMARY_CLUSTER_ID}"
    return 0
  fi

  local args=(
    aws rds create-db-cluster
    --region "${PRIMARY_REGION}"
    --db-cluster-identifier "${PRIMARY_CLUSTER_ID}"
    --engine "${ENGINE}"
    --global-cluster-identifier "${GLOBAL_CLUSTER_ID}"
    --master-username "${MASTER_USERNAME}"
    --master-user-password "${MASTER_PASSWORD}"
    --database-name "${DATABASE_NAME}"
    --db-subnet-group-name "${PRIMARY_SUBNET_GROUP}"
    --vpc-security-group-ids "${PRIMARY_SECURITY_GROUP_ID}"
    --backup-retention-period "${DEFAULT_BACKUP_RETENTION_DAYS}"
    --no-deletion-protection
    --copy-tags-to-snapshot
    --tags "Key=Name,Value=${PRIMARY_CLUSTER_ID}" "Key=environment,Value=${ENV_TAG_VALUE}" "Key=project,Value=${PROJECT_TAG_VALUE}"
  )

  if [[ -n "${ENGINE_VERSION}" ]]; then
    args+=(--engine-version "${ENGINE_VERSION}")
  fi

  "${args[@]}" >/dev/null
  wait_for_cluster_available "${PRIMARY_REGION}" "${PRIMARY_CLUSTER_ID}"
}

ensure_primary_instance() {
  if db_instance_exists "${PRIMARY_REGION}" "${PRIMARY_INSTANCE_ID}"; then
    wait_for_instance_available "${PRIMARY_REGION}" "${PRIMARY_INSTANCE_ID}"
    return 0
  fi

  aws rds create-db-instance \
    --region "${PRIMARY_REGION}" \
    --db-instance-identifier "${PRIMARY_INSTANCE_ID}" \
    --db-cluster-identifier "${PRIMARY_CLUSTER_ID}" \
    --engine "${ENGINE}" \
    --db-instance-class "${INSTANCE_CLASS}" \
    --no-publicly-accessible \
    --tags "Key=Name,Value=${PRIMARY_INSTANCE_ID}" "Key=environment,Value=${ENV_TAG_VALUE}" "Key=project,Value=${PROJECT_TAG_VALUE}" \
    >/dev/null

  wait_for_instance_available "${PRIMARY_REGION}" "${PRIMARY_INSTANCE_ID}"
}

ensure_secondary_cluster() {
  if db_cluster_exists "${SECONDARY_REGION}" "${SECONDARY_CLUSTER_ID}"; then
    wait_for_cluster_available "${SECONDARY_REGION}" "${SECONDARY_CLUSTER_ID}"
    return 0
  fi

  aws rds create-db-cluster \
    --region "${SECONDARY_REGION}" \
    --db-cluster-identifier "${SECONDARY_CLUSTER_ID}" \
    --engine "${ENGINE}" \
    --engine-version "${PRIMARY_ENGINE_VERSION}" \
    --global-cluster-identifier "${GLOBAL_CLUSTER_ID}" \
    --db-subnet-group-name "${SECONDARY_SUBNET_GROUP}" \
    --vpc-security-group-ids "${SECONDARY_SECURITY_GROUP_ID}" \
    --backup-retention-period "${DEFAULT_BACKUP_RETENTION_DAYS}" \
    --no-deletion-protection \
    --copy-tags-to-snapshot \
    --tags "Key=Name,Value=${SECONDARY_CLUSTER_ID}" "Key=environment,Value=${ENV_TAG_VALUE}" "Key=project,Value=${PROJECT_TAG_VALUE}" \
    >/dev/null

  wait_for_cluster_available "${SECONDARY_REGION}" "${SECONDARY_CLUSTER_ID}"
}

ensure_secondary_instance() {
  if db_instance_exists "${SECONDARY_REGION}" "${SECONDARY_INSTANCE_ID}"; then
    wait_for_instance_available "${SECONDARY_REGION}" "${SECONDARY_INSTANCE_ID}"
    return 0
  fi

  aws rds create-db-instance \
    --region "${SECONDARY_REGION}" \
    --db-instance-identifier "${SECONDARY_INSTANCE_ID}" \
    --db-cluster-identifier "${SECONDARY_CLUSTER_ID}" \
    --engine "${ENGINE}" \
    --db-instance-class "${INSTANCE_CLASS}" \
    --no-publicly-accessible \
    --tags "Key=Name,Value=${SECONDARY_INSTANCE_ID}" "Key=environment,Value=${ENV_TAG_VALUE}" "Key=project,Value=${PROJECT_TAG_VALUE}" \
    >/dev/null

  wait_for_instance_available "${SECONDARY_REGION}" "${SECONDARY_INSTANCE_ID}"
}

write_state() {
  local primary_cluster_arn secondary_cluster_arn

  primary_cluster_arn="$(describe_db_cluster_arn "${PRIMARY_REGION}" "${PRIMARY_CLUSTER_ID}")"
  secondary_cluster_arn="$(describe_db_cluster_arn "${SECONDARY_REGION}" "${SECONDARY_CLUSTER_ID}")"
  PRIMARY_CLUSTER_ENDPOINT="$(aws rds describe-db-clusters \
    --region "${PRIMARY_REGION}" \
    --db-cluster-identifier "${PRIMARY_CLUSTER_ID}" \
    --query 'DBClusters[0].Endpoint' \
    --output text)"
  SECONDARY_CLUSTER_ENDPOINT="$(aws rds describe-db-clusters \
    --region "${SECONDARY_REGION}" \
    --db-cluster-identifier "${SECONDARY_CLUSTER_ID}" \
    --query 'DBClusters[0].Endpoint' \
    --output text)"

  mkdir -p "${STATE_DIR}"
  cat > "${STATE_FILE}" <<EOF
PRIMARY_REGION=${PRIMARY_REGION}
SECONDARY_REGION=${SECONDARY_REGION}
NAME=${NAME}
GLOBAL_CLUSTER_ID=${GLOBAL_CLUSTER_ID}
PRIMARY_CLUSTER_ID=${PRIMARY_CLUSTER_ID}
SECONDARY_CLUSTER_ID=${SECONDARY_CLUSTER_ID}
PRIMARY_INSTANCE_ID=${PRIMARY_INSTANCE_ID}
SECONDARY_INSTANCE_ID=${SECONDARY_INSTANCE_ID}
PRIMARY_SUBNET_GROUP=${PRIMARY_SUBNET_GROUP}
SECONDARY_SUBNET_GROUP=${SECONDARY_SUBNET_GROUP}
PRIMARY_SECURITY_GROUP_ID=${PRIMARY_SECURITY_GROUP_ID}
SECONDARY_SECURITY_GROUP_ID=${SECONDARY_SECURITY_GROUP_ID}
PRIMARY_VPC_ID=${PRIMARY_VPC_ID}
SECONDARY_VPC_ID=${SECONDARY_VPC_ID}
ENGINE=${ENGINE}
ENGINE_VERSION=${PRIMARY_ENGINE_VERSION}
INSTANCE_CLASS=${INSTANCE_CLASS}
MASTER_USERNAME=${MASTER_USERNAME}
MASTER_PASSWORD=${MASTER_PASSWORD}
DATABASE_NAME=${DATABASE_NAME}
PRIMARY_CLUSTER_ENDPOINT=${PRIMARY_CLUSTER_ENDPOINT}
SECONDARY_CLUSTER_ENDPOINT=${SECONDARY_CLUSTER_ENDPOINT}
PRIMARY_CLUSTER_ARN=${primary_cluster_arn}
SECONDARY_CLUSTER_ARN=${secondary_cluster_arn}
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --name)
      NAME="${2:?Missing value for --name}"
      shift 2
      ;;
    --primary-region)
      PRIMARY_REGION="${2:?Missing value for --primary-region}"
      shift 2
      ;;
    --secondary-region)
      SECONDARY_REGION="${2:?Missing value for --secondary-region}"
      shift 2
      ;;
    --engine)
      ENGINE="${2:?Missing value for --engine}"
      shift 2
      ;;
    --engine-version)
      ENGINE_VERSION="${2:?Missing value for --engine-version}"
      shift 2
      ;;
    --instance-class)
      INSTANCE_CLASS="${2:?Missing value for --instance-class}"
      shift 2
      ;;
    --master-username)
      MASTER_USERNAME="${2:?Missing value for --master-username}"
      shift 2
      ;;
    --master-password)
      MASTER_PASSWORD="${2:?Missing value for --master-password}"
      shift 2
      ;;
    --database-name)
      DATABASE_NAME="${2:?Missing value for --database-name}"
      shift 2
      ;;
    --primary-vpc-id)
      PRIMARY_VPC_ID="${2:?Missing value for --primary-vpc-id}"
      shift 2
      ;;
    --secondary-vpc-id)
      SECONDARY_VPC_ID="${2:?Missing value for --secondary-vpc-id}"
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
require_command base64
require_command tr
require_command cut

if [[ "${PRIMARY_REGION}" == "${SECONDARY_REGION}" ]]; then
  echo "ERROR: primary and secondary Regions must be different." >&2
  exit 1
fi

NAME="$(normalize_name "${NAME}")"
GLOBAL_CLUSTER_ID="$(build_name "${NAME}" "" 63)"
PRIMARY_CLUSTER_ID="$(build_name "${NAME}" "-primary-cluster" 63)"
SECONDARY_CLUSTER_ID="$(build_name "${NAME}" "-secondary-cluster" 63)"
PRIMARY_INSTANCE_ID="$(build_name "${NAME}" "-primary-1" 63)"
SECONDARY_INSTANCE_ID="$(build_name "${NAME}" "-secondary-1" 63)"
PRIMARY_SUBNET_GROUP="$(build_name "${NAME}" "-primary-subnet" 255)"
SECONDARY_SUBNET_GROUP="$(build_name "${NAME}" "-secondary-subnet" 255)"
PRIMARY_SECURITY_GROUP_NAME="$(build_name "${NAME}" "-primary-sg" 255)"
SECONDARY_SECURITY_GROUP_NAME="$(build_name "${NAME}" "-secondary-sg" 255)"

if [[ -z "${MASTER_PASSWORD}" ]]; then
  MASTER_PASSWORD="$(generate_password)"
fi

if [[ -z "${PRIMARY_VPC_ID}" ]]; then
  PRIMARY_VPC_ID="$(find_default_vpc "${PRIMARY_REGION}")"
fi

if [[ -z "${SECONDARY_VPC_ID}" ]]; then
  SECONDARY_VPC_ID="$(find_default_vpc "${SECONDARY_REGION}")"
fi

PRIMARY_SUBNET_CSV="$(get_subnet_csv "${PRIMARY_REGION}" "${PRIMARY_VPC_ID}")"
SECONDARY_SUBNET_CSV="$(get_subnet_csv "${SECONDARY_REGION}" "${SECONDARY_VPC_ID}")"

PRIMARY_SUBNET_GROUP="$(ensure_db_subnet_group "${PRIMARY_REGION}" "${PRIMARY_SUBNET_GROUP}" "${PRIMARY_SUBNET_CSV}")"
SECONDARY_SUBNET_GROUP="$(ensure_db_subnet_group "${SECONDARY_REGION}" "${SECONDARY_SUBNET_GROUP}" "${SECONDARY_SUBNET_CSV}")"
PRIMARY_SECURITY_GROUP_ID="$(ensure_security_group "${PRIMARY_REGION}" "${PRIMARY_VPC_ID}" "${PRIMARY_SECURITY_GROUP_NAME}")"
SECONDARY_SECURITY_GROUP_ID="$(ensure_security_group "${SECONDARY_REGION}" "${SECONDARY_VPC_ID}" "${SECONDARY_SECURITY_GROUP_NAME}")"

echo "Primary Region:          ${PRIMARY_REGION}"
echo "Secondary Region:        ${SECONDARY_REGION}"
echo "Base name:               ${NAME}"
echo "Global cluster ID:       ${GLOBAL_CLUSTER_ID}"
echo "Primary cluster ID:      ${PRIMARY_CLUSTER_ID}"
echo "Secondary cluster ID:    ${SECONDARY_CLUSTER_ID}"
echo "Instance class:          ${INSTANCE_CLASS}"
echo "Engine:                  ${ENGINE}"
echo
echo "Creating or reusing Aurora Global Database resources..."

ensure_global_cluster
ensure_primary_cluster
ensure_primary_instance

PRIMARY_ENGINE_VERSION="$(aws rds describe-db-clusters \
  --region "${PRIMARY_REGION}" \
  --db-cluster-identifier "${PRIMARY_CLUSTER_ID}" \
  --query 'DBClusters[0].EngineVersion' \
  --output text)"

tag_rds_resource "${PRIMARY_REGION}" "$(describe_db_cluster_arn "${PRIMARY_REGION}" "${PRIMARY_CLUSTER_ID}")" "${PRIMARY_CLUSTER_ID}"

ensure_secondary_cluster
ensure_secondary_instance
tag_rds_resource "${SECONDARY_REGION}" "$(describe_db_cluster_arn "${SECONDARY_REGION}" "${SECONDARY_CLUSTER_ID}")" "${SECONDARY_CLUSTER_ID}"

wait_for_global_cluster_available
wait_for_global_member_count 2
write_state

echo
echo "Aurora Global Database is ready."
echo "Primary endpoint:        ${PRIMARY_CLUSTER_ENDPOINT}"
echo "Secondary endpoint:      ${SECONDARY_CLUSTER_ENDPOINT}"
echo "Engine version:          ${PRIMARY_ENGINE_VERSION}"
echo "State saved to:          ${STATE_FILE}"
echo "Master username:         ${MASTER_USERNAME}"
echo "Master password:         ${MASTER_PASSWORD}"
