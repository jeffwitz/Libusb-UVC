#!/usr/bin/env python3
"""Capture a YUY2 frame and display it with matplotlib."""

from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path
import time
from typing import Optional

import matplotlib
import usb.core

if not os.environ.get("DISPLAY"):
    matplotlib.use("Agg")

import matplotlib.pyplot as plt

LOG = logging.getLogger(__name__)

from uvc_usb import (
    UVCCamera,
    FrameAssemblyResult,
    UVCPacketAssembler,
    find_uvc_devices,
    list_streaming_interfaces,
    resolve_stream_preference,
    select_format_and_frame,
    decode_to_rgb,
    describe_device,
    UVCError,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Capture and display a YUY2 frame")
    parser.add_argument("--vid", type=lambda x: int(x, 0), help="Vendor ID filter")
    parser.add_argument("--pid", type=lambda x: int(x, 0), help="Product ID filter")
    parser.add_argument("--device-index", type=int, default=0, help="Index of the matching device to use")
    parser.add_argument("--interface", type=int, default=1, help="Video streaming interface number")
    parser.add_argument("--width", type=int, default=640, help="Desired frame width")
    parser.add_argument("--height", type=int, default=480, help="Desired frame height")
    parser.add_argument("--format", type=int, help="Optional UVC bFormatIndex to force")
    parser.add_argument("--frame", type=int, help="Optional UVC bFrameIndex to force")
    parser.add_argument("--fps", type=float, default=30.0, help="Target frame rate in Hz")
    parser.add_argument("--skip-frames", type=int, default=2, help="Number of frames to discard before showing")
    parser.add_argument("--timeout", type=int, default=2000, help="Read timeout in milliseconds")
    parser.add_argument("--alt-setting", type=int, help="Force a specific alternate setting after commit")
    parser.add_argument("--log-level", default="WARNING")
    args = parser.parse_args()

    logging.basicConfig(level=args.log_level.upper())

    devices = find_uvc_devices(args.vid, args.pid)
    if not devices:
        print("No matching UVC devices.")
        return 1

    if not (0 <= args.device_index < len(devices)):
        print(f"Device index {args.device_index} out of range (found {len(devices)})")
        return 1

    dev = devices[args.device_index]
    print(f"Using device: {describe_device(dev)}")

    interfaces = list_streaming_interfaces(dev)
    if args.interface not in interfaces:
        print(f"Interface {args.interface} is not a UVC streaming interface on this device")
        return 1

    streaming = interfaces[args.interface]
    if not streaming.formats:
        print("Selected interface does not expose any streaming formats")
        return 1

    try:
        if args.format is not None or args.frame is not None:
            stream_format, frame = select_format_and_frame(
                streaming.formats, args.format, args.frame
            )
        else:
            stream_format, frame = resolve_stream_preference(
                streaming,
                args.width,
                args.height,
                codec="yuyv",
            )
    except (ValueError, UVCError) as exc:
        print(f"Format selection error: {exc}")
        return 1

    frame_payload = capture_frame_async(
        dev,
        args.interface,
        stream_format,
        frame,
        fps=args.fps,
        alt_setting=args.alt_setting,
        skip_frames=args.skip_frames,
        timeout_ms=args.timeout,
    )
    if frame_payload is None:
        print("Timed out while waiting for a complete frame")
        return 1

    try:
        rgb = decode_to_rgb(frame_payload, stream_format, frame)
    except RuntimeError as exc:
        print(f"Failed to convert frame: {exc}")
        return 1
    plt.figure("UVC Frame")
    plt.imshow(rgb)
    plt.axis("off")
    plt.title(f"{frame.width}x{frame.height} {stream_format.description}")
    if matplotlib.get_backend().lower() == "agg":
        output = Path("uvc_frame.png")
        plt.savefig(output)
        print(f"Headless environment detected; saved frame to {output}")
    else:
        plt.show()
    return 0


def capture_frame_async(
    dev,
    interface_number: int,
    stream_format: StreamFormat,
    frame: FrameInfo,
    *,
    fps: float,
    alt_setting: Optional[int],
    skip_frames: int,
    timeout_ms: int,
):
    expected_size = frame.max_frame_size or (frame.width * frame.height * 2)

    with UVCCamera.from_device(dev, interface_number) as camera:
        negotiation = camera.configure_stream(
            stream_format,
            frame,
            frame_rate=fps,
            alt_setting=alt_setting,
        )
        if negotiation.get("dwMaxVideoFrameSize") is not None:
            print(
                "Negotiated frame size:"
                f" {negotiation['dwMaxVideoFrameSize']} bytes"
            )
        if negotiation.get("dwMaxPayloadTransferSize") is not None:
            print(
                "Negotiated payload size:"
                f" {negotiation['dwMaxPayloadTransferSize']} bytes"
            )
        if negotiation.get("selected_alt") is not None:
            print(
                "Using alt setting {alt} (packet {packet} bytes, endpoint 0x{ep:02x})".format(
                    alt=negotiation['selected_alt'],
                    packet=negotiation.get('iso_packet_size', 'n/a'),
                    ep=negotiation.get('endpoint_address', 0),
                )
            )

        frames_to_skip = max(0, skip_frames)
        frame_event = False
        captured_frame: Optional[bytes] = None

        assembler = UVCPacketAssembler(expected_size=expected_size)

        def handle_result(result: FrameAssemblyResult) -> None:
            nonlocal frames_to_skip, frame_event, captured_frame
            LOG.debug(
                "Finalized frame reason=%s size=%s expected=%s error=%s",
                result.reason,
                result.size,
                result.expected_size,
                result.error,
            )
            if not result.complete:
                return
            if frames_to_skip > 0:
                frames_to_skip -= 1
                LOG.debug(
                    "Skipping frame (reason=%s) size=%s remaining=%s",
                    result.reason,
                    result.size,
                    frames_to_skip,
                )
                return
            captured_frame = result.payload
            frame_event = True
            LOG.debug(
                "Captured frame (reason=%s) size=%s",
                result.reason,
                result.size,
            )

        def on_packet(packet: bytes) -> None:
            if not packet:
                return

            for result in assembler.submit(packet):
                handle_result(result)

        camera.start_async_stream(
            on_packet,
            transfers=12,
            packets_per_transfer=32,
            timeout_ms=max(timeout_ms, 2000),
        )

        try:
            deadline = time.time() + (timeout_ms / 1000.0 if timeout_ms else 5)
            while not frame_event and time.time() < deadline:
                camera.poll_async_events(0.01)
            if not frame_event:
                result = assembler.flush("timeout")
                if result is not None:
                    handle_result(result)
            if not frame_event:
                return None
            return captured_frame
        finally:
            camera.stop_async_stream()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
