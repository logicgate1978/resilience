#!/usr/bin/env bash

set -euo pipefail

DEFAULT_REGION="ap-southeast-1"
DEFAULT_STACK_NAME="resilience-asg-web"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE_DIR="${SCRIPT_DIR}/.state"
STATE_FILE="${STATE_DIR}/current_asg_stack.txt"
ALB_DNS_FILE="${STATE_DIR}/current_asg_alb_dns.txt"

REGION="${DEFAULT_REGION}"
STACK_NAME="${DEFAULT_STACK_NAME}"
VPC_ID=""
ASG_NAME=""
LAUNCH_TEMPLATE_NAME=""
ALB_NAME=""
ALB_ARN=""
ALB_SECURITY_GROUP_ID=""
TARGET_GROUP_NAME=""
TARGET_GROUP_ARN=""
EC2_SECURITY_GROUP_ID=""
REGION_FROM_ARG="false"
STACK_NAME_FROM_ARG="false"

usage() {
  cat <<'EOF'
Usage:
  ./commands/ec2/destroy_asg_alb_stack.sh [--region <aws-region>] [--stack-name <name>]

Defaults:
  region: ap-southeast-1
  stack-name: resilience-asg-web

Notes:
  - If commands/ec2/.state/current_asg_stack.txt exists, the script reads resource names and IDs from it.
  - CLI arguments override the Region and stack name from the state file.
  - The script removes the ASG, ALB, target group, launch template, security groups, and local state files.
EOF
}

require_command() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "ERROR: Required command not found: $1" >&2
    exit 1
  fi
}

load_state() {
  if [[ -f "${STATE_FILE}" ]]; then
    # shellcheck disable=SC1090
    source "${STATE_FILE}"
  fi
}

ensure_name_defaults() {
  ASG_NAME="${ASG_NAME:-${STACK_NAME}-asg}"
  LAUNCH_TEMPLATE_NAME="${LAUNCH_TEMPLATE_NAME:-${STACK_NAME}-lt}"
  ALB_NAME="${ALB_NAME:-$(echo "${STACK_NAME}-alb" | tr '[:upper:]' '[:lower:]' | tr -cs 'a-z0-9-' '-' | sed 's/^-//; s/-$//' | cut -c1-32)}"
  TARGET_GROUP_NAME="${TARGET_GROUP_NAME:-$(echo "${STACK_NAME}-tg" | tr '[:upper:]' '[:lower:]' | tr -cs 'a-z0-9-' '-' | sed 's/^-//; s/-$//' | cut -c1-32)}"
}

lookup_alb_arn() {
  if [[ -n "${ALB_ARN}" ]]; then
    return 0
  fi
  ALB_ARN="$(aws elbv2 describe-load-balancers \
    --region "${REGION}" \
    --names "${ALB_NAME}" \
    --query 'LoadBalancers[0].LoadBalancerArn' \
    --output text 2>/dev/null || true)"
  if [[ "${ALB_ARN}" == "None" ]]; then
    ALB_ARN=""
  fi
}

lookup_target_group_arn() {
  if [[ -n "${TARGET_GROUP_ARN}" ]]; then
    return 0
  fi
  TARGET_GROUP_ARN="$(aws elbv2 describe-target-groups \
    --region "${REGION}" \
    --names "${TARGET_GROUP_NAME}" \
    --query 'TargetGroups[0].TargetGroupArn' \
    --output text 2>/dev/null || true)"
  if [[ "${TARGET_GROUP_ARN}" == "None" ]]; then
    TARGET_GROUP_ARN=""
  fi
}

lookup_security_group_id_by_name() {
  local group_name="$1"
  local group_id
  group_id="$(aws ec2 describe-security-groups \
    --region "${REGION}" \
    --filters Name=group-name,Values="${group_name}" \
    --query 'SecurityGroups[0].GroupId' \
    --output text 2>/dev/null || true)"
  if [[ "${group_id}" == "None" ]]; then
    group_id=""
  fi
  echo "${group_id}"
}

delete_asg() {
  local existing_asg
  existing_asg="$(aws autoscaling describe-auto-scaling-groups \
    --region "${REGION}" \
    --auto-scaling-group-names "${ASG_NAME}" \
    --query 'AutoScalingGroups[0].AutoScalingGroupName' \
    --output text)"

  if [[ -z "${existing_asg}" || "${existing_asg}" == "None" ]]; then
    echo "ASG not found: ${ASG_NAME}"
    return 0
  fi

  echo "Scaling ASG '${ASG_NAME}' down to zero..."
  aws autoscaling update-auto-scaling-group \
    --region "${REGION}" \
    --auto-scaling-group-name "${ASG_NAME}" \
    --min-size 0 \
    --max-size 0 \
    --desired-capacity 0 \
    >/dev/null

  echo "Deleting ASG '${ASG_NAME}'..."
  aws autoscaling delete-auto-scaling-group \
    --region "${REGION}" \
    --auto-scaling-group-name "${ASG_NAME}" \
    --force-delete \
    >/dev/null

  echo "Waiting for ASG deletion..."
  for _ in $(seq 1 40); do
    existing_asg="$(aws autoscaling describe-auto-scaling-groups \
      --region "${REGION}" \
      --auto-scaling-group-names "${ASG_NAME}" \
      --query 'AutoScalingGroups[0].AutoScalingGroupName' \
      --output text)"
    if [[ -z "${existing_asg}" || "${existing_asg}" == "None" ]]; then
      return 0
    fi
    sleep 15
  done

  echo "WARNING: Timed out waiting for ASG '${ASG_NAME}' to delete." >&2
}

