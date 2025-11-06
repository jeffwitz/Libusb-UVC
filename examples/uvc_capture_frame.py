#!/usr/bin/env python3
"""Capture a single frame from a UVC camera and save it as an image file."""

from __future__ import annotations

import argparse
import logging
import pathlib
import sys
import time
from pathlib import Path

from PIL import Image

ROOT = pathlib.Path(__file__).resolve().parents[1]
try:
    from libusb_uvc import (
        CodecPreference,
        FrameInfo,
        StreamFormat,
        UVCCamera,
        UVCError,
        VS_FORMAT_MJPEG,
        VS_FORMAT_UNCOMPRESSED,
        decode_to_rgb,
        describe_device,
    )
except ImportError:  # pragma: no cover
    sys.path.insert(0, str(ROOT / "src"))
    from libusb_uvc import (
        CodecPreference,
        FrameInfo,
        StreamFormat,
        UVCCamera,
        UVCError,
        VS_FORMAT_MJPEG,
        VS_FORMAT_UNCOMPRESSED,
        decode_to_rgb,
        describe_device,
    )

LOG = logging.getLogger("capture_frame")


def save_frame(output_path: Path, payload: bytes, stream_format: StreamFormat, frame: FrameInfo) -> None:
    """Persist the captured payload, converting when convenient."""
    output_suffix = output_path.suffix.lower()

    if stream_format.subtype == VS_FORMAT_MJPEG:
        if output_suffix in {".jpg", ".jpeg"}:
            output_path.write_bytes(payload)
            LOG.info("Saved MJPEG payload directly to %s", output_path)
            return
        try:
            import cv2
            import numpy as np

            arr = np.frombuffer(payload, dtype=np.uint8)
            bgr = cv2.imdecode(arr, cv2.IMREAD_COLOR)
            if bgr is None:
                raise RuntimeError("cv2.imdecode returned None")
            rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
            Image.fromarray(rgb).save(output_path)
            LOG.info("Converted MJPEG payload to %s", output_suffix.upper())
            return
        except ImportError:
            LOG.warning("OpenCV unavailable; saving MJPEG payload as .raw")
        except Exception as exc:
            LOG.warning("MJPEG conversion failed (%s); saving raw payload", exc)
        output_path.with_suffix(".raw").write_bytes(payload)
        return

    if stream_format.subtype == VS_FORMAT_UNCOMPRESSED:
        if output_suffix in {".jpg", ".jpeg", ".png", ".tiff", ".tif"}:
            rgb = decode_to_rgb(payload, stream_format, frame)
            Image.fromarray(rgb).save(output_path)
            LOG.info("Converted uncompressed frame to %s", output_suffix.upper())
            return
        output_path.write_bytes(payload)
        LOG.info("Saved uncompressed payload as raw bytes")
        return

    LOG.warning("Unsupported format %s; saving raw payload", stream_format.description)
    output_path.write_bytes(payload)


def main() -> int:
    parser = argparse.ArgumentParser(description="Capture a single UVC frame")
    parser.add_argument("--vid", type=lambda x: int(x, 0), help="Vendor ID filter")
    parser.add_argument("--pid", type=lambda x: int(x, 0), help="Product ID filter")
    parser.add_argument("--device-index", type=int, default=0, help="Index of the matching device")
    parser.add_argument("--interface", type=int, default=1, help="Video streaming interface number")
    parser.add_argument("--width", type=int, required=True, help="Desired frame width")
    parser.add_argument("--height", type=int, required=True, help="Desired frame height")
    parser.add_argument("--fps", type=float, default=15.0, help="Target frame rate in Hz")
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
    )
    parser.add_argument("--skip-frames", type=int, default=2, help="Frames to discard before saving")
    parser.add_argument("--timeout", type=int, default=5000, help="Capture timeout in milliseconds")
    parser.add_argument("--output", type=Path, required=True, help="Destination file (e.g. frame.jpg)")
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

            save_frame(args.output, captured.payload, captured.format, captured.frame)

    except UVCError as exc:
        print(f"Failed to initialize or capture from camera: {exc}")
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
