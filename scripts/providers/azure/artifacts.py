from __future__ import annotations

from typing import Any, Dict, Optional


def artifact_entry(
    artifact_type: str,
    *,
    local_path: Optional[str] = None,
    content_json: Optional[Dict[str, Any]] = None,
    object_url: Optional[str] = None,
) -> Dict[str, Any]:
    return {
        "artifact_type": artifact_type,
        "local_path": local_path,
        "content_json": content_json,
        "object_url": object_url,
    }
