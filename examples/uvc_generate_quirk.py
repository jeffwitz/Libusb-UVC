#!/usr/bin/env python3
"""Generate a JSON skeleton describing Extension Unit controls.

This helper inspects a connected UVC device, validates its Extension Unit
controls via :class:`libusb_uvc.UVCControlsManager`, and prints a ready-to-edit
quirks file.  It is intended as a starting point when adding vendor-specific
controls to ``src/libusb_uvc/quirks``.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
from typing import Dict, Optional

ROOT = pathlib.Path(__file__).resolve().parents[1]
try:
    from libusb_uvc import (
        ControlEntry,
        ExtensionUnit,
        UVCControlsManager,
        describe_device,
        find_uvc_devices,
        find_vc_interface_number,
        list_control_units,
    )
except ImportError:  # pragma: no cover - editable install fallback
    sys.path.insert(0, str(ROOT / "src"))
    from libusb_uvc import (  # type: ignore
        ControlEntry,
        ExtensionUnit,
        UVCControlsManager,
        describe_device,
        find_uvc_devices,
        find_vc_interface_number,
        list_control_units,
    )


def _int_or_hex(value: str) -> int:
    return int(value, 0)


def _infer_type(entry: Optional[ControlEntry]) -> str:
    if entry is None:
        return "int"
    if entry.length and entry.length > 2 and entry.minimum is None and entry.maximum is None:
        return "bytes"
    if entry.minimum is not None and entry.maximum is not None:
        if entry.minimum == 0 and entry.maximum == 1:
            return "bool"
    return "int"


def _build_control_payload(entry: Optional[ControlEntry], fallback_name: str) -> Dict[str, object]:
    data: Dict[str, object] = {"name": fallback_name}
    if entry is None:
        data["type"] = "int"
        return data

    data["name"] = entry.name or fallback_name
    data["type"] = _infer_type(entry)
    if entry.length is not None:
        data["length"] = entry.length
    if entry.minimum is not None:
        data["min"] = entry.minimum
    if entry.maximum is not None:
        data["max"] = entry.maximum
    if entry.step is not None:
        data["step"] = entry.step
    if entry.default is not None:
        data["default"] = entry.default
    data["info"] = f"0x{entry.info:02x}"
    if entry.raw_default is not None:
        data["raw_default"] = entry.raw_default.hex()
    return data


def _select_device(args):
    devices = find_uvc_devices(args.vid, args.pid)
    if not devices:
        print("No matching UVC devices found.", file=sys.stderr)
        return None
    if args.device_index >= len(devices):
        print(f"Device index {args.device_index} out of range (found {len(devices)} devices)", file=sys.stderr)
        return None
    dev = devices[args.device_index]
    print(f"Using device: {describe_device(dev)}", file=sys.stderr)
    return dev


def _choose_interface(dev, args) -> Optional[int]:
    if args.vc_interface is not None:
        return args.vc_interface
    iface = find_vc_interface_number(dev)
    print(f"Selected VC interface {iface}", file=sys.stderr)
    return iface


def generate_skeleton(args) -> Optional[dict]:
    dev = _select_device(args)
    if dev is None:
        return None

    vc_interface = _choose_interface(dev, args)
    if vc_interface is None:
        print("Unable to determine VC interface number.", file=sys.stderr)
        return None

    unit_map = list_control_units(dev)
    units = unit_map.get(vc_interface, [])
    if not units:
        print(f"No VC units parsed on interface {vc_interface}", file=sys.stderr)
        return None

    extension_units = [u for u in units if isinstance(u, ExtensionUnit)]
    if args.guid:
        extension_units = [u for u in extension_units if u.guid.lower() == args.guid.lower()]

    if not extension_units:
        print("No matching Extension Units found.", file=sys.stderr)
        return None

    manager = UVCControlsManager(dev, units, interface_number=vc_interface)
    validated = {
        (entry.unit_id, entry.selector): entry
        for entry in manager.get_controls()
        if entry.type == "Extension Unit"
    }

    skeletons = []
    for unit in extension_units:
        controls = {}
        for control in unit.controls:
            key = str(control.selector)
            entry = validated.get((unit.unit_id, control.selector))
            controls[key] = _build_control_payload(entry, control.name)

        skeletons.append(
            {
                "guid": unit.guid,
                "name": unit.guid if args.unit_name is None else args.unit_name,
                "unit_id": unit.unit_id,
                "controls": controls,
            }
        )

    if args.single and skeletons:
        return skeletons[0]
    return {"extension_units": skeletons}


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate a quirks JSON skeleton for Extension Unit controls")
    parser.add_argument("--vid", type=_int_or_hex, help="Vendor ID filter (hex or decimal)")
    parser.add_argument("--pid", type=_int_or_hex, help="Product ID filter (hex or decimal)")
    parser.add_argument("--device-index", type=int, default=0, help="Index of the matching device (default: 0)")
    parser.add_argument("--vc-interface", type=int, help="VC interface number (auto-detected if omitted)")
    parser.add_argument("--guid", help="Filter to a specific Extension Unit GUID")
    parser.add_argument("--unit-name", help="Override the generated name field")
    parser.add_argument("--output", type=pathlib.Path, help="Write JSON to the given path instead of stdout")
    parser.add_argument("--single", action="store_true", help="Emit only the first matching Extension Unit object")
    parser.add_argument("--indent", type=int, default=2, help="Indent level for JSON output (default: 2)")
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    skeleton = generate_skeleton(args)
    if skeleton is None:
        return 1

    text = json.dumps(skeleton, indent=args.indent, sort_keys=True)
    if args.output:
        args.output.write_text(text + "\n", encoding="utf-8")
        print(f"Wrote skeleton to {args.output}", file=sys.stderr)
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
