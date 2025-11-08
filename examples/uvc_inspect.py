#!/usr/bin/env python3
"""Inspect UVC camera capabilities using the high-level helpers."""

from __future__ import annotations

import argparse
import logging
from typing import Dict, Optional

import usb.core
import usb.util

from uvc_cli import (
    add_device_arguments,
    apply_device_filters,
    ensure_repo_import,
    resolve_device_index,
)

ensure_repo_import()
from libusb_uvc import (  # type: ignore  # pylint: disable=wrong-import-position
    GET_CUR,
    GET_DEF,
    GET_INFO,
    GET_MAX,
    GET_MIN,
    GET_RES,
    CapturedFrame,
    CodecPreference,
    ControlEntry,
    StreamingInterface,
    UVCCamera,
    UVCControlsManager,
    UVCError,
    VS_FORMAT_MJPEG,
    VS_FORMAT_UNCOMPRESSED,
    claim_vc_interface,
    describe_device,
    find_uvc_devices,
    list_control_units,
    list_streaming_interfaces,
    probe_streaming_interface,
    select_format_and_frame,
)

LOG = logging.getLogger("inspect_device")


def print_device_summary(vid: Optional[int], pid: Optional[int]) -> int:
    devices = find_uvc_devices(vid, pid)
    if not devices:
        print("No UVC devices detected.")
        return 1

    print("Detected UVC devices:")
    for idx, dev in enumerate(devices):
        desc = describe_device(dev)
        bus = getattr(dev, "bus", "?")
        address = getattr(dev, "address", "?")
        ports = getattr(dev, "port_numbers", None)
        if ports:
            path = ".".join(str(p) for p in ports)
        else:
            single = getattr(dev, "port_number", None)
            path = str(single) if single is not None else "-"
        serial = None
        try:
            if dev.iSerialNumber:
                serial = usb.util.get_string(dev, dev.iSerialNumber)
        except Exception:
            serial = None
        print(f"[{idx}] {desc}")
        print(
            f"    VID:PID=0x{dev.idVendor:04x}:0x{dev.idProduct:04x} "
            f"bus={bus} addr={address} path={path} serial={serial or '-'}"
        )
    return 0


def _fetch_control_value(
    dev: usb.core.Device,
    control: ControlEntry,
    request: int,
    *,
    length_hint: Optional[int] = None,
) -> Optional[bytes]:
    if length_hint is None:
        length_hint = control.length or len(control.raw_default or b"") or 4
    try:
        with claim_vc_interface(dev, control.interface_number):
            data = dev.ctrl_transfer(
                usb.util.build_request_type(
                    usb.util.CTRL_IN, usb.util.CTRL_TYPE_CLASS, usb.util.CTRL_RECIPIENT_INTERFACE
                ),
                request,
                control.selector << 8,
                (control.interface_number << 8) | control.unit_id,
                length_hint,
                timeout=500,
            )
    except (usb.core.USBError, RuntimeError):
        return None
    return bytes(data) if data is not None else None


def _format_value(label: str, raw: Optional[bytes], *, signed: bool = False) -> Optional[str]:
    if raw is None or not raw:
        return None
    if label == "info":
        return f"info=0x{raw[0]:02x}"
    if len(raw) <= 4:
        value = int.from_bytes(raw, "little", signed=signed)
        return f"{label}={value}"
    return f"{label}=0x{raw.hex()}"


def print_controls(dev: usb.core.Device) -> None:
    units_map = list_control_units(dev)
    if not units_map:
        print("  No Video Control units found or parsed.")
        return

    for interface_number, units in units_map.items():
        print(f"  Interface {interface_number}:")
        try:
            with claim_vc_interface(dev, interface_number):
                manager = UVCControlsManager(dev, units, interface_number=interface_number)
                controls = manager.get_controls()
        except RuntimeError as exc:
            print(f"    Unable to claim VC interface {interface_number}: {exc}")
            continue
        if not controls:
            print("    (No validated controls)")
            continue
        for entry in controls:
            details = [f"info=0x{entry.info:02x}"]
            if entry.minimum is not None:
                details.append(f"min={entry.minimum}")
            if entry.maximum is not None:
                details.append(f"max={entry.maximum}")
            if entry.step is not None:
                details.append(f"step={entry.step}")
            if entry.default is not None:
                details.append(f"def={entry.default}")

            cur_raw = _fetch_control_value(dev, entry, GET_CUR, length_hint=entry.length)
            if cur_raw is not None:
                signed = entry.minimum is not None and entry.minimum < 0
                formatted = _format_value("cur", cur_raw, signed=signed)
                if formatted:
                    details.append(formatted)
            print(
                f"    Unit {entry.unit_id} ({entry.type}) selector {entry.selector}: {entry.name}"
            )
            print(f"      ({', '.join(details)})")


