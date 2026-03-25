#!/usr/bin/env bash

set -euo pipefail

DEFAULT_REGION="ap-southeast-1"
DEFAULT_STACK_NAME="resilience-asg-web"
DEFAULT_INSTANCE_TYPE="t3.micro"
DEFAULT_AMI_PARAM="/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-6.1-x86_64"
DEFAULT_HEALTH_CHECK_PATH="/"
ENV_TAG_VALUE="development"
PROJECT_TAG_VALUE="clouddash"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE_DIR="${SCRIPT_DIR}/.state"
STATE_FILE="${STATE_DIR}/current_asg_stack.txt"
ALB_DNS_FILE="${STATE_DIR}/current_asg_alb_dns.txt"

REGION="${DEFAULT_REGION}"
STACK_NAME="${DEFAULT_STACK_NAME}"
VPC_ID=""
MIN_SIZE=""
MAX_SIZE=""
DESIRED_CAPACITY=""

usage() {
  cat <<'EOF'
Usage:
  ./commands/ec2/create_asg_alb_stack.sh [--region <aws-region>] [--stack-name <name>] [--vpc-id <vpc-id>] [--min <count>] [--max <count>] [--desired <count>]

Defaults:
  region: ap-southeast-1
  stack-name: resilience-asg-web

Capacity rules:
  - If --min, --max, and --desired are all omitted, the script uses min=max=desired=1.
  - If any capacity argument is provided, --max becomes mandatory.
  - When omitted in that case:
      min defaults to 0
      desired defaults to max

Notes:
  - The script uses the default VPC and its default subnets unless --vpc-id is supplied.
  - It creates or updates:
      - launch template
      - Auto Scaling Group
      - internet-facing ALB
      - target group
      - security groups
  - No SSH access is opened.
EOF
}

require_command() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "ERROR: Required command not found: $1" >&2
    exit 1
  fi
}

ensure_integer() {
  local name="$1"
  local value="$2"
  if [[ ! "${value}" =~ ^[0-9]+$ ]]; then
    echo "ERROR: ${name} must be a non-negative integer." >&2
    exit 1
  fi
}

short_name() {
  local raw="$1"
  local max_len="$2"
  local normalized
  normalized="$(echo "${raw}" | tr '[:upper:]' '[:lower:]' | tr -cs 'a-z0-9-' '-')"
  normalized="${normalized#-}"
  normalized="${normalized%-}"
  echo "${normalized:0:${max_len}}"
}

