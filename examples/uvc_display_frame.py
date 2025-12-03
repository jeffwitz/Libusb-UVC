#!/usr/bin/env python3
"""Capture a single frame using libusb_uvc and display/save it with Matplotlib."""

from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path

import matplotlib

if not os.environ.get("DISPLAY"):
    matplotlib.use("Agg")

import matplotlib.pyplot as plt

from uvc_cli import (
    add_device_arguments,
    add_streaming_arguments,
    apply_device_filters,
    configure_logging,
    ensure_repo_import,
    resolve_device_index,
)

ensure_repo_import()
from libusb_uvc import CodecPreference, DecoderPreference, UVCCamera, UVCError, decode_to_rgb, describe_device

LOG = logging.getLogger("display_frame")


def main() -> int:
    parser = argparse.ArgumentParser(description="Capture and display a single frame")
    add_device_arguments(parser)
    add_streaming_arguments(
        parser,
        width_default=640,
        height_default=480,
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
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()
    apply_device_filters(args)
    resolve_device_index(args)

    configure_logging(args.log_level, name="display_frame")

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
