#!/usr/bin/env python3
"""Enumerate UVC camera modes using PyUSB and the lightweight helpers."""

from __future__ import annotations

import argparse
import logging

import usb.core

from uvc_usb import (
    find_uvc_devices,
    list_streaming_interfaces,
    probe_streaming_interface,
    select_format_and_frame,
    describe_device,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Enumerate camera formats via PyUSB")
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
        interfaces = list_streaming_interfaces(dev)

        for intf_number in sorted(interfaces):
            info = interfaces[intf_number]
            for alt in info.alt_settings:
                print(f"  Streaming interface {intf_number} alt {alt.alternate_setting}")
                if alt.alternate_setting != 0:
                    comment = "Alternate streaming setting"
                    if alt.max_packet_size:
                        comment += f" (max payload {alt.max_packet_size} bytes)"
                    print(f"    ({comment})")
                    continue

                if not info.formats:
                    print("    (No formats parsed)")
                    continue

                for fmt in info.formats:
                    print(f"    Format {fmt.format_index}: {fmt.description}")
                    for frame in fmt.frames:
                        rates = ", ".join(f"{hz:.2f} Hz" for hz in frame.intervals_hz())
                        suffix = f" @ {rates}" if rates else ""
                        print(f"      #{frame.frame_index}: {frame.width}x{frame.height}{suffix}")

        if args.probe_interface is None or args.probe_interface not in interfaces:
            continue

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

    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
