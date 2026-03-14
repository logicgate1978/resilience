from __future__ import annotations

from .base import ServiceTemplateGenerator


class EFSTemplateGenerator(ServiceTemplateGenerator):
    service_name = "efs"
    action_map = {}
    target_spec_map = {}
