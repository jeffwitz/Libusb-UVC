#!/usr/bin/env python3
"""Live preview of two UVC cameras with timestamp-based synchronization."""

from __future__ import annotations

import argparse
import logging
import queue
import threading
from typing import Optional

import cv2
import numpy as np

from uvc_cli import ensure_repo_import, parse_device_id

ensure_repo_import()

import usb.util

from libusb_uvc import (  # type: ignore  # pylint: disable=wrong-import-position
    CodecPreference,
    UVCCamera,
    UVCError,
    describe_device,
    find_uvc_devices,
)

LOG = logging.getLogger("stereo_preview")


def frame_producer(
    camera: UVCCamera,
    args: argparse.Namespace,
    output: queue.Queue,
    stop_event: threading.Event,
) -> None:
    """Continuously grab frames from *camera* and push them into *output*."""

    try:
        stream = camera.stream(
            width=args.width,
            height=args.height,
            codec=args.codec,
            frame_rate=args.fps if args.fps > 0 else None,
            queue_size=4,
        )
        with stream as frames:
            for frame in frames:
                if stop_event.is_set():
                    break
                try:
                    output.put_nowait(frame)
                except queue.Full:
                    try:
                        output.get_nowait()
                    except queue.Empty:
                        pass
                    output.put_nowait(frame)
    except UVCError as exc:
        LOG.error("Frame producer error on %s: %s", describe_device(camera.device), exc)
    finally:
        stop_event.set()
        LOG.info("Producer for %s stopped", describe_device(camera.device))


def main() -> int:
    parser = argparse.ArgumentParser(description="Timestamp-synchronised stereo preview")
    parser.add_argument("--left-index", type=int, default=0, help="Device index of the left camera")
    parser.add_argument("--right-index", type=int, default=1, help="Device index of the right camera")
    parser.add_argument("--device-id", help="VID:PID shared by both cameras (hex or decimal)")
    parser.add_argument("--left-device-sn", help="Serial number of the left camera")
    parser.add_argument("--right-device-sn", help="Serial number of the right camera")
    parser.add_argument("--interface", type=int, default=1, help="UVC interface number to claim")
    parser.add_argument("--width", type=int, default=640, help="Frame width")
    parser.add_argument("--height", type=int, default=480, help="Frame height")
    parser.add_argument("--fps", type=float, default=15.0, help="Expected frame rate")
    codec_choices = ["auto", "yuyv", "mjpeg", "frame_based", "h264", "h265"]
    parser.add_argument(
        "--codec",
        default="mjpeg",
        choices=codec_choices,
        help="Codec to request on both cameras",
    )
    parser.add_argument("--max-ts-diff", type=float, default=0.033, help="Max timestamp delta (s) for pairing")
    parser.add_argument("--print-deltas", action="store_true", help="Print timestamp deltas for each pair")
    parser.add_argument("--display", action="store_true", help="Show OpenCV window with stereo preview")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    logging.basicConfig(level=args.log_level.upper())
    args.codec = getattr(CodecPreference, args.codec.upper())

    left_cam = None
    right_cam = None
    try:
        if args.device_id:
            vid, pid = parse_device_id(args.device_id)
            left_cam = _open_camera_filtered(vid, pid, args.left_device_sn, args.interface, label="left")
            right_cam = _open_camera_filtered(vid, pid, args.right_device_sn, args.interface, label="right")
        else:
            left_cam = UVCCamera.open(device_index=args.left_index)
            right_cam = UVCCamera.open(device_index=args.right_index)
    except UVCError as exc:
        LOG.error("Unable to open cameras: %s", exc)
        return 1

    LOG.info("Left : %s", describe_device(left_cam.device))
    LOG.info("Right: %s", describe_device(right_cam.device))

    left_queue: queue.Queue = queue.Queue(maxsize=2)
    right_queue: queue.Queue = queue.Queue(maxsize=2)
    stop_event = threading.Event()

    left_thread = threading.Thread(
        target=frame_producer,
        args=(left_cam, args, left_queue, stop_event),
        name="left-producer",
        daemon=True,
    )
    right_thread = threading.Thread(
        target=frame_producer,
        args=(right_cam, args, right_queue, stop_event),
        name="right-producer",
        daemon=True,
    )
    left_thread.start()
    right_thread.start()

    if args.display:
        cv2.namedWindow("stereo", cv2.WINDOW_NORMAL)

    left_frame = None
    right_frame = None
    frame_period_ms = (1000.0 / args.fps) if args.fps > 0 else None
    left_drops = 0
    right_drops = 0

    try:
        while not stop_event.is_set():
            if left_frame is None:
                try:
                    left_frame = left_queue.get(timeout=0.1)
                except queue.Empty:
                    if not left_thread.is_alive():
                        break
            if right_frame is None:
                try:
                    right_frame = right_queue.get(timeout=0.1)
                except queue.Empty:
                    if not right_thread.is_alive():
                        break

            if left_frame is None or right_frame is None:
                continue

            delta = left_frame.timestamp - right_frame.timestamp

            if abs(delta) <= args.max_ts_diff:
                if args.print_deltas:
                    hw_offset = None
                    if frame_period_ms is not None:
                        hw_offset = (left_drops - right_drops) * frame_period_ms
                    msg = f"Δ={delta*1000:.3f} ms (ts L={left_frame.timestamp:.6f}s R={right_frame.timestamp:.6f}s)"
                    if hw_offset is not None:
                        msg += f" | offset≈{hw_offset:.3f} ms"
                    print(msg)
                if args.display:
                    try:
                        left_bgr = left_frame.to_bgr()
                        right_bgr = right_frame.to_bgr()
                    except RuntimeError as exc:  # numpy/cv2 errors
                        LOG.warning("Conversion failed: %s", exc)
                        left_frame = None
                        right_frame = None
                        continue
                    stereo = np.hstack((left_bgr, right_bgr))
                    cv2.imshow("stereo", stereo)
                left_frame = None
                right_frame = None
            elif delta > 0:
                left_frame = None
                left_drops += 1
            else:
                right_frame = None
                right_drops += 1

            if args.display:
                key = cv2.waitKey(1) & 0xFF
                if key in (ord("q"), 27):
                    break
    except KeyboardInterrupt:
        LOG.info("Interrupted by user")
    finally:
        stop_event.set()
        left_thread.join(timeout=1)
        right_thread.join(timeout=1)
        if left_cam:
            left_cam.close()
        if right_cam:
            right_cam.close()
        if args.display:
            cv2.destroyAllWindows()

    return 0


def _open_camera_filtered(vid: int, pid: int, serial: Optional[str], interface: int, label: str) -> UVCCamera:
    if not serial:
        raise UVCError(f"--{label}-device-sn is required when --device-id is provided")
    devices = find_uvc_devices(vid, pid)
    if not devices:
        raise UVCError(f"No cameras found for VID:PID {vid:04x}:{pid:04x}")
    target_index = None
    for idx, dev in enumerate(devices):
        device_serial = None
        try:
            if dev.iSerialNumber:
                device_serial = usb.util.get_string(dev, dev.iSerialNumber)
        except Exception:
            device_serial = None
        if device_serial == serial:
            target_index = idx
            break
    if target_index is None:
        raise UVCError(f"No camera with serial {serial} for VID:PID {vid:04x}:{pid:04x}")
    return UVCCamera.open(vid=vid, pid=pid, device_index=target_index, interface=interface)


if __name__ == "__main__":
    raise SystemExit(main())
