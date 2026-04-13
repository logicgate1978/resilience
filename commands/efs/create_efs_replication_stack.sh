#!/usr/bin/env bash

set -euo pipefail

DEFAULT_PRIMARY_REGION="ap-southeast-1"
DEFAULT_SECONDARY_REGION="ap-southeast-2"
DEFAULT_NAME="resilience-efs-replication"
ENV_TAG_VALUE="development"
PROJECT_TAG_VALUE="clouddash"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE_DIR="${SCRIPT_DIR}/.state"
STATE_FILE="${STATE_DIR}/current_efs_replication_stack.txt"

PRIMARY_REGION="${DEFAULT_PRIMARY_REGION}"
SECONDARY_REGION="${DEFAULT_SECONDARY_REGION}"
NAME="${DEFAULT_NAME}"

PRIMARY_NAME_TAG=""
SECONDARY_NAME_TAG=""
PRIMARY_CREATION_TOKEN=""
SECONDARY_CREATION_TOKEN=""
PRIMARY_FILE_SYSTEM_ID=""
SECONDARY_FILE_SYSTEM_ID=""

usage() {
  cat <<'EOF'
Usage:
  ./commands/efs/create_efs_replication_stack.sh [--name <base-name>] [--primary-region <aws-region>] [--secondary-region <aws-region>]

Defaults:
  name: resilience-efs-replication
  primary-region: ap-southeast-1
  secondary-region: ap-southeast-2

Notes:
  - Creates one EFS file system in the primary Region.
  - Creates one EFS file system in the secondary Region.
  - Configures one-way EFS replication from the primary file system to the secondary file system.
  - Uses an existing destination file system in the secondary Region.
  - Writes local state to commands/efs/.state/current_efs_replication_stack.txt.
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
    normalized="efs-replication"
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

write_state() {
  mkdir -p "${STATE_DIR}"
  cat > "${STATE_FILE}" <<EOF
PRIMARY_REGION=${PRIMARY_REGION}
SECONDARY_REGION=${SECONDARY_REGION}
NAME=${NAME}
PRIMARY_NAME_TAG=${PRIMARY_NAME_TAG}
SECONDARY_NAME_TAG=${SECONDARY_NAME_TAG}
PRIMARY_CREATION_TOKEN=${PRIMARY_CREATION_TOKEN}
SECONDARY_CREATION_TOKEN=${SECONDARY_CREATION_TOKEN}
PRIMARY_FILE_SYSTEM_ID=${PRIMARY_FILE_SYSTEM_ID}
SECONDARY_FILE_SYSTEM_ID=${SECONDARY_FILE_SYSTEM_ID}
EOF
}

file_system_exists() {
  local region="$1"
  local file_system_id="$2"
  aws efs describe-file-systems \
    --region "${region}" \
    --file-system-id "${file_system_id}" \
    --query 'FileSystems[0].FileSystemId' \
    --output text >/dev/null 2>&1
}

find_file_system_by_name_tag() {
  local region="$1"
  local name_tag="$2"
  local ids file_system_id actual_name env_tag project_tag

  ids="$(aws efs describe-file-systems \
    --region "${region}" \
    --query 'FileSystems[].FileSystemId' \
    --output text 2>/dev/null || true)"

  for file_system_id in ${ids}; do
    actual_name="$(aws efs list-tags-for-resource \
      --region "${region}" \
      --resource-id "${file_system_id}" \
      --query "Tags[?Key=='Name'].Value | [0]" \
      --output text 2>/dev/null || true)"
    env_tag="$(aws efs list-tags-for-resource \
      --region "${region}" \
      --resource-id "${file_system_id}" \
      --query "Tags[?Key=='environment'].Value | [0]" \
      --output text 2>/dev/null || true)"
    project_tag="$(aws efs list-tags-for-resource \
      --region "${region}" \
      --resource-id "${file_system_id}" \
      --query "Tags[?Key=='project'].Value | [0]" \
      --output text 2>/dev/null || true)"

    if [[ "${actual_name}" == "${name_tag}" && "${env_tag}" == "${ENV_TAG_VALUE}" && "${project_tag}" == "${PROJECT_TAG_VALUE}" ]]; then
      echo "${file_system_id}"
      return 0
    fi
  done

  return 1
}

wait_for_file_system_available() {
  local region="$1"
  local file_system_id="$2"
  local deadline state
  deadline=$((SECONDS + 1200))

  while (( SECONDS < deadline )); do
    state="$(aws efs describe-file-systems \
      --region "${region}" \
      --file-system-id "${file_system_id}" \
      --query 'FileSystems[0].LifeCycleState' \
      --output text 2>/dev/null || true)"
    if [[ "${state}" == "available" ]]; then
      return 0
    fi
    sleep 10
  done

  echo "ERROR: Timed out waiting for EFS file system '${file_system_id}' in ${region} to become available." >&2
  exit 1
}

wait_for_overwrite_protection() {
  local region="$1"
  local file_system_id="$2"
  local desired_state="$3"
  local deadline current_state
  deadline=$((SECONDS + 900))

  while (( SECONDS < deadline )); do
    current_state="$(aws efs describe-file-systems \
      --region "${region}" \
      --file-system-id "${file_system_id}" \
      --query 'FileSystems[0].FileSystemProtection.ReplicationOverwriteProtection' \
      --output text 2>/dev/null || true)"
    if [[ "${current_state}" == "${desired_state}" ]]; then
      return 0
    fi
    sleep 10
  done

  echo "ERROR: Timed out waiting for replication overwrite protection '${desired_state}' on '${file_system_id}' in ${region}." >&2
  exit 1
}

ensure_file_system() {
  local region="$1"
  local name_tag="$2"
  local creation_token="$3"
  local file_system_id

  file_system_id="$(find_file_system_by_name_tag "${region}" "${name_tag}" || true)"
  if [[ -n "${file_system_id}" && "${file_system_id}" != "None" ]]; then
    echo "EFS file system already exists: ${file_system_id} (${region}, Name=${name_tag})" >&2
    wait_for_file_system_available "${region}" "${file_system_id}"
    echo "${file_system_id}"
    return 0
  fi

  echo "Creating EFS file system '${name_tag}' in ${region}..." >&2
  file_system_id="$(aws efs create-file-system \
    --region "${region}" \
    --creation-token "${creation_token}" \
    --performance-mode generalPurpose \
    --throughput-mode bursting \
    --tags "Key=Name,Value=${name_tag}" "Key=environment,Value=${ENV_TAG_VALUE}" "Key=project,Value=${PROJECT_TAG_VALUE}" \
    --query 'FileSystemId' \
    --output text)"

  wait_for_file_system_available "${region}" "${file_system_id}"
  echo "${file_system_id}"
}

ensure_secondary_overwrite_protection_disabled() {
  local region="$1"
  local file_system_id="$2"
  local current_state

  current_state="$(aws efs describe-file-systems \
    --region "${region}" \
    --file-system-id "${file_system_id}" \
    --query 'FileSystems[0].FileSystemProtection.ReplicationOverwriteProtection' \
    --output text 2>/dev/null || true)"

  if [[ "${current_state}" == "DISABLED" || "${current_state}" == "REPLICATING" ]]; then
    echo "Replication overwrite protection already suitable on ${file_system_id}: ${current_state}"
    return 0
  fi

  echo "Disabling replication overwrite protection on secondary file system '${file_system_id}'..."
  aws efs update-file-system-protection \
    --region "${region}" \
    --file-system-id "${file_system_id}" \
    --replication-overwrite-protection DISABLED \
    >/dev/null

  wait_for_overwrite_protection "${region}" "${file_system_id}" "DISABLED"
}

get_replication_destination_id() {
  local region="$1"
  local source_file_system_id="$2"
  aws efs describe-replication-configurations \
    --region "${region}" \
    --file-system-id "${source_file_system_id}" \
    --query 'Replications[0].Destinations[0].FileSystemId' \
    --output text 2>/dev/null || true
}

get_replication_destination_region() {
  local region="$1"
  local source_file_system_id="$2"
  aws efs describe-replication-configurations \
    --region "${region}" \
    --file-system-id "${source_file_system_id}" \
    --query 'Replications[0].Destinations[0].Region' \
    --output text 2>/dev/null || true
}

wait_for_replication_ready() {
  local source_region="$1"
  local source_file_system_id="$2"
  local target_destination_id="$3"
  local deadline destination_id destination_region status
  deadline=$((SECONDS + 1800))

  while (( SECONDS < deadline )); do
    destination_id="$(aws efs describe-replication-configurations \
      --region "${source_region}" \
      --file-system-id "${source_file_system_id}" \
      --query 'Replications[0].Destinations[0].FileSystemId' \
      --output text 2>/dev/null || true)"
    destination_region="$(aws efs describe-replication-configurations \
      --region "${source_region}" \
      --file-system-id "${source_file_system_id}" \
      --query 'Replications[0].Destinations[0].Region' \
      --output text 2>/dev/null || true)"
    status="$(aws efs describe-replication-configurations \
      --region "${source_region}" \
      --file-system-id "${source_file_system_id}" \
      --query 'Replications[0].Destinations[0].Status' \
      --output text 2>/dev/null || true)"

    if [[ "${destination_id}" == "${target_destination_id}" && "${destination_region}" == "${SECONDARY_REGION}" && "${status}" == "ENABLED" ]]; then
      return 0
    fi
    if [[ "${status}" == "ERROR" || "${status}" == "PAUSED" ]]; then
      echo "ERROR: EFS replication entered state '${status}' while waiting to become ready." >&2
      exit 1
    fi
    sleep 15
  done

  echo "ERROR: Timed out waiting for EFS replication ${source_file_system_id} -> ${target_destination_id} to become ENABLED." >&2
  exit 1
}

ensure_replication() {
  local source_region="$1"
  local source_file_system_id="$2"
  local destination_region="$3"
  local destination_file_system_id="$4"
  local current_destination_id current_destination_region

  current_destination_id="$(get_replication_destination_id "${source_region}" "${source_file_system_id}")"
  current_destination_region="$(get_replication_destination_region "${source_region}" "${source_file_system_id}")"

  if [[ "${current_destination_id}" == "${destination_file_system_id}" && "${current_destination_region}" == "${destination_region}" ]]; then
    echo "EFS replication already exists: ${source_file_system_id} -> ${destination_file_system_id}"
    wait_for_replication_ready "${source_region}" "${source_file_system_id}" "${destination_file_system_id}"
    return 0
  fi

  if [[ -n "${current_destination_id}" && "${current_destination_id}" != "None" ]]; then
    echo "ERROR: Source file system '${source_file_system_id}' already replicates to '${current_destination_id}' in '${current_destination_region}'." >&2
    exit 1
  fi

  echo "Creating EFS replication ${source_region}:${source_file_system_id} -> ${destination_region}:${destination_file_system_id}..."
  aws efs create-replication-configuration \
    --region "${source_region}" \
    --source-file-system-id "${source_file_system_id}" \
    --destinations "[{\"Region\":\"${destination_region}\",\"FileSystemId\":\"${destination_file_system_id}\"}]" \
    >/dev/null

  wait_for_replication_ready "${source_region}" "${source_file_system_id}" "${destination_file_system_id}"
  wait_for_overwrite_protection "${destination_region}" "${destination_file_system_id}" "REPLICATING"
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

PRIMARY_NAME_TAG="$(build_name "${NAME}" "-primary" 128)"
SECONDARY_NAME_TAG="$(build_name "${NAME}" "-secondary" 128)"
PRIMARY_CREATION_TOKEN="$(build_name "${NAME}" "-primary" 64)"
SECONDARY_CREATION_TOKEN="$(build_name "${NAME}" "-secondary" 64)"

echo "Primary Region:          ${PRIMARY_REGION}"
echo "Secondary Region:        ${SECONDARY_REGION}"
echo "Base name:               ${NAME}"
echo "Primary Name tag:        ${PRIMARY_NAME_TAG}"
echo "Secondary Name tag:      ${SECONDARY_NAME_TAG}"
echo
echo "Creating or reusing EFS replication resources..."
echo

PRIMARY_FILE_SYSTEM_ID="$(ensure_file_system "${PRIMARY_REGION}" "${PRIMARY_NAME_TAG}" "${PRIMARY_CREATION_TOKEN}")"
SECONDARY_FILE_SYSTEM_ID="$(ensure_file_system "${SECONDARY_REGION}" "${SECONDARY_NAME_TAG}" "${SECONDARY_CREATION_TOKEN}")"

ensure_secondary_overwrite_protection_disabled "${SECONDARY_REGION}" "${SECONDARY_FILE_SYSTEM_ID}"
ensure_replication "${PRIMARY_REGION}" "${PRIMARY_FILE_SYSTEM_ID}" "${SECONDARY_REGION}" "${SECONDARY_FILE_SYSTEM_ID}"
write_state

echo
echo "EFS replication stack is ready."
echo "Primary file system:    ${PRIMARY_FILE_SYSTEM_ID}"
echo "Secondary file system:  ${SECONDARY_FILE_SYSTEM_ID}"
echo "State file:             ${STATE_FILE}"
