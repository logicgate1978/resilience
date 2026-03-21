#!/usr/bin/env bash

set -euo pipefail

DEFAULT_REGION="ap-southeast-1"
DEFAULT_USERNAME="fis-experiment"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
ENV_FILE="${REPO_ROOT}/.env"
STATE_FILE="${SCRIPT_DIR}/.state/last_eks_cluster.env"
EKS_ACCESS_POLICY_ARN="arn:aws:eks::aws:cluster-access-policy/AmazonEKSClusterAdminPolicy"
FIS_EKS_POLICY_ARN="arn:aws:iam::aws:policy/service-role/AWSFaultInjectionSimulatorEKSAccess"

REGION=""
CLUSTER_NAME=""
FIS_ROLE_ARN=""
USERNAME="${DEFAULT_USERNAME}"
STATE_REGION=""
STATE_CLUSTER_NAME=""

usage() {
  cat <<'EOF'
Usage:
  ./commands/grant_fis_eks_access.sh [--name <cluster-name>] [--region <aws-region>] [--fis-role-arn <role-arn>] [--username <k8s-username>]

Defaults:
  region: ap-southeast-1
  username: fis-experiment

Behavior:
  - Reads FIS_ROLE_ARN from repo-root .env if --fis-role-arn is omitted
  - Reads the last created cluster from commands/.state/last_eks_cluster.env if --name is omitted
  - Attaches AWSFaultInjectionSimulatorEKSAccess to the IAM role
  - Creates or verifies an EKS access entry for the role using username fis-experiment
  - Associates AmazonEKSClusterAdminPolicy to the role on the cluster
EOF
}

require_command() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "ERROR: Required command not found: $1" >&2
    exit 1
  fi
}

load_env_value() {
  local key="$1"
  if [[ ! -f "${ENV_FILE}" ]]; then
    return
  fi
  grep "^${key}=" "${ENV_FILE}" | cut -d'=' -f2- | tail -n 1 || true
}

load_state_if_present() {
  if [[ -f "${STATE_FILE}" ]]; then
    STATE_REGION="$(grep '^REGION=' "${STATE_FILE}" | cut -d'=' -f2- || true)"
    STATE_CLUSTER_NAME="$(grep '^CLUSTER_NAME=' "${STATE_FILE}" | cut -d'=' -f2- || true)"
  fi
}

role_name_from_arn() {
  local arn="$1"
  local prefix=":role/"
  local role_path
  if [[ "${arn}" != *"${prefix}"* ]]; then
    echo "ERROR: Invalid IAM role ARN: ${arn}" >&2
    exit 1
  fi
  role_path="${arn#*${prefix}}"
  echo "${role_path##*/}"
}

policy_attached() {
  local role_name="$1"
  local policy_arn="$2"
  aws iam list-attached-role-policies \
    --role-name "${role_name}" \
    --query "AttachedPolicies[?PolicyArn=='${policy_arn}'].PolicyArn" \
    --output text 2>/dev/null | grep -q "${policy_arn}"
}

access_entry_exists() {
  local cluster_name="$1"
  local principal_arn="$2"
  local region="$3"
  aws eks describe-access-entry \
    --cluster-name "${cluster_name}" \
    --principal-arn "${principal_arn}" \
    --region "${region}" >/dev/null 2>&1
}

access_policy_associated() {
  local cluster_name="$1"
  local principal_arn="$2"
  local region="$3"
  aws eks list-associated-access-policies \
    --cluster-name "${cluster_name}" \
    --principal-arn "${principal_arn}" \
    --region "${region}" \
    --query "associatedAccessPolicies[?policyArn=='${EKS_ACCESS_POLICY_ARN}'].policyArn" \
    --output text 2>/dev/null | grep -q "${EKS_ACCESS_POLICY_ARN}"
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
    --fis-role-arn)
      FIS_ROLE_ARN="${2:?Missing value for --fis-role-arn}"
      shift 2
      ;;
    --username)
      USERNAME="${2:?Missing value for --username}"
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

load_state_if_present

REGION="${REGION:-${STATE_REGION:-${DEFAULT_REGION}}}"
CLUSTER_NAME="${CLUSTER_NAME:-${STATE_CLUSTER_NAME:-}}"
FIS_ROLE_ARN="${FIS_ROLE_ARN:-$(load_env_value FIS_ROLE_ARN)}"

if [[ -z "${CLUSTER_NAME}" ]]; then
  echo "ERROR: Cluster name is required. Pass --name or create the cluster first so state is available." >&2
  exit 1
fi

if [[ -z "${FIS_ROLE_ARN}" ]]; then
  echo "ERROR: FIS_ROLE_ARN is required. Add it to .env or pass --fis-role-arn." >&2
  exit 1
fi

ROLE_NAME="$(role_name_from_arn "${FIS_ROLE_ARN}")"

echo "Cluster name:  ${CLUSTER_NAME}"
echo "Region:        ${REGION}"
echo "FIS role ARN:  ${FIS_ROLE_ARN}"
echo "IAM role name: ${ROLE_NAME}"
echo "K8s username:  ${USERNAME}"
echo

if policy_attached "${ROLE_NAME}" "${FIS_EKS_POLICY_ARN}"; then
  echo "AWSFaultInjectionSimulatorEKSAccess is already attached. Skipping."
else
  echo "Attaching AWSFaultInjectionSimulatorEKSAccess to ${ROLE_NAME}..."
  aws iam attach-role-policy \
    --role-name "${ROLE_NAME}" \
    --policy-arn "${FIS_EKS_POLICY_ARN}"
fi

if access_entry_exists "${CLUSTER_NAME}" "${FIS_ROLE_ARN}" "${REGION}"; then
  echo "EKS access entry already exists. Skipping create-access-entry."
else
  echo "Creating EKS access entry..."
  aws eks create-access-entry \
    --cluster-name "${CLUSTER_NAME}" \
    --principal-arn "${FIS_ROLE_ARN}" \
    --username "${USERNAME}" \
    --region "${REGION}"
fi

if access_policy_associated "${CLUSTER_NAME}" "${FIS_ROLE_ARN}" "${REGION}"; then
  echo "AmazonEKSClusterAdminPolicy is already associated. Skipping associate-access-policy."
else
  echo "Associating AmazonEKSClusterAdminPolicy..."
  aws eks associate-access-policy \
    --cluster-name "${CLUSTER_NAME}" \
    --principal-arn "${FIS_ROLE_ARN}" \
    --policy-arn "${EKS_ACCESS_POLICY_ARN}" \
    --access-scope type=cluster \
    --region "${REGION}"
fi

echo
echo "FIS EKS access setup completed."
echo "If your cluster uses legacy aws-auth instead of EKS access entries, you may need a different mapping approach."