write_state() {
  mkdir -p "${STATE_DIR}"
  cat > "${STATE_FILE}" <<EOF
REGION=${REGION}
STACK_NAME=${STACK_NAME}
VPC_ID=${VPC_ID}
ASG_NAME=${ASG_NAME}
LAUNCH_TEMPLATE_NAME=${LAUNCH_TEMPLATE_NAME}
ALB_NAME=${ALB_NAME}
ALB_ARN=${ALB_ARN}
ALB_SECURITY_GROUP_ID=${ALB_SECURITY_GROUP_ID}
TARGET_GROUP_NAME=${TARGET_GROUP_NAME}
TARGET_GROUP_ARN=${TARGET_GROUP_ARN}
EC2_SECURITY_GROUP_ID=${EC2_SECURITY_GROUP_ID}
ALB_DNS_NAME=${ALB_DNS_NAME}
EOF
  printf '%s\n' "${ALB_DNS_NAME}" > "${ALB_DNS_FILE}"
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

load_subnets() {
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

  if [[ "${#SUBNET_IDS[@]}" -lt 2 ]]; then
    echo "ERROR: ALB requires at least two subnets in different Availability Zones. Found ${#SUBNET_IDS[@]} subnet(s) in VPC '${VPC_ID}'." >&2
    exit 1
  fi

  SUBNET_CSV="$(IFS=,; echo "${SUBNET_IDS[*]}")"
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

  echo "${group_id}"
}

ensure_ingress_rule() {
  local mode="$1"
  shift
  local stderr_file
  stderr_file="$(mktemp)"

  if ! aws ec2 "${mode}" --region "${REGION}" "$@" 2>"${stderr_file}"; then
    if grep -q "InvalidPermission.Duplicate" "${stderr_file}"; then
      rm -f "${stderr_file}"
      return 0
    fi
    cat "${stderr_file}" >&2
    rm -f "${stderr_file}"
    return 1
  fi

  rm -f "${stderr_file}"
}

resolve_ami_id() {
  local ami_id
  ami_id="$(aws ssm get-parameter \
    --region "${REGION}" \
    --name "${DEFAULT_AMI_PARAM}" \
    --query 'Parameter.Value' \
    --output text)"

  if [[ -z "${ami_id}" || "${ami_id}" == "None" ]]; then
    echo "ERROR: Could not resolve Amazon Linux 2023 AMI from SSM parameter '${DEFAULT_AMI_PARAM}'." >&2
    exit 1
  fi

  echo "${ami_id}"
}

ensure_launch_template() {
  local launch_template_data_file="$1"
  local lt_id
  local lt_exists="false"

  lt_id="$(aws ec2 describe-launch-templates \
    --region "${REGION}" \
    --launch-template-names "${LAUNCH_TEMPLATE_NAME}" \
    --query 'LaunchTemplates[0].LaunchTemplateId' \
    --output text 2>/dev/null || true)"

  if [[ -n "${lt_id}" && "${lt_id}" != "None" ]]; then
    lt_exists="true"
  fi

  if [[ "${lt_exists}" == "true" ]]; then
    local latest_version new_version
    latest_version="$(aws ec2 describe-launch-templates \
      --region "${REGION}" \
      --launch-template-names "${LAUNCH_TEMPLATE_NAME}" \
      --query 'LaunchTemplates[0].LatestVersionNumber' \
      --output text)"

    new_version="$(aws ec2 create-launch-template-version \
      --region "${REGION}" \
      --launch-template-name "${LAUNCH_TEMPLATE_NAME}" \
      --source-version "${latest_version}" \
      --launch-template-data "file://${launch_template_data_file}" \
      --query 'LaunchTemplateVersion.VersionNumber' \
      --output text)"

    aws ec2 modify-launch-template \
      --region "${REGION}" \
      --launch-template-name "${LAUNCH_TEMPLATE_NAME}" \
      --default-version "${new_version}" \
      >/dev/null
  else
    aws ec2 create-launch-template \
      --region "${REGION}" \
      --launch-template-name "${LAUNCH_TEMPLATE_NAME}" \
      --launch-template-data "file://${launch_template_data_file}" \
      --tag-specifications "ResourceType=launch-template,Tags=[{Key=Name,Value=${LAUNCH_TEMPLATE_NAME}},{Key=environment,Value=${ENV_TAG_VALUE}},{Key=project,Value=${PROJECT_TAG_VALUE}}]" \
      >/dev/null
  fi

  LAUNCH_TEMPLATE_DEFAULT_VERSION="$(aws ec2 describe-launch-templates \
    --region "${REGION}" \
    --launch-template-names "${LAUNCH_TEMPLATE_NAME}" \
    --query 'LaunchTemplates[0].DefaultVersionNumber' \
    --output text)"
}

