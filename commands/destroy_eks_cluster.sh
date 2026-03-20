#!/usr/bin/env bash

set -euo pipefail

DEFAULT_REGION="ap-southeast-1"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE_DIR="${SCRIPT_DIR}/.state"
STATE_FILE="${STATE_DIR}/last_eks_cluster.env"

REGION=""
CLUSTER_NAME=""
STATE_REGION=""
STATE_CLUSTER_NAME=""

usage() {
  cat <<'EOF'
Usage:
  ./commands/destroy_eks_cluster.sh [--name <cluster-name>] [--region <aws-region>]

Defaults:
  region: ap-southeast-1

Behavior:
  - If --name is omitted, the script tries to read the last created cluster from commands/.state/last_eks_cluster.env
EOF
}

require_command() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "ERROR: Required command not found: $1" >&2
    exit 1
  fi
}

load_state_if_present() {
  if [[ -f "${STATE_FILE}" ]]; then
    STATE_REGION="$(grep '^REGION=' "${STATE_FILE}" | cut -d'=' -f2- || true)"
    STATE_CLUSTER_NAME="$(grep '^CLUSTER_NAME=' "${STATE_FILE}" | cut -d'=' -f2- || true)"
  fi
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --region)
      REGION="${2:?Missing value for --region}"
      shift 2
      ;;
    --name)
      CLUSTER_NAME="${2:?Missing value for --name}"
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

require_command eksctl

load_state_if_present

REGION="${REGION:-${STATE_REGION:-${DEFAULT_REGION}}}"
CLUSTER_NAME="${CLUSTER_NAME:-${STATE_CLUSTER_NAME:-}}"

if [[ -z "${CLUSTER_NAME}" ]]; then
  echo "ERROR: Cluster name is required. Pass --name or create the cluster first so state is available." >&2
  exit 1
fi

echo "Deleting EKS cluster '${CLUSTER_NAME}' in region '${REGION}'..."
eksctl delete cluster --name "${CLUSTER_NAME}" --region "${REGION}"

if [[ -f "${STATE_FILE}" ]]; then
  CURRENT_STATE_CLUSTER="$(grep '^CLUSTER_NAME=' "${STATE_FILE}" | cut -d'=' -f2- || true)"
  if [[ "${CURRENT_STATE_CLUSTER}" == "${CLUSTER_NAME}" ]]; then
    rm -f "${STATE_FILE}"
  fi
fi

echo "Cluster deleted successfully."
