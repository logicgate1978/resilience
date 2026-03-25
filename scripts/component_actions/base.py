from abc import ABC, abstractmethod
from typing import Any, ClassVar, Dict, List, Optional, Type

from utility import normalize_service_name


class CustomComponentAction(ABC):
    registry: ClassVar[List[Type["CustomComponentAction"]]] = []
    service_name: ClassVar[Optional[str]] = None
    action_names: ClassVar[List[str]] = []

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        if cls.service_name and cls.action_names:
            CustomComponentAction.registry.append(cls)

    def supports(self, service_name: str, action: str) -> bool:
        return normalize_service_name(service_name) == self.service_name and action in self.action_names

    @abstractmethod
    def build_plan_item(
        self,
        *,
        manifest: Dict[str, Any],
        svc: Dict[str, Any],
        session,
        region: str,
        index: int,
        default_timeout_seconds: int,
    ) -> Dict[str, Any]:
        raise NotImplementedError

    @abstractmethod
    def execute_item(
        self,
        *,
        session,
        item: Dict[str, Any],
        poll_seconds: int,
        timeout_seconds: int,
    ) -> Dict[str, Any]:
        raise NotImplementedError
