import base64
import tempfile
from typing import Any, Dict
from urllib.parse import urlencode

from botocore.signers import RequestSigner
from kubernetes import client


def build_eks_bearer_token(session, region: str, cluster_name: str) -> str:
    credentials = session.get_credentials()
    if credentials is None:
        raise ValueError("No AWS credentials available for EKS Kubernetes API authentication.")
    sts_client = session.client("sts", region_name=region)
    service_id = sts_client.meta.service_model.service_id

    signer = RequestSigner(
        service_id,
        region,
        "sts",
        "v4",
        credentials,
        session._session.get_component("event_emitter"),
    )

    params = {
        "method": "GET",
        "url": f"https://sts.{region}.amazonaws.com/?{urlencode({'Action': 'GetCallerIdentity', 'Version': '2011-06-15'})}",
        "body": {},
        "headers": {"x-k8s-aws-id": cluster_name},
        "context": {},
    }
    presigned_url = signer.generate_presigned_url(
        request_dict=params,
        region_name=region,
        expires_in=60,
        operation_name="",
    )
    token = base64.urlsafe_b64encode(presigned_url.encode("utf-8")).decode("utf-8").rstrip("=")
    return f"k8s-aws-v1.{token}"


def get_eks_cluster_connection(session, region: str, cluster_name: str) -> Dict[str, Any]:
    eks = session.client("eks", region_name=region)
    resp = eks.describe_cluster(name=cluster_name)
    cluster = resp["cluster"]
    return {
        "name": cluster["name"],
        "endpoint": cluster["endpoint"],
        "certificate_authority_data": cluster["certificateAuthority"]["data"],
    }


def create_apps_v1_api(session, region: str, cluster_name: str):
    connection = get_eks_cluster_connection(session, region, cluster_name)
    token = build_eks_bearer_token(session, region, cluster_name)

    ca_data = base64.b64decode(connection["certificate_authority_data"])
    ca_file = tempfile.NamedTemporaryFile(prefix="eks-ca-", suffix=".crt", delete=False)
    ca_file.write(ca_data)
    ca_file.flush()
    ca_file.close()

    cfg = client.Configuration()
    cfg.host = connection["endpoint"]
    cfg.verify_ssl = True
    cfg.ssl_ca_cert = ca_file.name
    cfg.api_key = {"authorization": f"Bearer {token}"}

    api_client = client.ApiClient(configuration=cfg)
    return client.AppsV1Api(api_client)