def print_streaming(
    dev: usb.core.Device,
    *,
    still_tests: Optional[dict] = None,
) -> None:
    interfaces = list_streaming_interfaces(dev)
    for interface in interfaces.values():
        print(f"  Interface {interface.interface_number}:")
        for fmt in interface.formats:
            print(f"    Format {fmt.format_index}: {fmt.description}")
            for frame in fmt.frames:
                fps = frame.intervals_hz()
                fps_desc = ", ".join(f"{v:.2f} Hz" for v in fps) if fps else "-"
                print(
                    f"      Frame {frame.frame_index}: {frame.width}x{frame.height} "
                    f"max={frame.max_frame_size} bytes fps={fps_desc}"
                )
            if fmt.still_frames:
                print("      Still-image frames:")
                for still in fmt.still_frames:
                    endpoint = (
                        "stream"
                        if still.endpoint_address in (None, 0)
                        else f"0x{still.endpoint_address:02x}"
                    )
                    comps = ", ".join(str(idx) for idx in still.compression_indices) or "-"
                    print(
                        f"        #{still.frame_index}: {still.width}x{still.height} "
                        f"endpoint={endpoint} compression={comps}"
                    )
        if still_tests:
            tracker = still_tests.get(interface.interface_number)
            if tracker and tracker.get("message"):
                print(f"    Still capture test: {tracker['message']}")
        print("    Alternate settings:")
        for alt in interface.alt_settings:
            endpoint = f"0x{alt.endpoint_address:02x}" if alt.endpoint_address is not None else "-"
            attrs = f"0x{alt.endpoint_attributes:02x}" if alt.endpoint_attributes is not None else "-"
            print(
                f"      Alt {alt.alternate_setting}: endpoint={endpoint} attrs={attrs} "
                f"packet={alt.max_packet_size}"
            )


def run_probe(dev: usb.core.Device, args) -> None:
    interfaces = list_streaming_interfaces(dev)
    info = interfaces.get(args.probe_interface)
    if info is None:
        print(f"Interface {args.probe_interface} is not a streaming interface")
        return
    stream_format, frame = select_format_and_frame(
        info.formats,
        args.probe_format,
        args.probe_frame,
    )
    try:
        result = probe_streaming_interface(
            dev,
            info.interface_number,
            stream_format,
            frame,
            args.probe_rate,
            bool(args.commit),
            args.alt_setting,
        )
    except usb.core.USBError as exc:
        print(f"  Probe request failed: {exc}")
        return
    print(
        f"  Probe result: format {stream_format.description}, frame {frame.width}x{frame.height}"
    )
    for key in sorted(result):
        print(f"    {key}: {result[key]}")


def _payload_summary(frame: CapturedFrame) -> str:
    head = frame.payload[:16]
    return " ".join(f"{byte:02x}" for byte in head)


def _is_valid_payload(frame: CapturedFrame) -> bool:
    if not frame.payload:
        return False
    subtype = frame.format.subtype
    if subtype == VS_FORMAT_MJPEG:
        return frame.payload.startswith(b"\xff\xd8")
    if subtype == VS_FORMAT_UNCOMPRESSED:
        return any(frame.payload)
    return True


def _still_combinations(interface: StreamingInterface) -> List[tuple]:
    combos: List[tuple] = []

    def area(width: int, height: int) -> int:
        return width * height

    # Method 2 descriptors first (highest resolution to lowest)
    for fmt in interface.formats:
        ordered = sorted(fmt.still_frames, key=lambda frame: area(frame.width, frame.height), reverse=True)
        if ordered:
            still = ordered[0]
            comps = still.compression_indices or [1]
            combos.append(("method2", fmt, still, comps))

    # Method 1 fallback (streaming frames with bmStillSupported)
    for fmt in interface.formats:
        frames = [frame for frame in fmt.frames if frame.supports_still]
        if frames:
            frame = max(frames, key=lambda item: area(item.width, item.height))
            combos.append(("method1", fmt, frame, [1]))

    return combos


