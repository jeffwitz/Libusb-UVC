#!/usr/bin/env python3
"""Capture a single frame using the async API and display it with Matplotlib."""

from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path

import matplotlib
import usb.core

# Set matplotlib backend before importing pyplot if no display is available
if not os.environ.get("DISPLAY"):
    matplotlib.use("Agg")

import matplotlib.pyplot as plt

from uvc_usb import (
    UVCCamera,
    CodecPreference,
    find_uvc_devices,
    resolve_stream_preference,
    decode_to_rgb,
    describe_device,
    UVCError,
)

# Import the robust capture function from our other script
from pyusb_capture_frame import capture_single_frame

LOG = logging.getLogger(__name__)


def main() -> int:
    parser = argparse.ArgumentParser(description="Capture and display a single frame.")
    # --- Arguments harmonisés avec les autres scripts ---
    parser.add_argument("--vid", type=lambda x: int(x, 0), help="Vendor ID filter")
    parser.add_argument("--pid", type=lambda x: int(x, 0), help="Product ID filter")
    parser.add_argument("--device-index", type=int, default=0, help="Index of the matching device to use")
    parser.add_argument("--interface", type=int, default=1, help="Video streaming interface number")
    parser.add_argument("--width", type=int, default=640, help="Desired frame width")
    parser.add_argument("--height", type=int, default=480, help="Desired frame height")
    parser.add_argument("--fps", type=float, default=15.0, help="Target frame rate for negotiation")
    parser.add_argument("--skip-frames", type=int, default=2, help="Number of frames to discard before displaying")
    parser.add_argument("--timeout", type=int, default=5000, help="Capture timeout in milliseconds")
    parser.add_argument(
        "--codec",
        choices=[CodecPreference.AUTO, CodecPreference.YUYV, CodecPreference.MJPEG],
        default=CodecPreference.AUTO,
        help="Preferred codec (YUYV is often best for this script)",
    )
    parser.add_argument("--log-level", default="INFO", help="Logging level (e.g., DEBUG, INFO, WARNING)")
    args = parser.parse_args()

    logging.basicConfig(level=args.log_level.upper())

    devices = find_uvc_devices(args.vid, args.pid)
    if not devices:
        print("No matching UVC devices found.")
        return 1

    if not (0 <= args.device_index < len(devices)):
        print(f"Device index {args.device_index} is out of range (found {len(devices)} devices)")
        return 1

    dev = devices[args.device_index]
    print(f"Using device: {describe_device(dev)}")

    frame_payload = None
    stream_format_out = None
    frame_out = None

    try:
        with UVCCamera.from_device(dev, args.interface) as camera:
            stream_format, frame = resolve_stream_preference(
                camera.interface, args.width, args.height, codec=args.codec
            )
            stream_format_out = stream_format
            frame_out = frame

            print(
                f"Selected format: #{stream_format.format_index} ({stream_format.description}), "
                f"Frame: #{frame.frame_index} {frame.width}x{frame.height} (negotiating @ {args.fps}fps)"
            )

            # --- Réutilisation de la logique de capture ---
            frame_payload = capture_single_frame(
                camera,
                stream_format,
                frame,
                fps=args.fps,
                skip_frames=args.skip_frames,
                timeout_ms=args.timeout,
            )

    except (usb.core.USBError, UVCError) as exc:
        print(f"Failed to initialize or capture from camera: {exc}")
        return 1

    if frame_payload is None:
        print("Timed out or failed to capture a complete frame.")
        return 1

    try:
        print("Decoding frame for display...")
        rgb_array = decode_to_rgb(frame_payload, stream_format_out, frame_out)
    except RuntimeError as exc:
        print(f"Failed to convert frame: {exc}")
        print("The raw payload may be incomplete or corrupted.")
        return 1

    print("Displaying frame with Matplotlib...")
    plt.figure("Libusb-UVC Frame")
    plt.imshow(rgb_array)
    plt.axis("off")
    plt.title(f"{frame_out.width}x{frame_out.height} - {stream_format_out.description}")

    if matplotlib.get_backend().lower() == "agg":
        output_path = Path("uvc_frame_display.png")
        plt.savefig(output_path)
        print(f"Headless environment detected; saved frame to {output_path}")
    else:
        plt.show()

    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
