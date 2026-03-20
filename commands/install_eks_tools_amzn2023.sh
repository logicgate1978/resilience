#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TMP_BASE="${TMPDIR:-/var/tmp}"
TMP_DIR="$(mktemp -d -p "${TMP_BASE}")"

cleanup() {
  rm -rf "${TMP_DIR}"
}
trap cleanup EXIT

require_command() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "ERROR: Required command not found: $1" >&2
    exit 1
  fi
}

download_file() {
  local url="$1"
  local output="$2"
  curl -fL --retry 5 --retry-delay 2 --retry-connrefused -o "${output}" "${url}"
}

ensure_curl() {
  if command -v curl >/dev/null 2>&1; then
    return
  fi
  echo "Installing curl-minimal..."
  sudo dnf install -y curl-minimal
}

detect_arch() {
  local machine
  machine="$(uname -m)"
  case "${machine}" in
    x86_64)
      EKSCTL_PLATFORM="Linux_amd64"
      AWSCLI_ARCH="x86_64"
      KUBECTL_ARCH="amd64"
      ;;
    aarch64|arm64)
      EKSCTL_PLATFORM="Linux_arm64"
      AWSCLI_ARCH="aarch64"
      KUBECTL_ARCH="arm64"
      ;;
    *)
      echo "ERROR: Unsupported architecture: ${machine}" >&2
      exit 1
      ;;
  esac
}

install_os_packages() {
  echo "Installing base OS packages..."
  sudo dnf install -y unzip tar gzip
}

install_aws_cli() {
  if command -v aws >/dev/null 2>&1; then
    echo "AWS CLI already installed. Skipping."
    return
  fi
  echo "Installing AWS CLI..."
  download_file "https://awscli.amazonaws.com/awscli-exe-linux-${AWSCLI_ARCH}.zip" "${TMP_DIR}/awscliv2.zip"
  unzip -q "${TMP_DIR}/awscliv2.zip" -d "${TMP_DIR}"
  sudo "${TMP_DIR}/aws/install" --update
}

install_kubectl() {
  if command -v kubectl >/dev/null 2>&1; then
    echo "kubectl already installed. Skipping."
    return
  fi
  local stable_version
  stable_version="$(curl -fsSL https://dl.k8s.io/release/stable.txt)"

  echo "Installing kubectl ${stable_version}..."
  download_file "https://dl.k8s.io/release/${stable_version}/bin/linux/${KUBECTL_ARCH}/kubectl" "${TMP_DIR}/kubectl"
  chmod +x "${TMP_DIR}/kubectl"
  sudo install -m 0755 "${TMP_DIR}/kubectl" /usr/local/bin/kubectl
}

install_eksctl() {
  if command -v eksctl >/dev/null 2>&1; then
    echo "eksctl already installed. Skipping."
    return
  fi
  echo "Installing eksctl..."
  download_file "https://github.com/eksctl-io/eksctl/releases/latest/download/eksctl_${EKSCTL_PLATFORM}.tar.gz" "${TMP_DIR}/eksctl.tar.gz"
  gzip -t "${TMP_DIR}/eksctl.tar.gz"
  tar -xzf "${TMP_DIR}/eksctl.tar.gz" -C "${TMP_DIR}" eksctl
  chmod +x "${TMP_DIR}/eksctl"
  sudo install -m 0755 "${TMP_DIR}/eksctl" /usr/local/bin/eksctl
}

print_versions() {
  echo
  echo "Installed tool versions:"
  if command -v aws >/dev/null 2>&1; then
    aws --version
  fi
  if command -v kubectl >/dev/null 2>&1; then
    kubectl version --client --output=yaml | sed -n '1,12p'
  fi
  if command -v eksctl >/dev/null 2>&1; then
    eksctl version
  fi
}

main() {
  require_command sudo
  require_command dnf
  require_command uname

  install_os_packages
  ensure_curl
  detect_arch
  install_aws_cli
  install_kubectl
  install_eksctl
  print_versions

  echo
  echo "EKS tooling setup completed."
  echo "You can now use:"
  echo "  ${SCRIPT_DIR}/create_eks_cluster.sh"
  echo "  ${SCRIPT_DIR}/deploy_eks_sample_workload.sh"
  echo "  ${SCRIPT_DIR}/destroy_eks_cluster.sh"
}

main "$@"