ensure_target_group() {
  local existing_tg_arn
  existing_tg_arn="$(aws elbv2 describe-target-groups \
    --region "${REGION}" \
    --names "${TARGET_GROUP_NAME}" \
    --query 'TargetGroups[0].TargetGroupArn' \
    --output text 2>/dev/null || true)"

  if [[ -n "${existing_tg_arn}" && "${existing_tg_arn}" != "None" ]]; then
    local existing_vpc
    existing_vpc="$(aws elbv2 describe-target-groups \
      --region "${REGION}" \
      --target-group-arns "${existing_tg_arn}" \
      --query 'TargetGroups[0].VpcId' \
      --output text)"
    if [[ "${existing_vpc}" != "${VPC_ID}" ]]; then
      echo "ERROR: Existing target group '${TARGET_GROUP_NAME}' belongs to VPC '${existing_vpc}', not '${VPC_ID}'." >&2
      exit 1
    fi
    TARGET_GROUP_ARN="${existing_tg_arn}"
    aws elbv2 modify-target-group \
      --region "${REGION}" \
      --target-group-arn "${TARGET_GROUP_ARN}" \
      --health-check-path "${DEFAULT_HEALTH_CHECK_PATH}" \
      --health-check-protocol HTTP \
      >/dev/null
  else
    TARGET_GROUP_ARN="$(aws elbv2 create-target-group \
      --region "${REGION}" \
      --name "${TARGET_GROUP_NAME}" \
      --protocol HTTP \
      --port 80 \
      --target-type instance \
      --vpc-id "${VPC_ID}" \
      --health-check-protocol HTTP \
      --health-check-path "${DEFAULT_HEALTH_CHECK_PATH}" \
      --tags "Key=Name,Value=${TARGET_GROUP_NAME}" "Key=environment,Value=${ENV_TAG_VALUE}" "Key=project,Value=${PROJECT_TAG_VALUE}" \
      --query 'TargetGroups[0].TargetGroupArn' \
      --output text)"
  fi

  aws elbv2 add-tags \
    --region "${REGION}" \
    --resource-arns "${TARGET_GROUP_ARN}" \
    --tags "Key=Name,Value=${TARGET_GROUP_NAME}" "Key=environment,Value=${ENV_TAG_VALUE}" "Key=project,Value=${PROJECT_TAG_VALUE}" \
    >/dev/null
}

ensure_load_balancer() {
  local existing_alb_arn
  existing_alb_arn="$(aws elbv2 describe-load-balancers \
    --region "${REGION}" \
    --names "${ALB_NAME}" \
    --query 'LoadBalancers[0].LoadBalancerArn' \
    --output text 2>/dev/null || true)"

  if [[ -n "${existing_alb_arn}" && "${existing_alb_arn}" != "None" ]]; then
    ALB_ARN="${existing_alb_arn}"
    aws elbv2 set-security-groups \
      --region "${REGION}" \
      --load-balancer-arn "${ALB_ARN}" \
      --security-groups "${ALB_SECURITY_GROUP_ID}" \
      >/dev/null
    aws elbv2 set-subnets \
      --region "${REGION}" \
      --load-balancer-arn "${ALB_ARN}" \
      --subnets "${SUBNET_IDS[@]}" \
      >/dev/null
  else
    ALB_ARN="$(aws elbv2 create-load-balancer \
      --region "${REGION}" \
      --name "${ALB_NAME}" \
      --type application \
      --scheme internet-facing \
      --subnets "${SUBNET_IDS[@]}" \
      --security-groups "${ALB_SECURITY_GROUP_ID}" \
      --tags "Key=Name,Value=${ALB_NAME}" "Key=environment,Value=${ENV_TAG_VALUE}" "Key=project,Value=${PROJECT_TAG_VALUE}" \
      --query 'LoadBalancers[0].LoadBalancerArn' \
      --output text)"
  fi

  aws elbv2 add-tags \
    --region "${REGION}" \
    --resource-arns "${ALB_ARN}" \
    --tags "Key=Name,Value=${ALB_NAME}" "Key=environment,Value=${ENV_TAG_VALUE}" "Key=project,Value=${PROJECT_TAG_VALUE}" \
    >/dev/null

  aws elbv2 wait load-balancer-available \
    --region "${REGION}" \
    --load-balancer-arns "${ALB_ARN}"

  ALB_DNS_NAME="$(aws elbv2 describe-load-balancers \
    --region "${REGION}" \
    --load-balancer-arns "${ALB_ARN}" \
    --query 'LoadBalancers[0].DNSName' \
    --output text)"

  mkdir -p "${STATE_DIR}"
  printf '%s\n' "${ALB_DNS_NAME}" > "${ALB_DNS_FILE}"
}

