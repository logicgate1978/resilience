"""Backward-compatible import wrapper for the AWS FIS engine."""

from providers.aws.engines.fis import create_template, generate_template_payload

__all__ = ["create_template", "generate_template_payload"]
