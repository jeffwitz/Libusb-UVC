"""Public interface for the libusb_uvc package.

The implementation lives in :mod:`libusb_uvc.core`.  This module simply
re-exports the public API defined in :data:`libusb_uvc.core.__all__` so that
existing ``from libusb_uvc import ...`` statements keep working while the code
base is refactored into smaller modules.
"""

from __future__ import annotations

from importlib import import_module

_core = import_module(".core", __name__)

__all__ = list(getattr(_core, "__all__", []))
__version__ = getattr(_core, "__version__", "0.0.0")

for _name, _value in vars(_core).items():
    if _name == "__all__":
        continue
    if _name.startswith("__") and _name not in {"__version__"}:
        continue
    globals().setdefault(_name, _value)

# Re-export the logger for backwards compatibility.
LOG = globals().get("LOG")

# Provide a direct reference to the implementation module for advanced users.
core = _core
