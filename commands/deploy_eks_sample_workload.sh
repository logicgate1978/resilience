#!/usr/bin/env bash

set -euo pipefail

DEFAULT_REGION="ap-southeast-1"
DEFAULT_NAMESPACE="default"
DEFAULT_APP_NAME="my-service"
DEFAULT_SERVICE_ACCOUNT="myserviceaccount"
DEFAULT_REPLICAS="2"
DEFAULT_IMAGE="public.ecr.aws/nginx/nginx:stable-alpine"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE_DIR="${SCRIPT_DIR}/.state"
STATE_FILE="${STATE_DIR}/last_eks_cluster.env"

REGION=""
CLUSTER_NAME=""
NAMESPACE="${DEFAULT_NAMESPACE}"
APP_NAME="${DEFAULT_APP_NAME}"
SERVICE_ACCOUNT="${DEFAULT_SERVICE_ACCOUNT}"
REPLICAS="${DEFAULT_REPLICAS}"
IMAGE="${DEFAULT_IMAGE}"
STATE_REGION=""
STATE_CLUSTER_NAME=""

usage() {
  cat <<'EOF'
Usage:
  ./commands/deploy_eks_sample_workload.sh [--name <cluster-name>] [--region <aws-region>] [--namespace <namespace>]
                                          [--app <app-name>] [--service-account <service-account>]
                                          [--replicas <count>] [--image <container-image>]

Defaults:
  region: ap-southeast-1
  namespace: default
  app: my-service
  service-account: myserviceaccount
  replicas: 2
  image: public.ecr.aws/nginx/nginx:stable-alpine

Behavior:
  - If --name is omitted, the script tries to read the last created cluster from commands/.state/last_eks_cluster.env
  - The deployment is labeled with app=<app-name> so it matches EKS pod-delete tests using labelSelector
  - The script also creates Role and RoleBinding objects for FIS eks:pod-delete
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
    --namespace)
      NAMESPACE="${2:?Missing value for --namespace}"
      shift 2
      ;;
    --app)
      APP_NAME="${2:?Missing value for --app}"
      shift 2
      ;;
    --service-account)
      SERVICE_ACCOUNT="${2:?Missing value for --service-account}"
      shift 2
      ;;
    --replicas)
      REPLICAS="${2:?Missing value for --replicas}"
      shift 2
      ;;
    --image)
      IMAGE="${2:?Missing value for --image}"
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

if ! [[ "${REPLICAS}" =~ ^[0-9]+$ ]] || [[ "${REPLICAS}" -lt 1 ]]; then
  echo "ERROR: --replicas must be a positive integer." >&2
  exit 1
fi

echo "Updating kubeconfig for cluster '${CLUSTER_NAME}' in region '${REGION}'..."
aws eks update-kubeconfig --region "${REGION}" --name "${CLUSTER_NAME}" >/dev/null

echo "Deploying sample workload '${APP_NAME}' to namespace '${NAMESPACE}'..."

cat <<EOF | kubectl apply -f -
apiVersion: v1
kind: Namespace
metadata:
  name: ${NAMESPACE}
---
apiVersion: v1
kind: ServiceAccount
metadata:
  name: ${SERVICE_ACCOUNT}
  namespace: ${NAMESPACE}
---
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: ${SERVICE_ACCOUNT}-fis-role
  namespace: ${NAMESPACE}
rules:
  - apiGroups: [""]
    resources: ["configmaps"]
    verbs: ["get", "create", "patch", "delete"]
  - apiGroups: [""]
    resources: ["pods"]
    verbs: ["create", "list", "get", "delete", "deletecollection"]
  - apiGroups: [""]
    resources: ["pods/ephemeralcontainers"]
    verbs: ["update"]
  - apiGroups: [""]
    resources: ["pods/exec"]
    verbs: ["create"]
  - apiGroups: ["apps"]
    resources: ["deployments"]
    verbs: ["get"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: ${SERVICE_ACCOUNT}-fis-binding
  namespace: ${NAMESPACE}
subjects:
  - kind: ServiceAccount
    name: ${SERVICE_ACCOUNT}
    namespace: ${NAMESPACE}
  - apiGroup: rbac.authorization.k8s.io
    kind: User
    name: fis-experiment
roleRef:
  kind: Role
  name: ${SERVICE_ACCOUNT}-fis-role
  apiGroup: rbac.authorization.k8s.io
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: ${APP_NAME}
  namespace: ${NAMESPACE}
  labels:
    app: ${APP_NAME}
spec:
  replicas: ${REPLICAS}
  selector:
    matchLabels:
      app: ${APP_NAME}
  template:
    metadata:
      labels:
        app: ${APP_NAME}
    spec:
      serviceAccountName: ${SERVICE_ACCOUNT}
      containers:
        - name: ${APP_NAME}
          image: ${IMAGE}
          ports:
            - containerPort: 80
          resources:
            requests:
              cpu: "100m"
              memory: "128Mi"
            limits:
              cpu: "200m"
              memory: "256Mi"
---
apiVersion: v1
kind: Service
metadata:
  name: ${APP_NAME}
  namespace: ${NAMESPACE}
spec:
  selector:
    app: ${APP_NAME}
  ports:
    - protocol: TCP
      port: 80
      targetPort: 80
  type: ClusterIP
EOF

kubectl rollout status deployment/"${APP_NAME}" -n "${NAMESPACE}" --timeout=300s

echo
echo "Sample workload deployed successfully."
echo "Cluster name:     ${CLUSTER_NAME}"
echo "Region:           ${REGION}"
echo "Namespace:        ${NAMESPACE}"
echo "Deployment name:  ${APP_NAME}"
echo "Service account:  ${SERVICE_ACCOUNT}"
echo "Replicas:         ${REPLICAS}"
echo
echo "This matches a manifest like:"
echo "  target.cluster_identifier: ${CLUSTER_NAME}"
echo "  target.namespace: ${NAMESPACE}"
echo "  target.selector_type: labelSelector"
echo "  target.selector_value: app=${APP_NAME}"
echo "  parameters.kubernetes_service_account: ${SERVICE_ACCOUNT}"
echo
echo "The script also created:"
echo "  Role: ${SERVICE_ACCOUNT}-fis-role"
echo "  RoleBinding: ${SERVICE_ACCOUNT}-fis-binding"
