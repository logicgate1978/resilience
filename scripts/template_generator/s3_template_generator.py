from __future__ import annotations

from .base import ServiceTemplateGenerator


class S3TemplateGenerator(ServiceTemplateGenerator):
    service_name = "s3"
    action_map = {}
    target_spec_map = {}
