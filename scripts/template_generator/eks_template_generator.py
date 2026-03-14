from __future__ import annotations

from .base import ServiceTemplateGenerator


class EKSTemplateGenerator(ServiceTemplateGenerator):
    service_name = "eks"
    action_map = {}
    target_spec_map = {}
