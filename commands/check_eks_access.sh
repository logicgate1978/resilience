#!/usr/bin/env bash

set -euo pipefail

DEFAULT_REGION="ap-southeast-1"
DEFAULT_NAMESPACE="default"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE_DIR="${SCRIPT_DIR}/.state"
STATE_FILE="${STATE_DIR}/last_eks_cluster.env"

REGION=""
CLUSTER_NAME=""
NAMESPACE="${DEFAULT_NAMESPACE}"
STATE_REGION=""
STATE_CLUSTER_NAME=""

usage() {
  cat <<'EOF'
Usage:
  ./commands/check_eks_access.sh [--name <cluster-name>] [--region <aws-region>] [--namespace <namespace>]

Defaults:
  region: ap-southeast-1
  namespace: default

Behavior:
  - If --name is omitted, the script tries to read the last created cluster from commands/.state/last_eks_cluster.env
  - It prints the current AWS identity, kube context, and selected Kubernetes RBAC checks
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

print_can_i() {
  local verb="$1"
  local resource="$2"
  local namespace_flag=()

  if [[ -n "${NAMESPACE}" ]]; then
    namespace_flag=(-n "${NAMESPACE}")
  fi

  if kubectl auth can-i "${verb}" "${resource}" "${namespace_flag[@]}" >/dev/null 2>&1; then
    local result
    result="$(kubectl auth can-i "${verb}" "${resource}" "${namespace_flag[@]}" || true)"
    printf "  %-8s %-22s %s\n" "${verb}" "${resource}" "${result}"
  else
    printf "  %-8s %-22s %s\n" "${verb}" "${resource}" "error"
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
    --namespace)
      NAMESPACE="${2:?Missing value for --namespace}"
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
require_command kubectl

load_state_if_present

REGION="${REGION:-${STATE_REGION:-${DEFAULT_REGION}}}"
CLUSTER_NAME="${CLUSTER_NAME:-${STATE_CLUSTER_NAME:-}}"

if [[ -z "${CLUSTER_NAME}" ]]; then
  echo "ERROR: Cluster name is required. Pass --name or create the cluster first so state is available." >&2
  exit 1
fi

echo "Updating kubeconfig for cluster '${CLUSTER_NAME}' in region '${REGION}'..."
aws eks update-kubeconfig --region "${REGION}" --name "${CLUSTER_NAME}" >/dev/null

echo
echo "AWS identity:"
aws sts get-caller-identity

echo
echo "Kubernetes context:"
echo "  current-context: $(kubectl config current-context)"
echo "  namespace:       ${NAMESPACE}"

echo
echo "Cluster reachability:"
kubectl cluster-info

echo
echo "RBAC checks:"
print_can_i get pods
print_can_i list pods
print_can_i create pods
print_can_i delete pods
print_can_i create role
print_can_i create rolebinding
print_can_i get deployments.apps

echo
echo "Namespace objects:"
kubectl get sa -n "${NAMESPACE}" || true
kubectl get role -n "${NAMESPACE}" || true
kubectl get rolebinding -n "${NAMESPACE}" || true
