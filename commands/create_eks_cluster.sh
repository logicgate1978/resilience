#!/usr/bin/env bash

set -euo pipefail

DEFAULT_REGION="ap-southeast-1"
DEFAULT_NODEGROUP_NAME="system-ng"
DEFAULT_INSTANCE_TYPE="t3.small"
DEFAULT_NODE_COUNT="1"
DEFAULT_VOLUME_SIZE="20"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE_DIR="${SCRIPT_DIR}/.state"
STATE_FILE="${STATE_DIR}/last_eks_cluster.env"

REGION="${DEFAULT_REGION}"
CLUSTER_NAME="resilience-eks-$(date +%Y%m%d%H%M%S)"
K8S_VERSION=""

usage() {
  cat <<'EOF'
Usage:
  ./commands/create_eks_cluster.sh [--region <aws-region>] [--name <cluster-name>] [--version <k8s-version>]

Defaults:
  region: ap-southeast-1
  name: resilience-eks-<timestamp>

Notes:
  - This script uses eksctl and aws CLI.
  - It creates a minimal managed node group with 1 x t3.small node.
  - NAT gateway creation is disabled to reduce cost.
EOF
}

require_command() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "ERROR: Required command not found: $1" >&2
    exit 1
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
    --version)
      K8S_VERSION="${2:?Missing value for --version}"
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
require_command eksctl

mkdir -p "${STATE_DIR}"

CONFIG_FILE="$(mktemp)"
cleanup() {
  rm -f "${CONFIG_FILE}"
}
trap cleanup EXIT

{
  cat <<EOF
apiVersion: eksctl.io/v1alpha5
kind: ClusterConfig

metadata:
  name: ${CLUSTER_NAME}
  region: ${REGION}
EOF

  if [[ -n "${K8S_VERSION}" ]]; then
    cat <<EOF
  version: "${K8S_VERSION}"
EOF
  fi

  cat <<EOF

vpc:
  nat:
    gateway: Disable
  clusterEndpoints:
    publicAccess: true
    privateAccess: false

managedNodeGroups:
  - name: ${DEFAULT_NODEGROUP_NAME}
    instanceType: ${DEFAULT_INSTANCE_TYPE}
    desiredCapacity: ${DEFAULT_NODE_COUNT}
    minSize: ${DEFAULT_NODE_COUNT}
    maxSize: ${DEFAULT_NODE_COUNT}
    volumeSize: ${DEFAULT_VOLUME_SIZE}
    privateNetworking: false
EOF
} > "${CONFIG_FILE}"

echo "Creating EKS cluster '${CLUSTER_NAME}' in region '${REGION}'..."
echo "This uses a single managed node (${DEFAULT_INSTANCE_TYPE}) and disables NAT gateways to minimize cost."

eksctl create cluster -f "${CONFIG_FILE}"

aws eks update-kubeconfig --region "${REGION}" --name "${CLUSTER_NAME}"

cat > "${STATE_FILE}" <<EOF
REGION=${REGION}
CLUSTER_NAME=${CLUSTER_NAME}
EOF

echo
echo "Cluster created successfully."
echo "Region:       ${REGION}"
echo "Cluster name: ${CLUSTER_NAME}"
echo "Kubeconfig updated for this cluster."
echo "State saved to: ${STATE_FILE}"
echo
echo "To destroy it later:"
echo "  ./commands/destroy_eks_cluster.sh --name ${CLUSTER_NAME} --region ${REGION}"
