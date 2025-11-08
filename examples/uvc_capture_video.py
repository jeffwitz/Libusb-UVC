#!/usr/bin/env python3
"""Live preview of a UVC stream using the high-level helpers."""

from __future__ import annotations

import argparse
import logging
import time

import cv2

from uvc_cli import add_device_arguments, apply_device_filters, configure_logging, ensure_repo_import, resolve_device_index

ensure_repo_import()
from libusb_uvc import (
    CodecPreference,
    DecoderPreference,
    StreamingInterface,
    UVCCamera,
    UVCError,
    describe_device,
)

LOG = logging.getLogger("capture_video")


def _format_fps_list(frame) -> str:
    fps_values = frame.intervals_hz()
    if not fps_values:
        return "-"
    return ", ".join(f"{fps:.2f}" for fps in fps_values)


def print_streaming_modes(streaming: StreamingInterface) -> None:
    print(f"Streaming interface {streaming.interface_number}")
    print("Formats:")
    if not streaming.formats:
        print("  (no formats advertised)")
    for fmt in streaming.formats:
        print(f"  Format {fmt.format_index}: {fmt.description}")
        for frame in fmt.frames:
            fps_summary = _format_fps_list(frame)
            print(
                f"    Frame {frame.frame_index}: {frame.width}x{frame.height} | "
                f"Max {frame.max_frame_size} bytes | FPS {fps_summary}"
            )

    print("Alternate settings:")
    if not streaming.alt_settings:
        print("  (no alternate settings)")
    for alt in streaming.alt_settings:
        endpoint = f"0x{alt.endpoint_address:02x}" if alt.endpoint_address is not None else "-"
        attrs = f"0x{alt.endpoint_attributes:02x}" if alt.endpoint_attributes is not None else "-"
        print(
            f"  Alt {alt.alternate_setting}: endpoint={endpoint} attrs={attrs} "
            f"max-packet={alt.max_packet_size}"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="Live preview via libusb_uvc + OpenCV")
    add_device_arguments(parser)
    parser.add_argument("--width", type=int, default=640, help="Desired frame width")
    parser.add_argument("--height", type=int, default=480, help="Desired frame height")
    parser.add_argument("--fps", type=float, default=15.0, help="Target frame rate in Hz")
    parser.add_argument("--skip-frames", type=int, default=2, help="Frames to discard before display")
    parser.add_argument("--timeout", type=int, default=3000, help="Async transfer timeout (ms)")
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
        help="Force a specific codec when multiple are available",
    )
    parser.add_argument(
        "--decoder",
        choices=[
            DecoderPreference.AUTO,
            DecoderPreference.NONE,
            DecoderPreference.PYAV,
            DecoderPreference.GSTREAMER,
        ],
        default=DecoderPreference.AUTO,
        help="Select a decoder backend for frame-based formats (experimental)",
    )
    parser.add_argument("--strict-fps", action="store_true", help="Require exact FPS match during PROBE")
    parser.add_argument("--duration", type=float, help="Automatically stop preview after the given seconds")
    parser.add_argument("--list", action="store_true", help="List formats for the interface and exit")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()
    apply_device_filters(args)
    resolve_device_index(args)

    configure_logging(args.log_level, name="capture_video")

    try:
        with UVCCamera.open(
            vid=args.vid,
            pid=args.pid,
            device_index=args.device_index,
            interface=args.interface,
        ) as camera:
            print(f"Using device: {describe_device(camera.device)}")

            if args.list:
                print_streaming_modes(camera.interface)
                return 0

            frame_rate = args.fps if args.fps > 0 else None

            stream = camera.stream(
                width=args.width,
                height=args.height,
                codec=args.codec,
                decoder=args.decoder,
                frame_rate=frame_rate,
                strict_fps=args.strict_fps,
                queue_size=6,
                skip_initial=max(0, args.skip_frames),
                timeout_ms=max(args.timeout, 1000),
                duration=args.duration,
            )

            with stream as frames:
                start = time.time()
                window = "libusb_uvc_preview"
                try:
                    cv2.namedWindow(window, cv2.WINDOW_NORMAL)
                except cv2.error as exc:
                    LOG.error("OpenCV window creation failed: %s", exc)
                    return 1

                try:
                    for frame in frames:
                        try:
                            bgr = frame.to_bgr()
                        except RuntimeError as exc:
                            LOG.warning("Frame conversion failed: %s", exc)
                            continue

                        cv2.imshow(window, bgr)
                        key = cv2.waitKey(1) & 0xFF
                        if key in (ord("q"), 27):
                            break
                        if args.duration and (time.time() - start) >= args.duration:
                            break
                except KeyboardInterrupt:
                    LOG.info("Capture interrupted after %.2fs", time.time() - start)
                finally:
                    cv2.destroyWindow(window)

    except UVCError as exc:
        print(f"Failed to start stream: {exc}")
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
