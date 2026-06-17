"""Backward-compatible import wrapper for AWS provider module."""

import importlib as _importlib

_impl = _importlib.import_module("providers.aws.template_generator.ec2_template_generator")

globals().update(
    {
        name: getattr(_impl, name)
        for name in dir(_impl)
        if not name.startswith("__")
    }
)

del _impl
__all__ = [name for name in globals() if not name.startswith("__")]