from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from utility import normalize_service_name, parse_tags


class ValidationError(ValueError):
    pass


@dataclass
class ValidationContext:
    manifest: Dict[str, Any]
    service: Dict[str, Any]
    session: Any
    region: Optional[str]
    zone: Optional[str] = None
    _selected_resource_arns: Optional[List[str]] = field(default=None, init=False, repr=False)

    @property
    def service_name(self) -> str:
        return normalize_service_name(self.service.get("name"))

    @property
    def action(self) -> str:
        return str(self.service.get("action") or "").strip().lower()

    @property
    def action_key(self) -> str:
        return f"{self.service_name}:{self.action}"

    def get_selected_resource_arns(self) -> List[str]:
        if self._selected_resource_arns is None:
            from resource import collect_service_resource_arns

            self._selected_resource_arns = collect_service_resource_arns(
                self.service,
                session=self.session,
                region=self.region,
                zone=self.zone,
            )
        return self._selected_resource_arns

    def selection_summary(self) -> str:
        identifier = str(self.service.get("identifier") or "").strip()
        if identifier:
            tags = parse_tags(self.service.get("tags"))
            if tags:
                tag_text = ",".join(f"{k}={v}" for k, v in sorted(tags.items()))
                return f"identifier: {identifier}; tags: {tag_text}"
            return f"identifier: {identifier}"
        tags = parse_tags(self.service.get("tags"))
        if tags:
            tag_text = ",".join(f"{k}={v}" for k, v in sorted(tags.items()))
            return f"tags: {tag_text}"
        target = self.service.get("target")
        if isinstance(target, dict) and target:
            cluster_identifier = target.get("cluster_identifier")
            if cluster_identifier:
                return f"target.cluster_identifier: {cluster_identifier}"
        iam_roles = self.service.get("iam_roles")
        if iam_roles:
            return f"iam_roles: {iam_roles}"
        iam_role_arns = self.service.get("iam_role_arns")
        if iam_role_arns:
            return f"iam_role_arns: {iam_role_arns}"
        return "no explicit selector provided"


class BaseServiceValidator:
    service_name = ""

    def run(self, validation_name: str, context: ValidationContext) -> None:
        fn = getattr(self, validation_name, None)
        if not callable(fn):
            raise ValidationError(
                f"Validation '{validation_name}' is not implemented for service '{context.service_name}'."
            )
        fn(context)

    def fail(self, context: ValidationContext, message: str) -> None:
        raise ValidationError(f"Validation failed for {context.action_key}: {message}")