ensure_listener() {
  local listener_arn
  listener_arn="$(aws elbv2 describe-listeners \
    --region "${REGION}" \
    --load-balancer-arn "${ALB_ARN}" \
    --query 'Listeners[?Port==`80`].ListenerArn | [0]' \
    --output text 2>/dev/null || true)"

  if [[ -n "${listener_arn}" && "${listener_arn}" != "None" ]]; then
    aws elbv2 modify-listener \
      --region "${REGION}" \
      --listener-arn "${listener_arn}" \
      --default-actions "Type=forward,TargetGroupArn=${TARGET_GROUP_ARN}" \
      >/dev/null
  else
    aws elbv2 create-listener \
      --region "${REGION}" \
      --load-balancer-arn "${ALB_ARN}" \
      --protocol HTTP \
      --port 80 \
      --default-actions "Type=forward,TargetGroupArn=${TARGET_GROUP_ARN}" \
      >/dev/null
  fi
}

ensure_asg() {
  local existing_asg
  existing_asg="$(aws autoscaling describe-auto-scaling-groups \
    --region "${REGION}" \
    --auto-scaling-group-names "${ASG_NAME}" \
    --query 'AutoScalingGroups[0].AutoScalingGroupName' \
    --output text)"

  if [[ -n "${existing_asg}" && "${existing_asg}" != "None" ]]; then
    aws autoscaling update-auto-scaling-group \
      --region "${REGION}" \
      --auto-scaling-group-name "${ASG_NAME}" \
      --launch-template "LaunchTemplateName=${LAUNCH_TEMPLATE_NAME},Version=${LAUNCH_TEMPLATE_DEFAULT_VERSION}" \
      --min-size "${MIN_SIZE}" \
      --max-size "${MAX_SIZE}" \
      --desired-capacity "${DESIRED_CAPACITY}" \
      --vpc-zone-identifier "${SUBNET_CSV}" \
      --target-group-arns "${TARGET_GROUP_ARN}" \
      --health-check-type ELB \
      --health-check-grace-period 120 \
      >/dev/null
  else
    aws autoscaling create-auto-scaling-group \
      --region "${REGION}" \
      --auto-scaling-group-name "${ASG_NAME}" \
      --launch-template "LaunchTemplateName=${LAUNCH_TEMPLATE_NAME},Version=${LAUNCH_TEMPLATE_DEFAULT_VERSION}" \
      --min-size "${MIN_SIZE}" \
      --max-size "${MAX_SIZE}" \
      --desired-capacity "${DESIRED_CAPACITY}" \
      --vpc-zone-identifier "${SUBNET_CSV}" \
      --target-group-arns "${TARGET_GROUP_ARN}" \
      --health-check-type ELB \
      --health-check-grace-period 120 \
      --tags \
        "ResourceId=${ASG_NAME},ResourceType=auto-scaling-group,Key=Name,Value=${STACK_NAME}-instance,PropagateAtLaunch=true" \
        "ResourceId=${ASG_NAME},ResourceType=auto-scaling-group,Key=environment,Value=${ENV_TAG_VALUE},PropagateAtLaunch=true" \
        "ResourceId=${ASG_NAME},ResourceType=auto-scaling-group,Key=project,Value=${PROJECT_TAG_VALUE},PropagateAtLaunch=true" \
      >/dev/null
  fi

  aws autoscaling create-or-update-tags \
    --region "${REGION}" \
    --tags \
      "ResourceId=${ASG_NAME},ResourceType=auto-scaling-group,Key=Name,Value=${STACK_NAME}-instance,PropagateAtLaunch=true" \
      "ResourceId=${ASG_NAME},ResourceType=auto-scaling-group,Key=environment,Value=${ENV_TAG_VALUE},PropagateAtLaunch=true" \
      "ResourceId=${ASG_NAME},ResourceType=auto-scaling-group,Key=project,Value=${PROJECT_TAG_VALUE},PropagateAtLaunch=true" \
    >/dev/null
}