def test_still_capture(
    *,
    vid: int,
    pid: int,
    device_index: int,
    interfaces: Dict[int, StreamingInterface],
) -> Dict[int, Dict[str, str]]:
    results: Dict[int, Dict[str, str]] = {}

    for interface_number in sorted(interfaces.keys()):
        tracker: Dict[str, str] = {}
        results[interface_number] = tracker
        interface = interfaces[interface_number]
        combos = _still_combinations(interface)
        if not combos:
            tracker["message"] = "No still-image descriptors advertised"
            continue
        try:
            with UVCCamera.open(
                vid=vid,
                pid=pid,
                device_index=device_index,
                interface=interface_number,
            ) as camera:
                attempts = 0
                success_msg: Optional[str] = None
                last_issue: Optional[str] = None

                for method, fmt, frame_desc, comp_list in combos:
                    for comp in comp_list or [1]:
                        attempts += 1
                        desc = (
                            f"method={method} fmt={fmt.format_index} frame={frame_desc.frame_index} "
                            f"{frame_desc.width}x{frame_desc.height} comp={comp}"
                        )
                        try:
                            info = camera.configure_still_image(
                                format_index=fmt.format_index,
                                frame_index=frame_desc.frame_index,
                                compression_index=comp,
                            )
                            setattr(camera, "_still_allow_fallback", False)
                        except Exception as exc:
                            last_issue = f"{desc} configure failed: {exc}"
                            continue

                        try:
                            frame = camera.capture_still_image(timeout_ms=2000)
                        except Exception as exc:
                            last_issue = f"{desc} capture failed: {exc}"
                            continue

                        if _is_valid_payload(frame):
                            success_msg = (
                                f"Still capture OK ({desc}) len={len(frame.payload)} "
                                f"subtype=0x{frame.format.subtype:02x} head={_payload_summary(frame)}"
                            )
                            break
                        else:
                            last_issue = (
                                f"{desc} returned unusable payload len={len(frame.payload)} "
                                f"subtype=0x{frame.format.subtype:02x} head={_payload_summary(frame)}"
                            )
                    if success_msg:
                        break

                if success_msg:
                    tracker["message"] = success_msg + " (first working combination)"
                else:
                    if last_issue:
                        tracker["message"] = (
                            f"All still combinations failed after {attempts} attempts; last issue: {last_issue}"
                        )
                    else:
                        tracker["message"] = "All still combinations failed"
        except Exception as exc:  # pragma: no cover - defensive
            tracker["message"] = f"Still capture setup failed: {exc}"

    return results


def main() -> int:
    parser = argparse.ArgumentParser(description="Inspect UVC cameras using libusb")
    add_device_arguments(parser, default_index=None)
    parser.add_argument("--probe-interface", type=int, help="Interface index to run PROBE/COMMIT on")
    parser.add_argument("--probe-format", type=int, help="Format index to select for PROBE")
    parser.add_argument("--probe-frame", type=int, help="Frame index to select for PROBE")
    parser.add_argument("--probe-rate", type=float, help="Frame rate (Hz) during PROBE")
    parser.add_argument("--commit", action="store_true", help="Send COMMIT after PROBE")
    parser.add_argument("--alt-setting", type=int, help="Alt setting to force on the VS interface")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--test-still", action="store_true", help="Attempt to capture a still frame for each VS interface")
    parser.add_argument("--list", action="store_true", help="Only list matching devices and exit")
    args = parser.parse_args()

    apply_device_filters(args)
    if getattr(args, "device_sn", None) or getattr(args, "device_path", None):
        resolve_device_index(args)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO)

    # Default behaviour: list devices unless a specific filter or index was provided.
    if args.list or (
        args.vid is None
        and args.pid is None
        and getattr(args, "device_sn", None) is None
        and getattr(args, "device_path", None) is None
        and args.device_index is None
    ):
        return print_device_summary(args.vid, args.pid)

    devices = find_uvc_devices(args.vid, args.pid)
    if not devices:
        print("No matching UVC devices found.")
        return 1

    if args.device_index is not None:
        indices = [args.device_index]
    else:
        indices = list(range(len(devices)))

    for idx in indices:
        if idx < 0 or idx >= len(devices):
            print(f"Device index {idx} out of range (found {len(devices)})")
            return 1
        dev = devices[idx]
        print(f"Device [{idx}]: {describe_device(dev)}")

        stream_map = list_streaming_interfaces(dev)
        still_results = None
        if args.test_still:
            still_results = test_still_capture(
                vid=dev.idVendor,
                pid=dev.idProduct,
                device_index=idx,
                interfaces=stream_map,
            )

        print("\n--- Video Streaming (VS) Interfaces ---")
        print_streaming(dev, still_tests=still_results)

        print("\n--- Video Control (VC) Interface & Controls ---")
        try:
            print_controls(dev)
        except usb.core.USBError as exc:
            print(f"  Unable to enumerate VC controls: {exc}")

        if args.probe_interface is not None:
            print("\n--- Probe/Commit Test ---")
            run_probe(dev, args)

        print("\n" + "=" * 70)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
