#!/usr/bin/env bash

set -euo pipefail

DEFAULT_PRIMARY_REGION="ap-southeast-1"
DEFAULT_SECONDARY_REGION="ap-southeast-2"
DEFAULT_NAME="resilience-efs-replication"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE_DIR="${SCRIPT_DIR}/.state"
STATE_FILE="${STATE_DIR}/current_efs_replication_stack.txt"

PRIMARY_REGION="${DEFAULT_PRIMARY_REGION}"
SECONDARY_REGION="${DEFAULT_SECONDARY_REGION}"
NAME="${DEFAULT_NAME}"

PRIMARY_NAME_TAG=""
SECONDARY_NAME_TAG=""
PRIMARY_FILE_SYSTEM_ID=""
SECONDARY_FILE_SYSTEM_ID=""

usage() {
  cat <<'EOF'
Usage:
  ./commands/efs/destroy_efs_replication_stack.sh [--name <base-name>] [--primary-region <aws-region>] [--secondary-region <aws-region>]

Notes:
  - If commands/efs/.state/current_efs_replication_stack.txt exists, the script reads file system IDs from it.
  - CLI arguments override the state file values when supplied.
  - The script deletes the replication configuration first, waits for it to be removed, then deletes both file systems and the local state file.
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

load_state() {
  if [[ -f "${STATE_FILE}" ]]; then
    # shellcheck disable=SC1090
    source "${STATE_FILE}"
  fi
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
  local ids file_system_id actual_name

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
    if [[ "${actual_name}" == "${name_tag}" ]]; then
      echo "${file_system_id}"
      return 0
    fi
  done

  return 1
}

wait_for_replication_deleted() {
  local source_region="$1"
  local source_file_system_id="$2"
  local deadline replications
  deadline=$((SECONDS + 1200))

  while (( SECONDS < deadline )); do
    replications="$(aws efs describe-replication-configurations \
      --region "${source_region}" \
      --file-system-id "${source_file_system_id}" \
      --query 'length(Replications)' \
      --output text 2>/dev/null || true)"
    if [[ "${replications}" == "0" || -z "${replications}" || "${replications}" == "None" ]]; then
      return 0
    fi
    sleep 10
  done

  echo "WARNING: Timed out waiting for replication to be deleted from '${source_file_system_id}'." >&2
  return 1
}

wait_for_file_system_deleted() {
  local region="$1"
  local file_system_id="$2"
  local deadline
  deadline=$((SECONDS + 1200))

  while (( SECONDS < deadline )); do
    if ! file_system_exists "${region}" "${file_system_id}"; then
      return 0
    fi
    sleep 10
  done

  echo "WARNING: Timed out waiting for EFS file system '${file_system_id}' in ${region} to be deleted." >&2
  return 1
}

delete_replication_if_present() {
  local source_region="$1"
  local source_file_system_id="$2"

  if ! file_system_exists "${source_region}" "${source_file_system_id}"; then
    echo "Primary file system not found: ${source_file_system_id} (${source_region})"
    return 0
  fi

  local replications
  replications="$(aws efs describe-replication-configurations \
    --region "${source_region}" \
    --file-system-id "${source_file_system_id}" \
    --query 'length(Replications)' \
    --output text 2>/dev/null || true)"

  if [[ "${replications}" == "0" || -z "${replications}" || "${replications}" == "None" ]]; then
    echo "No replication configuration found on primary file system '${source_file_system_id}'."
    return 0
  fi

  echo "Deleting replication configuration from '${source_file_system_id}'..."
  aws efs delete-replication-configuration \
    --region "${source_region}" \
    --source-file-system-id "${source_file_system_id}" \
    >/dev/null

  wait_for_replication_deleted "${source_region}" "${source_file_system_id}" || true
}

delete_file_system_if_present() {
  local region="$1"
  local file_system_id="$2"

  if [[ -z "${file_system_id}" ]]; then
    return 0
  fi
  if ! file_system_exists "${region}" "${file_system_id}"; then
    echo "File system not found: ${file_system_id} (${region})"
    return 0
  fi

  echo "Deleting EFS file system '${file_system_id}' in ${region}..."
  aws efs delete-file-system \
    --region "${region}" \
    --file-system-id "${file_system_id}" \
    >/dev/null

  wait_for_file_system_deleted "${region}" "${file_system_id}" || true
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
load_state

if [[ -z "${PRIMARY_NAME_TAG}" ]]; then
  PRIMARY_NAME_TAG="$(build_name "${NAME}" "-primary" 128)"
fi
if [[ -z "${SECONDARY_NAME_TAG}" ]]; then
  SECONDARY_NAME_TAG="$(build_name "${NAME}" "-secondary" 128)"
fi

if [[ -z "${PRIMARY_FILE_SYSTEM_ID}" || "${PRIMARY_FILE_SYSTEM_ID}" == "None" ]]; then
  PRIMARY_FILE_SYSTEM_ID="$(find_file_system_by_name_tag "${PRIMARY_REGION}" "${PRIMARY_NAME_TAG}" || true)"
fi
if [[ -z "${SECONDARY_FILE_SYSTEM_ID}" || "${SECONDARY_FILE_SYSTEM_ID}" == "None" ]]; then
  SECONDARY_FILE_SYSTEM_ID="$(find_file_system_by_name_tag "${SECONDARY_REGION}" "${SECONDARY_NAME_TAG}" || true)"
fi

echo "Primary Region:          ${PRIMARY_REGION}"
echo "Secondary Region:        ${SECONDARY_REGION}"
echo "Base name:               ${NAME}"
echo "Primary file system:     ${PRIMARY_FILE_SYSTEM_ID:-<not found>}"
echo "Secondary file system:   ${SECONDARY_FILE_SYSTEM_ID:-<not found>}"
echo
echo "Destroying EFS replication resources..."
echo

if [[ -n "${PRIMARY_FILE_SYSTEM_ID}" ]]; then
  delete_replication_if_present "${PRIMARY_REGION}" "${PRIMARY_FILE_SYSTEM_ID}"
fi
if [[ -n "${SECONDARY_FILE_SYSTEM_ID}" ]]; then
  delete_file_system_if_present "${SECONDARY_REGION}" "${SECONDARY_FILE_SYSTEM_ID}"
fi
if [[ -n "${PRIMARY_FILE_SYSTEM_ID}" ]]; then
  delete_file_system_if_present "${PRIMARY_REGION}" "${PRIMARY_FILE_SYSTEM_ID}"
fi

rm -f "${STATE_FILE}"

echo
echo "EFS replication stack has been removed."