wait_for_asg_instances() {
  local deadline active_count healthy_targets
  deadline=$((SECONDS + 900))

  if [[ "${DESIRED_CAPACITY}" -eq 0 ]]; then
    return 0
  fi

  echo "Waiting for ${DESIRED_CAPACITY} instance(s) to become InService and healthy behind the ALB..."

  while (( SECONDS < deadline )); do
    active_count="$(aws autoscaling describe-auto-scaling-groups \
      --region "${REGION}" \
      --auto-scaling-group-names "${ASG_NAME}" \
      --query 'length(AutoScalingGroups[0].Instances[?LifecycleState==`InService` && HealthStatus==`Healthy`])' \
      --output text)"

    healthy_targets="$(aws elbv2 describe-target-health \
      --region "${REGION}" \
      --target-group-arn "${TARGET_GROUP_ARN}" \
      --query 'length(TargetHealthDescriptions[?TargetHealth.State==`healthy`])' \
      --output text)"

    if [[ "${active_count}" == "${DESIRED_CAPACITY}" && "${healthy_targets}" == "${DESIRED_CAPACITY}" ]]; then
      return 0
    fi

    sleep 15
  done

  echo "WARNING: Timed out waiting for the ASG and ALB targets to become fully healthy." >&2
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --region)
      REGION="${2:?Missing value for --region}"
      shift 2
      ;;
    --stack-name)
      STACK_NAME="${2:?Missing value for --stack-name}"
      shift 2
      ;;
    --vpc-id)
      VPC_ID="${2:?Missing value for --vpc-id}"
      shift 2
      ;;
    --min)
      MIN_SIZE="${2:?Missing value for --min}"
      shift 2
      ;;
    --max)
      MAX_SIZE="${2:?Missing value for --max}"
      shift 2
      ;;
    --desired)
      DESIRED_CAPACITY="${2:?Missing value for --desired}"
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

if [[ -z "${MIN_SIZE}" && -z "${MAX_SIZE}" && -z "${DESIRED_CAPACITY}" ]]; then
  MIN_SIZE="1"
  MAX_SIZE="1"
  DESIRED_CAPACITY="1"
else
  if [[ -z "${MAX_SIZE}" ]]; then
    echo "ERROR: --max is required when any capacity argument is provided." >&2
    exit 1
  fi
  MIN_SIZE="${MIN_SIZE:-0}"
  DESIRED_CAPACITY="${DESIRED_CAPACITY:-${MAX_SIZE}}"
fi

ensure_integer "min" "${MIN_SIZE}"
ensure_integer "max" "${MAX_SIZE}"
ensure_integer "desired" "${DESIRED_CAPACITY}"

if (( MIN_SIZE > MAX_SIZE )); then
  echo "ERROR: min cannot be greater than max." >&2
  exit 1
fi

if (( DESIRED_CAPACITY < MIN_SIZE || DESIRED_CAPACITY > MAX_SIZE )); then
  echo "ERROR: desired must be between min and max." >&2
  exit 1
fi

require_command aws
require_command base64

if [[ -z "${VPC_ID}" ]]; then
  VPC_ID="$(find_default_vpc)"
fi

load_subnets

ALB_NAME="$(short_name "${STACK_NAME}-alb" 32)"
TARGET_GROUP_NAME="$(short_name "${STACK_NAME}-tg" 32)"
ASG_NAME="${STACK_NAME}-asg"
LAUNCH_TEMPLATE_NAME="${STACK_NAME}-lt"
ALB_SECURITY_GROUP_NAME="${STACK_NAME}-alb-sg"
EC2_SECURITY_GROUP_NAME="${STACK_NAME}-ec2-sg"

ALB_SECURITY_GROUP_ID="$(ensure_security_group "${ALB_SECURITY_GROUP_NAME}" "ALB security group for ${STACK_NAME}")"
EC2_SECURITY_GROUP_ID="$(ensure_security_group "${EC2_SECURITY_GROUP_NAME}" "EC2 security group for ${STACK_NAME}")"

