from __future__ import annotations

from typing import Any


def create_default_credential() -> Any:
    try:
        from azure.identity import DefaultAzureCredential
    except ImportError as exc:
        raise RuntimeError(
            "Azure authentication requires the 'azure-identity' package. "
            "Install scripts/requirements.txt before enabling Azure execution."
        ) from exc
    return DefaultAzureCredential(exclude_interactive_browser_credential=True)


def credential_summary() -> str:
    return "DefaultAzureCredential"
