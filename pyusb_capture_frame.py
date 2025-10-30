#!/usr/bin/env python3
"""Capture a single frame from a UVC camera and save it as an image file."""

from __future__ import annotations

import argparse
import logging
import threading
import time
from pathlib import Path
from typing import Optional

import usb.core
from PIL import Image

from uvc_usb import (
    UVCCamera,
    CodecPreference,
    find_uvc_devices,
    list_streaming_interfaces,
    resolve_stream_preference,
    describe_device,
    UVCError,
    StreamFormat,
    FrameInfo,
    decode_to_rgb,
    VS_FORMAT_UNCOMPRESSED,
    VS_FORMAT_MJPEG,
)

LOG = logging.getLogger(__name__)


def save_frame(
    output_path: Path, payload: bytes, stream_format: StreamFormat, frame: FrameInfo
):
    """Saves the captured frame to a file, converting it if necessary."""
    output_suffix = output_path.suffix.lower()

    print(f"Saving frame to {output_path}...")

    # Case 1: Stream is MJPEG
    if stream_format.subtype == VS_FORMAT_MJPEG:
        if output_suffix in (".jpg", ".jpeg"):
            # Payload is already a complete JPEG file
            output_path.write_bytes(payload)
            print("Payload is MJPEG, saved directly.")
        else:
            # Convert MJPEG to the desired format (e.g., TIFF)
            try:
                import cv2
                import numpy as np
                arr = np.frombuffer(payload, dtype=np.uint8)
                bgr = cv2.imdecode(arr, cv2.IMREAD_COLOR)
                if bgr is None:
                    raise RuntimeError("Failed to decode MJPEG frame with OpenCV")
                # Pillow needs RGB
                rgb_array = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
                img = Image.fromarray(rgb_array)
                img.save(output_path)
                print(f"Converted MJPEG stream to {output_path.suffix.upper()}.")
            except ImportError:
                print("Warning: OpenCV is required to convert MJPEG to other formats. Saving as raw.")
                output_path.with_suffix(".raw").write_bytes(payload)
            except Exception as e:
                print(f"Error converting MJPEG: {e}. Saving as raw.")
                output_path.with_suffix(".raw").write_bytes(payload)
        return

    # Case 2: Stream is Uncompressed (YUYV)
    if stream_format.subtype == VS_FORMAT_UNCOMPRESSED:
        if output_suffix in (".jpg", ".jpeg", ".tiff", ".tif", ".png"):
            try:
                # Convert YUYV payload to an RGB numpy array
                rgb_array = decode_to_rgb(payload, stream_format, frame)
                # Create a Pillow image from the array
                img = Image.fromarray(rgb_array)
                # Save the image in the desired format
                img.save(output_path)
                print(f"Converted YUYV frame to {output_path.suffix.upper()}.")
            except Exception as e:
                print(f"Error during YUYV conversion: {e}. Saving as raw instead.")
                output_path.with_suffix(".raw").write_bytes(payload)
        else:
            # Default to saving RAW if extension is unknown
            output_path.write_bytes(payload)
            print("Output format not specified for conversion, saved as raw payload.")
        return

    # Case 3: Other formats or fallback
    print("Format is not directly convertible, saving as raw payload.")
    output_path.write_bytes(payload)


