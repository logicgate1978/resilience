"""AWS custom action engine facade.

The implementation remains in the existing component_actions package for
backward compatibility while main.py moves to provider-based imports.
"""

from component_actions import (
    build_custom_execution_plan,
    collect_custom_impacted_resources,
    execute_custom_plan,
    service_uses_custom_engine,
)

__all__ = [
    "build_custom_execution_plan",
    "collect_custom_impacted_resources",
    "execute_custom_plan",
    "service_uses_custom_engine",
]

