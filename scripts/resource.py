"""Backward-compatible import wrapper for AWS resource discovery."""

from providers.aws import resource as _impl

globals().update(
    {
        name: getattr(_impl, name)
        for name in dir(_impl)
        if not name.startswith("__")
    }
)

del _impl
__all__ = [name for name in globals() if not name.startswith("__")]