def main() -> int:
    parser = argparse.ArgumentParser(description="Capture one frame from a UVC camera.")
    # --- Device Selection Arguments ---
    parser.add_argument("--vid", type=lambda x: int(x, 0), help="Vendor ID filter")
    parser.add_argument("--pid", type=lambda x: int(x, 0), help="Product ID filter")
    parser.add_argument("--device-index", type=int, default=0, help="Index of the matching device to use")
    parser.add_argument("--interface", type=int, default=1, help="Video streaming interface number")

    # --- Stream Configuration Arguments ---
    parser.add_argument("--width", type=int, required=True, help="Desired frame width")
    parser.add_argument("--height", type=int, required=True, help="Desired frame height")
    parser.add_argument("--fps", type=float, default=15.0, help="Target frame rate in Hz for stream negotiation")
    parser.add_argument(
        "--codec",
        choices=[CodecPreference.AUTO, CodecPreference.YUYV, CodecPreference.MJPEG],
        default=CodecPreference.AUTO,
        help="Preferred codec if multiple are available for the resolution",
    )

    # --- Capture Control Arguments ---
    parser.add_argument("--skip-frames", type=int, default=2, help="Number of frames to discard before saving (for sensor warmup)")
    parser.add_argument("--timeout", type=int, default=5000, help="Total capture timeout in milliseconds")
    parser.add_argument("--output", type=Path, required=True, help="Destination file for the image (e.g., frame.jpg, frame.tiff, frame.raw)")
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

    captured_payload = None
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

            captured_payload = capture_single_frame(
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

    if captured_payload is None:
        print("Timed out or failed to capture a complete frame.")
        return 1

    try:
        save_frame(args.output, captured_payload, stream_format_out, frame_out)
    except Exception as exc:
        print(f"Failed to save output file {args.output}: {exc}")
        return 1

    return 0


def capture_single_frame(
    camera: UVCCamera,
    stream_format: StreamFormat,
    frame: FrameInfo,
    *,
    fps: float,
    skip_frames: int,
    timeout_ms: int,
) -> Optional[bytes]:
    """Uses the async API to capture a single valid frame without extra threads."""

    is_mjpeg = stream_format.subtype == VS_FORMAT_MJPEG
    expected_size = None if is_mjpeg else (frame.max_frame_size or (frame.width * frame.height * 2))

    frame_bytes = bytearray()
    frame_error = False
    current_fid: Optional[int] = None
    frames_to_skip = max(0, skip_frames)

    captured_frame: Optional[bytes] = None
    # Use a simple list as a mutable flag that the callback can modify
    capture_complete_flag = []

    def finalize_frame(reason: str) -> None:
        nonlocal frames_to_skip, frame_error, current_fid, captured_frame
        if current_fid is None or capture_complete_flag:
            return

        size_ok = (
            not frame_error
            and ((expected_size is None and len(frame_bytes) > 0) or
                 (expected_size is not None and len(frame_bytes) == expected_size))
        )

        if size_ok:
            if frames_to_skip > 0:
                frames_to_skip -= 1
                LOG.debug(f"Skipping frame (reason={reason}), remaining={frames_to_skip}")
            else:
                LOG.info(f"Captured complete frame (reason={reason}, size={len(frame_bytes)})")
                captured_frame = bytes(frame_bytes)
                capture_complete_flag.append(True)
        else:
            LOG.debug(f"Dropping incomplete frame (reason={reason}, error={frame_error}, size={len(frame_bytes)})")

        frame_bytes.clear()
        frame_error = False
        current_fid = None

    def on_packet(packet: bytes) -> None:
        nonlocal current_fid, frame_error
        if not packet or capture_complete_flag:
            return

        header_len = packet[0]
        if header_len < 2 or header_len > len(packet):
            frame_error = True
            return

        flags = packet[1]
        payload = packet[header_len:]
        fid = flags & 0x01
        eof = bool(flags & 0x02)
        err = bool(flags & 0x40)

        if err:
            frame_error = True

        if current_fid is None:
            current_fid = fid
            frame_bytes.clear()
            frame_error = bool(err)
        elif fid != current_fid:
            finalize_frame("fid-toggle")
            current_fid = fid
            frame_bytes.clear()
            frame_error = bool(err)

        if payload:
            frame_bytes.extend(payload)

        if expected_size is not None and len(frame_bytes) > expected_size:
            frame_error = True

        if eof:
            finalize_frame("eof")

    camera.configure_stream(stream_format, frame, frame_rate=fps)

    camera.start_async_stream(
        on_packet,
        transfers=16,
        packets_per_transfer=64,
        timeout_ms=max(timeout_ms, 2000),
    )

    final_frame = None
    try:
        deadline = time.time() + (timeout_ms / 1000.0)
        # Main thread does the polling
        while not capture_complete_flag and time.time() < deadline:
            camera.poll_async_events(0.01)

        if capture_complete_flag:
            final_frame = captured_frame
        else:
            LOG.warning("Timeout reached while waiting for a complete frame.")

    finally:
        camera.stop_async_stream()

    return final_frame


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
