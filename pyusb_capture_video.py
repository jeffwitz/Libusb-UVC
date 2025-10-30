#!/usr/bin/env python3
"""Live preview of a YUYV UVC stream using OpenCV."""

from __future__ import annotations

import argparse
import contextlib
import logging
import os
import threading
import time
from typing import Optional

import cv2
import usb.core

from uvc_usb import (
    UVCCamera,
    MJPEGPreviewPipeline,
    CodecPreference,
    UVCPacketAssembler,
    find_uvc_devices,
    list_streaming_interfaces,
    resolve_stream_preference,
    decode_to_rgb,
    describe_device,
    UVCError,
    StreamingInterface,
)

LOG = logging.getLogger(__name__)


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
    parser = argparse.ArgumentParser(description="Live YUYV preview via libusb1 + OpenCV")
    parser.add_argument("--vid", type=lambda x: int(x, 0), help="Vendor ID filter")
    parser.add_argument("--pid", type=lambda x: int(x, 0), help="Product ID filter")
    parser.add_argument("--device-index", type=int, default=0, help="Index within the filtered device list")
    parser.add_argument("--interface", type=int, default=1, help="Video streaming interface number")
    parser.add_argument("--width", type=int, default=640, help="Desired frame width")
    parser.add_argument("--height", type=int, default=480, help="Desired frame height")
    parser.add_argument("--fps", type=float, default=15.0, help="Target frame rate in Hz")
    parser.add_argument("--skip-frames", type=int, default=2, help="Number of frames to discard before display")
    parser.add_argument("--timeout", type=int, default=5000, help="Maximum capture timeout in ms")
    parser.add_argument(
        "--codec",
        choices=[CodecPreference.AUTO, CodecPreference.YUYV, CodecPreference.MJPEG],
        default=CodecPreference.AUTO,
        help="Force a specific codec when multiple are available",
    )
    parser.add_argument(
        "--strict-fps",
        action="store_true",
        help="Require the requested fps to match an advertised frame interval exactly",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List the streaming formats/frames for the selected interface and exit",
    )
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    logging.basicConfig(level=args.log_level.upper())

    devices = find_uvc_devices(args.vid, args.pid)
    if not devices:
        print("No matching UVC devices.")
        return 1
    if not (0 <= args.device_index < len(devices)):
        print(f"Device index {args.device_index} out of range (found {len(devices)} devices)")
        return 1

    dev = devices[args.device_index]
    print(f"Using device: {describe_device(dev)}")
    interfaces = list_streaming_interfaces(dev)
    if args.interface not in interfaces:
        print(f"Interface {args.interface} is not a streaming interface")
        return 1

    streaming = interfaces[args.interface]

    if args.list:
        print_streaming_modes(streaming)
        return 0
    if not streaming.formats:
        print("Selected interface does not expose streaming formats")
        return 1

    try:
        stream_format, frame = resolve_stream_preference(
            streaming,
            args.width,
            args.height,
            codec=args.codec,
        )
    except UVCError as exc:
        print(exc)
        return 1
    is_mjpeg = stream_format.description.upper() in {"MJPEG", "MJPG"}
    expected_size = None if is_mjpeg else (frame.max_frame_size or (frame.width * frame.height * 2))

    assembler = UVCPacketAssembler(expected_size=expected_size)
    frames_to_skip = max(0, args.skip_frames)
    latest_frame: Optional[bytes] = None
    latest_ts = 0.0
    frame_event = threading.Event()
    frame_lock = threading.Lock()
    running = True

    def on_packet(packet: bytes) -> None:
        nonlocal frames_to_skip, latest_frame, latest_ts, running
        if not running or not packet:
            return

        for result in assembler.submit(packet):
            LOG.debug(
                "Finalized frame reason=%s size=%s expected=%s error=%s",
                result.reason,
                result.size,
                result.expected_size,
                result.error,
            )
            if not result.complete:
                continue
            if frames_to_skip > 0:
                frames_to_skip -= 1
                LOG.debug("Skipping frame (reason=%s) remaining=%s", result.reason, frames_to_skip)
                continue
            with frame_lock:
                latest_frame = result.payload
                latest_ts = time.time()
            frame_event.set()
            LOG.debug("Accepted frame (reason=%s) size=%s", result.reason, result.size)

    frames_displayed = 0
    fps_epoch_start = time.time()

    try:
        with UVCCamera.from_device(dev, args.interface) as camera:
            negotiation = camera.configure_stream(
                stream_format,
                frame,
                frame_rate=args.fps,
                alt_setting=None,
                strict_fps=args.strict_fps,
            )
            print(
                "Negotiated frame size: {size} bytes\nNegotiated payload size: {payload} bytes\nUsing alt setting {alt} (packet {packet} bytes, endpoint 0x{ep:02x})".format(
                    size=negotiation.get("dwMaxVideoFrameSize", expected_size),
                    payload=negotiation.get("dwMaxPayloadTransferSize", "n/a"),
                    alt=negotiation.get("selected_alt", camera.active_alt_setting),
                    packet=negotiation.get("iso_packet_size", camera.max_payload_size),
                    ep=negotiation.get("endpoint_address", camera.endpoint_address or 0),
                )
            )

            preview = None
            if is_mjpeg:
                try:
                    preview = MJPEGPreviewPipeline(args.fps)
                except RuntimeError as exc:
                    print(exc)
                    return 1

            camera.start_async_stream(
                on_packet,
                transfers=16,
                packets_per_transfer=64,
                timeout_ms=max(args.timeout, 2000),
            )

            window = None
            if not is_mjpeg:
                window = "pyusb_yuyv_preview"
                cv2.namedWindow(window, cv2.WINDOW_NORMAL)

            poll_stop = threading.Event()
            def _usb_poll_loop() -> None:
                while not poll_stop.is_set():
                    camera.poll_async_events(0.05)

            poll_thread = threading.Thread(target=_usb_poll_loop, name="uvc-usb-poll", daemon=True)
            poll_thread.start()

            try:
                while True:
                    if not frame_event.wait(timeout=0.05):
                        continue
                    frame_event.clear()
                    with frame_lock:
                        payload = latest_frame
                        frame_ts = latest_ts
                    if payload is None:
                        continue

                    if is_mjpeg and preview is not None:
                        preview.push(payload, frame_ts)
                    else:
                        try:
                            rgb = decode_to_rgb(payload, stream_format, frame)
                        except RuntimeError as exc:
                            LOG.warning("Conversion failed: %s", exc)
                            continue
                        bgr = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
                        cv2.imshow(window, bgr)
                        if cv2.waitKey(20) & 0xFF in (ord("q"), 27):  # ESC or q
                            break

                    frames_displayed += 1
                    now = time.time()
                    if now - fps_epoch_start >= 3.0:
                        fps_measured = frames_displayed / (now - fps_epoch_start)
                        print(
                            f"Average FPS over last {now - fps_epoch_start:.1f}s: {fps_measured:.2f}"
                        )
                        fps_epoch_start = now
                        frames_displayed = 0
            except KeyboardInterrupt:
                LOG.debug("Capture interrupted by user")
            finally:
                poll_stop.set()
                poll_thread.join(timeout=0.5)
                running = False
                camera.stop_async_stream()
                if not is_mjpeg:
                    cv2.destroyAllWindows()
                if preview is not None:
                    preview.close()
    except (UVCError, usb.core.USBError) as exc:
        print(f"Failed to start stream: {exc}")
        return 1

    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
