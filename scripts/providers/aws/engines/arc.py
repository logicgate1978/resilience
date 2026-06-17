"""AWS ARC region switch engine facade."""

from region_switch import (
    build_execution_plan,
    execute_region_plan,
    resolve_region_targets,
    validate_region_manifest,
)

__all__ = [
    "build_execution_plan",
    "execute_region_plan",
    "resolve_region_targets",
    "validate_region_manifest",
]

