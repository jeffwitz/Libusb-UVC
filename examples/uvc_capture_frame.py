#!/usr/bin/env python3
"""Capture a single frame from a UVC camera and save it as an image file."""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

from PIL import Image

from uvc_cli import (
    add_device_arguments,
    add_streaming_arguments,
    apply_device_filters,
    configure_logging,
    ensure_repo_import,
    resolve_device_index,
)

ensure_repo_import()
from libusb_uvc import (
    CodecPreference,
    DecoderPreference,
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
    add_device_arguments(parser)
    add_streaming_arguments(
        parser,
        width_default=None,
        height_default=None,
        width_required=True,
        height_required=True,
        fps_default=15.0,
        skip_default=2,
        timeout_default=5000,
        codec_choices=[
            CodecPreference.AUTO,
            CodecPreference.YUYV,
            CodecPreference.MJPEG,
            CodecPreference.FRAME_BASED,
            CodecPreference.H264,
            CodecPreference.H265,
        ],
        codec_default=CodecPreference.AUTO,
        include_decoder=True,
        decoder_choices=[
            DecoderPreference.AUTO,
            DecoderPreference.NONE,
            DecoderPreference.PYAV,
            DecoderPreference.GSTREAMER,
        ],
        decoder_default=DecoderPreference.AUTO,
        include_duration=True,
    )
    parser.add_argument("--output", type=Path, required=True, help="Destination file (e.g. frame.jpg)")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()
    apply_device_filters(args)
    resolve_device_index(args)

    configure_logging(args.log_level, name="capture_frame")

    try:
        with UVCCamera.open(
            vid=args.vid,
            pid=args.pid,
            device_index=args.device_index,
            interface=args.interface,
        ) as camera:
            print(f"Using device: {describe_device(camera.device)}")

            frame_rate = args.fps if args.fps > 0 else None
            duration = args.duration if args.duration is not None else max(args.timeout / 1000.0, 1.0)
            stream = camera.stream(
                width=args.width,
                height=args.height,
                codec=args.codec,
                decoder=args.decoder,
                frame_rate=frame_rate,
                strict_fps=args.strict_fps,
                skip_initial=max(0, args.skip_frames),
                queue_size=2,
                timeout_ms=max(args.timeout, 1000),
                duration=duration,
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
