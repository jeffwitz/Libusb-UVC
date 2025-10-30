#!/usr/bin/env python3
"""Capture a single frame from a UVC camera using the PyUSB helpers."""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

import usb.core

from uvc_usb import (
    UVCCamera,
    find_uvc_devices,
    list_streaming_interfaces,
    select_format_and_frame,
    describe_device,
    UVCError,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Capture one frame via PyUSB")
    parser.add_argument("--vid", type=lambda x: int(x, 0), help="Vendor ID filter")
    parser.add_argument("--pid", type=lambda x: int(x, 0), help="Product ID filter")
    parser.add_argument("--device-index", type=int, default=0, help="Index of the matching device to use")
    parser.add_argument("--interface", type=int, default=1, help="Video streaming interface number")
    parser.add_argument("--width", type=int, help="Desired frame width")
    parser.add_argument("--height", type=int, help="Desired frame height")
    parser.add_argument("--format", type=int, help="UVC bFormatIndex to commit")
    parser.add_argument("--frame", type=int, help="UVC bFrameIndex to commit")
    parser.add_argument("--fps", type=float, help="Target frame rate in Hz")
    parser.add_argument("--alt-setting", type=int, help="Force a specific alternate setting after commit")
    parser.add_argument("--skip-frames", type=int, default=0, help="Number of frames to discard before saving")
    parser.add_argument("--timeout", type=int, default=2000, help="Read timeout in milliseconds")
    parser.add_argument("--output", type=Path, help="Destination file for the raw payload")
    parser.add_argument("--log-level", default="WARNING")
    args = parser.parse_args()

    logging.basicConfig(level=args.log_level.upper())

    devices = find_uvc_devices(args.vid, args.pid)
    if not devices:
        print("No matching UVC devices.")
        return 1

    if not (0 <= args.device_index < len(devices)):
        print(f"Device index {args.device_index} is out of range (found {len(devices)} devices)")
        return 1

    dev = devices[args.device_index]
    print(f"Using device: {describe_device(dev)}")

    interfaces = list_streaming_interfaces(dev)
    if args.interface not in interfaces:
        print(f"Interface {args.interface} is not a UVC streaming interface on this device")
        return 1

    intf = interfaces[args.interface]
    if not intf.formats:
        print("Selected interface does not expose any streaming formats")
        return 1

    stream_format = None
    frame = None
    if args.format is not None or args.frame is not None:
        try:
            stream_format, frame = select_format_and_frame(intf.formats, args.format, args.frame)
        except ValueError as exc:
            print(f"Format selection error: {exc}")
            return 1
    elif args.width is not None and args.height is not None:
        match = intf.find_frame(args.width, args.height)
        if match is None:
            print(
                f"Resolution {args.width}x{args.height} not advertised on interface {args.interface}" 
                "; specify --format/--frame explicitly"
            )
            return 1
        stream_format, frame = match
    else:
        # Default to first advertised frame
        stream_format, frame = select_format_and_frame(intf.formats, None, None)

    try:
        with UVCCamera.from_device(dev, args.interface) as camera:
            if args.width is not None and args.height is not None and args.format is None and args.frame is None:
                negotiation = camera.configure_resolution(
                    args.width,
                    args.height,
                    preferred_format_index=stream_format.format_index,
                    frame_rate=args.fps,
                    alt_setting=args.alt_setting,
                )
            else:
                negotiation = camera.configure_stream(
                    stream_format,
                    frame,
                    frame_rate=args.fps,
                    alt_setting=args.alt_setting,
                )
            print(
                "Committed format #{fmt} ({desc}), frame #{frame_idx} {width}x{height}"
                .format(
                    fmt=stream_format.format_index,
                    desc=stream_format.description,
                    frame_idx=frame.frame_index,
                    width=frame.width,
                    height=frame.height,
                )
            )
            if negotiation.get("frame_rate_hz"):
                print(f"Negotiated frame rate: {negotiation['frame_rate_hz']:.2f} Hz")
            if negotiation.get("selected_alt") is not None:
                print(
                    "Using alt setting {alt} (packet {packet} bytes, endpoint 0x{ep:02x})".format(
                        alt=negotiation['selected_alt'],
                        packet=negotiation.get('iso_packet_size', 'n/a'),
                        ep=negotiation.get('endpoint_address', 0),
                    )
                )

            for _ in range(max(0, args.skip_frames)):
                camera.read_frame(timeout_ms=args.timeout)

            captured = camera.read_frame(timeout_ms=args.timeout)
    except (usb.core.USBError, UVCError) as exc:
        print(f"Failed to capture frame: {exc}")
        return 1

    payload = captured.payload
    if not payload:
        print("Captured frame was empty")
        return 1

    if args.output:
        args.output.write_bytes(payload)
        print(f"Saved {len(payload)} bytes to {args.output}")
    else:
        print(f"Captured frame size: {len(payload)} bytes (format {captured.format.description})")
        print("Use --output to store the raw payload (e.g., MJPEG) to disk.")

    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