ensure_ingress_rule authorize-security-group-ingress \
  --group-id "${ALB_SECURITY_GROUP_ID}" \
  --ip-permissions "IpProtocol=tcp,FromPort=80,ToPort=80,IpRanges=[{CidrIp=0.0.0.0/0,Description=Public HTTP}]"

ensure_ingress_rule authorize-security-group-ingress \
  --group-id "${EC2_SECURITY_GROUP_ID}" \
  --ip-permissions "IpProtocol=tcp,FromPort=80,ToPort=80,UserIdGroupPairs=[{GroupId=${ALB_SECURITY_GROUP_ID},Description=HTTP from ALB}]"

AMI_ID="$(resolve_ami_id)"
USER_DATA_B64="$(cat <<'EOF' | base64 | tr -d '\n'
#!/bin/bash
set -euxo pipefail
dnf install -y nginx
cat > /usr/share/nginx/html/index.html <<HTML
<!doctype html>
<html>
  <head>
    <title>resilience-asg-web</title>
  </head>
  <body>
    <h1>resilience-asg-web</h1>
    <p>nginx started successfully</p>
  </body>
</html>
HTML
systemctl enable nginx
systemctl restart nginx
EOF
)"

LAUNCH_TEMPLATE_DATA_FILE="$(mktemp)"
cleanup() {
  rm -f "${LAUNCH_TEMPLATE_DATA_FILE}"
}
trap cleanup EXIT

cat > "${LAUNCH_TEMPLATE_DATA_FILE}" <<EOF
{
  "ImageId": "${AMI_ID}",
  "InstanceType": "${DEFAULT_INSTANCE_TYPE}",
  "SecurityGroupIds": ["${EC2_SECURITY_GROUP_ID}"],
  "UserData": "${USER_DATA_B64}",
  "MetadataOptions": {
    "HttpTokens": "required",
    "HttpEndpoint": "enabled"
  },
  "TagSpecifications": [
    {
      "ResourceType": "instance",
      "Tags": [
        {"Key": "Name", "Value": "${STACK_NAME}-instance"},
        {"Key": "environment", "Value": "${ENV_TAG_VALUE}"},
        {"Key": "project", "Value": "${PROJECT_TAG_VALUE}"}
      ]
    },
    {
      "ResourceType": "volume",
      "Tags": [
        {"Key": "environment", "Value": "${ENV_TAG_VALUE}"},
        {"Key": "project", "Value": "${PROJECT_TAG_VALUE}"}
      ]
    }
  ]
}
EOF

echo "Region:            ${REGION}"
echo "Stack name:        ${STACK_NAME}"
echo "VPC:               ${VPC_ID}"
echo "Subnets:           ${SUBNET_CSV}"
echo "Capacity:          min=${MIN_SIZE} max=${MAX_SIZE} desired=${DESIRED_CAPACITY}"
echo "Instance type:     ${DEFAULT_INSTANCE_TYPE}"
echo "AMI parameter:     ${DEFAULT_AMI_PARAM}"
echo
echo "Creating or updating launch template, security groups, target group, ALB, and ASG..."

ensure_launch_template "${LAUNCH_TEMPLATE_DATA_FILE}"
ensure_target_group
ensure_load_balancer
ensure_listener
ensure_asg
wait_for_asg_instances
write_state

echo
echo "ASG web stack is ready."
echo "ALB DNS name:      ${ALB_DNS_NAME}"
echo "ALB URL:           http://${ALB_DNS_NAME}"
echo "ASG name:          ${ASG_NAME}"
echo "Launch template:   ${LAUNCH_TEMPLATE_NAME}"
echo "Target group:      ${TARGET_GROUP_NAME}"
echo "State saved to:    ${STATE_FILE}"
echo "ALB DNS saved to:  ${ALB_DNS_FILE}"