delete_alb() {
  lookup_alb_arn
  if [[ -z "${ALB_ARN}" ]]; then
    echo "ALB not found: ${ALB_NAME}"
    return 0
  fi

  echo "Deleting ALB '${ALB_NAME}'..."
  aws elbv2 delete-load-balancer \
    --region "${REGION}" \
    --load-balancer-arn "${ALB_ARN}" \
    >/dev/null

  echo "Waiting for ALB deletion..."
  aws elbv2 wait load-balancers-deleted \
    --region "${REGION}" \
    --load-balancer-arns "${ALB_ARN}" || true
}

delete_target_group() {
  lookup_target_group_arn
  if [[ -z "${TARGET_GROUP_ARN}" ]]; then
    echo "Target group not found: ${TARGET_GROUP_NAME}"
    return 0
  fi

  echo "Deleting target group '${TARGET_GROUP_NAME}'..."
  for _ in $(seq 1 20); do
    if aws elbv2 delete-target-group \
      --region "${REGION}" \
      --target-group-arn "${TARGET_GROUP_ARN}" \
      >/dev/null 2>&1; then
      return 0
    fi
    sleep 10
  done

  echo "WARNING: Unable to delete target group '${TARGET_GROUP_NAME}'. It may still be referenced or draining." >&2
}

delete_launch_template() {
  local launch_template_id
  launch_template_id="$(aws ec2 describe-launch-templates \
    --region "${REGION}" \
    --launch-template-names "${LAUNCH_TEMPLATE_NAME}" \
    --query 'LaunchTemplates[0].LaunchTemplateId' \
    --output text 2>/dev/null || true)"

  if [[ -z "${launch_template_id}" || "${launch_template_id}" == "None" ]]; then
    echo "Launch template not found: ${LAUNCH_TEMPLATE_NAME}"
    return 0
  fi

  echo "Deleting launch template '${LAUNCH_TEMPLATE_NAME}'..."
  aws ec2 delete-launch-template \
    --region "${REGION}" \
    --launch-template-name "${LAUNCH_TEMPLATE_NAME}" \
    >/dev/null
}

delete_security_group() {
  local group_id="$1"
  local group_name="$2"

  if [[ -z "${group_id}" ]]; then
    group_id="$(lookup_security_group_id_by_name "${group_name}")"
  fi

  if [[ -z "${group_id}" ]]; then
    echo "Security group not found: ${group_name}"
    return 0
  fi

  echo "Deleting security group '${group_name}'..."
  for _ in $(seq 1 20); do
    if aws ec2 delete-security-group \
      --region "${REGION}" \
      --group-id "${group_id}" \
      >/dev/null 2>&1; then
      return 0
    fi
    sleep 10
  done

  echo "WARNING: Unable to delete security group '${group_name}'. It may still be attached to ENIs." >&2
}

remove_state_files() {
  rm -f "${STATE_FILE}" "${ALB_DNS_FILE}"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --region)
      REGION="${2:?Missing value for --region}"
      REGION_FROM_ARG="true"
      shift 2
      ;;
    --stack-name)
      STACK_NAME="${2:?Missing value for --stack-name}"
      STACK_NAME_FROM_ARG="true"
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

if [[ "${REGION_FROM_ARG}" != "true" ]]; then
  REGION="${REGION:-${DEFAULT_REGION}}"
fi

if [[ "${STACK_NAME_FROM_ARG}" != "true" ]]; then
  STACK_NAME="${STACK_NAME:-${DEFAULT_STACK_NAME}}"
  ASG_NAME="${ASG_NAME:-}"
  LAUNCH_TEMPLATE_NAME="${LAUNCH_TEMPLATE_NAME:-}"
  ALB_NAME="${ALB_NAME:-}"
  TARGET_GROUP_NAME="${TARGET_GROUP_NAME:-}"
else
  ASG_NAME=""
  LAUNCH_TEMPLATE_NAME=""
  ALB_NAME=""
  TARGET_GROUP_NAME=""
  ALB_ARN=""
  TARGET_GROUP_ARN=""
  ALB_SECURITY_GROUP_ID=""
  EC2_SECURITY_GROUP_ID=""
fi

ensure_name_defaults

ALB_SECURITY_GROUP_NAME="${STACK_NAME}-alb-sg"
EC2_SECURITY_GROUP_NAME="${STACK_NAME}-ec2-sg"

echo "Region:            ${REGION}"
echo "Stack name:        ${STACK_NAME}"
echo "ASG name:          ${ASG_NAME}"
echo "ALB name:          ${ALB_NAME}"
echo "Target group:      ${TARGET_GROUP_NAME}"
echo "Launch template:   ${LAUNCH_TEMPLATE_NAME}"
echo
echo "Destroying ASG web stack resources..."

delete_asg
delete_alb
delete_target_group
delete_launch_template
delete_security_group "${EC2_SECURITY_GROUP_ID}" "${EC2_SECURITY_GROUP_NAME}"
delete_security_group "${ALB_SECURITY_GROUP_ID}" "${ALB_SECURITY_GROUP_NAME}"
remove_state_files

echo
echo "ASG web stack cleanup finished."
echo "Removed state files: ${STATE_FILE} and ${ALB_DNS_FILE}"
