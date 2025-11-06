#!/usr/bin/env python3
"""Capture a single frame using libusb_uvc and display/save it with Matplotlib."""

from __future__ import annotations

import argparse
import logging
import os
import pathlib
import sys
from pathlib import Path

import matplotlib

if not os.environ.get("DISPLAY"):
    matplotlib.use("Agg")

import matplotlib.pyplot as plt

ROOT = pathlib.Path(__file__).resolve().parents[1]
try:
    from libusb_uvc import CodecPreference, UVCCamera, UVCError, decode_to_rgb, describe_device
except ImportError:  # pragma: no cover
    sys.path.insert(0, str(ROOT / "src"))
    from libusb_uvc import CodecPreference, UVCCamera, UVCError, decode_to_rgb, describe_device

LOG = logging.getLogger("display_frame")


def main() -> int:
    parser = argparse.ArgumentParser(description="Capture and display a single frame")
    parser.add_argument("--vid", type=lambda x: int(x, 0), help="Vendor ID filter")
    parser.add_argument("--pid", type=lambda x: int(x, 0), help="Product ID filter")
    parser.add_argument("--device-index", type=int, default=0, help="Index within the detected devices")
    parser.add_argument("--interface", type=int, default=1, help="Video streaming interface number")
    parser.add_argument("--width", type=int, default=640, help="Desired frame width")
    parser.add_argument("--height", type=int, default=480, help="Desired frame height")
    parser.add_argument("--fps", type=float, default=15.0, help="Target frame rate for negotiation")
    parser.add_argument("--skip-frames", type=int, default=2, help="Frames to discard before displaying")
    parser.add_argument("--timeout", type=int, default=5000, help="Capture timeout in milliseconds")
    parser.add_argument(
        "--codec",
        choices=[
            CodecPreference.AUTO,
            CodecPreference.YUYV,
            CodecPreference.MJPEG,
            CodecPreference.FRAME_BASED,
            CodecPreference.H264,
            CodecPreference.H265,
        ],
        default=CodecPreference.AUTO,
        help="Preferred codec",
    )
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    logging.basicConfig(level=args.log_level.upper())

    try:
        with UVCCamera.open(
            vid=args.vid,
            pid=args.pid,
            device_index=args.device_index,
            interface=args.interface,
        ) as camera:
            print(f"Using device: {describe_device(camera.device)}")

            stream = camera.stream(
                width=args.width,
                height=args.height,
                codec=args.codec,
                frame_rate=args.fps if args.fps > 0 else None,
                strict_fps=False,
                skip_initial=max(0, args.skip_frames),
                queue_size=2,
                timeout_ms=max(args.timeout, 1000),
                duration=max(args.timeout / 1000.0, 1.0),
            )

            captured = None
            with stream as frames:
                for frame in frames:
                    captured = frame
                    break

            if captured is None:
                print("Timed out or failed to capture a frame.")
                return 1

    except UVCError as exc:
        print(f"Failed to capture frame: {exc}")
        return 1

    try:
        rgb = decode_to_rgb(captured.payload, captured.format, captured.frame)
    except RuntimeError as exc:
        print(f"Failed to decode frame: {exc}")
        return 1

    plt.figure("libusb_uvc_frame")
    plt.imshow(rgb)
    plt.axis("off")
    plt.title(f"{captured.frame.width}x{captured.frame.height} - {captured.format.description}")

    if matplotlib.get_backend().lower() == "agg":
        output_path = Path("libusb_uvc_frame.png")
        plt.savefig(output_path)
        print(f"Headless environment detected; saved frame to {output_path}")
    else:
        plt.show()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
