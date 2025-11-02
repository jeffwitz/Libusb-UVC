#!/usr/bin/env python3
"""Trigger a still-image capture via the UVC still image controls."""

from __future__ import annotations

import argparse
import logging
import pathlib
import sys
from pathlib import Path

ROOT = pathlib.Path(__file__).resolve().parents[1]
try:
    from libusb_uvc import (
        CodecPreference,
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
        UVCCamera,
        UVCError,
        VS_FORMAT_MJPEG,
        VS_FORMAT_UNCOMPRESSED,
        decode_to_rgb,
        describe_device,
    )

LOG = logging.getLogger("capture_still")


def save_payload(output_path: Path, payload: bytes, stream_format, frame_info) -> None:
    suffix = output_path.suffix.lower()

    if stream_format.subtype == VS_FORMAT_MJPEG:
        if suffix in {".jpg", ".jpeg"}:
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
        except ImportError:
            LOG.warning("OpenCV unavailable; storing MJPEG payload as raw bytes")
            output_path.with_suffix(".raw").write_bytes(payload)
            return
        except Exception as exc:
            LOG.warning("Failed to decode MJPEG payload (%s); storing raw bytes", exc)
            output_path.with_suffix(".raw").write_bytes(payload)
            return

        try:
            from PIL import Image  # type: ignore
        except ImportError:
            LOG.warning("Pillow unavailable; storing MJPEG payload as raw bytes")
            output_path.with_suffix(".raw").write_bytes(payload)
            return

        Image.fromarray(rgb).save(output_path)
        LOG.info("Converted MJPEG payload to %s", suffix or ".png")
        return

    if stream_format.subtype == VS_FORMAT_UNCOMPRESSED:
        rgb = decode_to_rgb(payload, stream_format, frame_info)
        try:
            from PIL import Image  # type: ignore
        except ImportError:
            LOG.warning("Pillow unavailable; storing raw payload for uncompressed frame")
            output_path.with_suffix(".raw").write_bytes(payload)
            return

        if suffix not in {".tiff", ".tif"}:
            output_path = output_path.with_suffix(".tiff")
            LOG.info("Using TIFF container for uncompressed payload: %s", output_path)
        Image.fromarray(rgb).save(output_path)
        LOG.info("Saved uncompressed still to %s", output_path)
        return

    LOG.warning("Unknown format subtype 0x%02x; storing raw payload", stream_format.subtype)
    output_path.write_bytes(payload)


def main() -> int:
    parser = argparse.ArgumentParser(description="Capture a still image via UVC still-image controls")
    parser.add_argument("--vid", type=lambda s: int(s, 0), help="Vendor ID (hex ok)")
    parser.add_argument("--pid", type=lambda s: int(s, 0), help="Product ID (hex ok)")
    parser.add_argument("--device-index", type=int, default=0, help="Index within detected devices")
    parser.add_argument("--interface", type=int, default=1, help="Video streaming interface number")
    parser.add_argument("--width", type=int, help="Still image width (defaults to the highest advertised if omitted)")
    parser.add_argument("--height", type=int, help="Still image height")
    parser.add_argument("--codec", choices=[CodecPreference.AUTO, CodecPreference.YUYV, CodecPreference.MJPEG], default=CodecPreference.AUTO)
    parser.add_argument("--format-index", type=int, help="Still image format index")
    parser.add_argument("--frame-index", type=int, help="Still image frame index")
    parser.add_argument("--compression-index", type=int, default=1, help="Still image compression index")
    parser.add_argument("--timeout", type=int, default=5000, help="Capture timeout in milliseconds")
    parser.add_argument("--log-level", default="INFO")
    parser.add_argument("--output", type=Path, required=True, help="Destination file (e.g. still.jpg)")
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

            if args.width and args.height:
                info = camera.configure_still_image(
                    width=args.width,
                    height=args.height,
                    codec=args.codec,
                    compression_index=args.compression_index,
                )
            elif args.format_index is not None or args.frame_index is not None:
                info = camera.configure_still_image(
                    format_index=args.format_index,
                    frame_index=args.frame_index,
                    compression_index=args.compression_index,
                )
            else:
                info = camera.configure_still_image(
                    codec=args.codec,
                    compression_index=args.compression_index,
                )

            LOG.info("Still PROBE/COMMIT info: %s", info)

            frame = camera.capture_still_image(timeout_ms=max(1000, args.timeout))
            LOG.info(
                "Captured still frame %sx%s (%d bytes, subtype=0x%02x)",
                frame.frame.width,
                frame.frame.height,
                len(frame.payload),
                frame.format.subtype,
            )
            save_payload(args.output, frame.payload, frame.format, frame.frame)

    except UVCError as exc:
        print(f"Failed to capture still image: {exc}")
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
