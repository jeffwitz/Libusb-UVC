#!/usr/bin/env python3
"""Enumerate UVC camera modes and controls using the lightweight helpers."""

from __future__ import annotations

import argparse
import contextlib
import logging
from typing import Optional

import usb.core

from uvc_usb import (
    find_uvc_devices,
    list_streaming_interfaces,
    list_control_units,
    probe_streaming_interface,
    select_format_and_frame,
    describe_device,
    _read_control,
    GET_CUR, GET_MIN, GET_MAX, GET_RES, GET_DEF, GET_LEN,
    UVCControl, UVCUnit, ExtensionUnit,
)


def _read_control_value(
    dev: usb.core.Device,
    interface: int,
    unit_id: int,
    control: UVCControl,
    request: int
) -> Optional[int]:
    """Helper to read a control value and handle errors."""
    # Higher byte of wValue is the control selector
    # Higher byte of wIndex is the unit/terminal ID
    w_value = control.selector << 8
    # The interface number for control requests is the VC interface, not the VS interface
    w_index = unit_id << 8 | interface

    try:
        # Determine data length by querying GET_LEN first
        length_data = _read_control(dev, GET_LEN, w_value, w_index, 2)
        if length_data is None or len(length_data) < 2:
            return None # Cannot determine length

        length = int.from_bytes(length_data, "little")
        if length == 0:
            return None

        data = _read_control(dev, request, w_value, w_index, length)
        if data is not None:
            # Handle boolean controls which might be 1 byte
            if length == 1:
                return int.from_bytes(data, "little", signed=False)
            return int.from_bytes(data, "little", signed=True)
    except usb.core.USBError as e:
        # Stall (EPIPE, errno 32) errors are common if a request (e.g., GET_MIN) isn't supported
        if e.errno != 32: # Suppress stall errors for cleaner output
             logging.debug(f"USBError reading control {control.name}: {e}")
        return None
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description="Enumerate camera formats and controls via PyUSB")
    parser.add_argument("--vid", type=lambda x: int(x, 0), help="Vendor ID filter")
    parser.add_argument("--pid", type=lambda x: int(x, 0), help="Product ID filter")
    parser.add_argument("--log-level", default="WARNING")
    parser.add_argument("--probe-interface", type=int, help="Run VS probe on the given interface number")
    parser.add_argument("--probe-format", type=int, help="UVC bFormatIndex to request during probe")
    parser.add_argument("--probe-frame", type=int, help="UVC bFrameIndex to request during probe")
    parser.add_argument("--probe-fps", type=float, help="Desired frame rate in Hz for probe negotiation")
    parser.add_argument(
        "--commit",
        action="store_true",
        help="Send VS COMMIT after a successful PROBE (required before streaming)",
    )
    parser.add_argument(
        "--alt-setting",
        type=int,
        help="If set, switch the interface to this alternate setting after COMMIT",
    )
    args = parser.parse_args()

    logging.basicConfig(level=args.log_level.upper())

    devices = find_uvc_devices(args.vid, args.pid)
    if not devices:
        print("No matching UVC devices.")
        return 1

    for dev in devices:
        print(f"Device: {describe_device(dev)}")

        # --- List Streaming Interfaces (existing logic) ---
        print("\n--- Video Streaming (VS) Interfaces ---")
        interfaces = list_streaming_interfaces(dev)
        if not interfaces:
            print("  No Video Streaming interfaces found.")

        for intf_number in sorted(interfaces):
            info = interfaces[intf_number]
            print(f"  Interface {intf_number}:")
            if not info.formats:
                print("    (No formats parsed)")
                continue

            for fmt in info.formats:
                print(f"    Format {fmt.format_index}: {fmt.description}")
                for frame in fmt.frames:
                    rates = ", ".join(f"{hz:.2f} Hz" for hz in frame.intervals_hz())
                    suffix = f" @ {rates}" if rates else ""
                    print(f"      Frame #{frame.frame_index}: {frame.width}x{frame.height}{suffix}")

        # --- MODIFIED: List Control Interfaces ---
        print("\n--- Video Control (VC) Interface & Controls ---")
        try:
            dev.set_configuration()
            control_units_map = list_control_units(dev)
            if not control_units_map:
                print("  No Video Control units found or parsed.")

            for intf_number, units in control_units_map.items():
                print(f"  Interface {intf_number}:")
                reattach = False
                try:
                    # Detach kernel driver for exclusive access to the control interface
                    if dev.is_kernel_driver_active(intf_number):
                        dev.detach_kernel_driver(intf_number)
                        reattach = True

                    for unit in units:
                        if isinstance(unit, ExtensionUnit):
                            print(f"    Unit {unit.unit_id} ({unit.type}):")
                            print(f"      GUID: {unit.guid}")
                            if not unit.controls:
                                print("      (Control parsing for this XU is not yet implemented)")
                        elif isinstance(unit, UVCUnit):
                            print(f"    Unit {unit.unit_id} ({unit.type}):")
                            if not unit.controls:
                                print("      (No standard controls detected)")
                            for control in unit.controls:
                                cur = _read_control_value(dev, intf_number, unit.unit_id, control, GET_CUR)
                                min_val = _read_control_value(dev, intf_number, unit.unit_id, control, GET_MIN)
                                max_val = _read_control_value(dev, intf_number, unit.unit_id, control, GET_MAX)
                                res = _read_control_value(dev, intf_number, unit.unit_id, control, GET_RES)
                                def_val = _read_control_value(dev, intf_number, unit.unit_id, control, GET_DEF)

                                details_parts = []
                                if min_val is not None: details_parts.append(f"min={min_val}")
                                if max_val is not None: details_parts.append(f"max={max_val}")
                                if res is not None: details_parts.append(f"step={res}")
                                if def_val is not None: details_parts.append(f"def={def_val}")
                                if cur is not None: details_parts.append(f"cur={cur}")
                                details = ", ".join(details_parts)

                                print(f"      Control: {control.name:<30} ({details})")

                finally:
                    # Ensure kernel driver is reattached
                    if reattach:
                        with contextlib.suppress(usb.core.USBError):
                            dev.attach_kernel_driver(intf_number)

        except usb.core.USBError as e:
            print(f"  Error inspecting controls: {e}")
            print("  (This may be a permissions issue, the device may be in use, or the kernel driver could not be detached).")
        except Exception as e:
            print(f"  An unexpected error occurred during control inspection: {e}")

        # --- Probe/Commit Logic (existing logic) ---
        if args.probe_interface is not None and args.probe_interface in interfaces:
            print("\n--- Probe/Commit Test ---")
            info = interfaces[args.probe_interface]
            try:
                stream_format, frame = select_format_and_frame(
                    info.formats,
                    args.probe_format,
                    args.probe_frame,
                )
            except ValueError as exc:
                print(f"  Probe selection error: {exc}")
                continue

            do_commit = args.commit or args.alt_setting is not None
            try:
                probe_info = probe_streaming_interface(
                    dev,
                    args.probe_interface,
                    stream_format,
                    frame,
                    args.probe_fps,
                    do_commit,
                    args.alt_setting,
                )
            except usb.core.USBError as exc:
                print(f"  Probe request failed: {exc}")
                continue

            print(
                "  Probe result: Format #{f_idx} ({f_desc}), Frame #{fr_idx} {width}x{height}".format(
                    f_idx=stream_format.format_index,
                    f_desc=stream_format.description,
                    fr_idx=frame.frame_index,
                    width=frame.width,
                    height=frame.height,
                )
            )
            if probe_info.get("frame_rate_hz"):
                print(f"    Negotiated frame rate: {probe_info['frame_rate_hz']:.2f} Hz")
            if probe_info.get("dwMaxVideoFrameSize") is not None:
                print(f"    Max frame size: {probe_info['dwMaxVideoFrameSize']} bytes")
            if probe_info.get("dwMaxPayloadTransferSize") is not None:
                print(f"    Max payload transfer: {probe_info['dwMaxPayloadTransferSize']} bytes")
            if probe_info.get("committed"):
                print("    COMMIT sent successfully")
            if probe_info.get("alt_setting") is not None:
                print(f"    Alt setting applied: {probe_info['alt_setting']}")
            if "alt_setting_error" in probe_info:
                print(f"    Alt setting error: {probe_info['alt_setting_error']}")
        print("\n" + "="*70)


if __name__ == "__main__":
    raise SystemExit(main())
