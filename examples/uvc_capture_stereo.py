#!/usr/bin/env python3
"""Stereo preview with host-timestamp synchronisation."""

from __future__ import annotations

import argparse
import logging
import queue
import threading
import time
from dataclasses import dataclass
from typing import Optional, Tuple

import numpy as np
import usb.util

try:  # Optional preview dependency
    import cv2
except Exception:  # pragma: no cover - optional feature
    cv2 = None

from uvc_cli import ensure_repo_import, parse_device_id

ensure_repo_import()

from libusb_uvc import (  # type: ignore  # pylint: disable=wrong-import-position
    CodecPreference,
    DecoderPreference,
    UVCCamera,
    UVCError,
    describe_device,
    find_uvc_devices,
)

LOG = logging.getLogger("stereo_preview")


@dataclass
class TimestampedFrame:
    """Container for a captured frame and its timestamps."""

    frame: object
    host_timestamp: float
    device_timestamp: Optional[float]
    raw_device_timestamp: Optional[float]


def _normalise_codec(value: str) -> CodecPreference:
    token = value.strip().replace("-", "_").upper()
    return getattr(CodecPreference, token)


def _normalise_decoder(value: str) -> DecoderPreference:
    token = value.strip().upper()
    return getattr(DecoderPreference, token)


def frame_producer(
    camera: UVCCamera,
    args: argparse.Namespace,
    output: queue.Queue,
    stop_event: threading.Event,
    label: str,
) -> None:
    """Continuously grab frames from *camera* and push them into *output*."""

    try:
        stream = camera.stream(
            width=args.width,
            height=args.height,
            codec=args.codec,
            decoder=args.decoder,
            frame_rate=args.fps if args.fps > 0 else None,
            queue_size=args.stream_queue,
        )
        device_epoch = None
        with stream as frames:
            for frame in frames:
                if stop_event.is_set():
                    break
                host_ts = time.monotonic()
                raw_pts = getattr(frame, "timestamp", None)
                norm_pts = None
                if raw_pts is not None:
                    if device_epoch is None:
                        device_epoch = raw_pts
                    norm_pts = raw_pts - device_epoch
                wrapped = TimestampedFrame(
                    frame=frame,
                    host_timestamp=host_ts,
                    device_timestamp=norm_pts,
                    raw_device_timestamp=raw_pts,
                )
                try:
                    output.put_nowait(wrapped)
                except queue.Full:
                    try:
                        output.get_nowait()
                    except queue.Empty:
                        pass
                    output.put_nowait(wrapped)
    except UVCError as exc:
        LOG.error("Frame producer error on %s: %s", label, exc)
    finally:
        stop_event.set()
        LOG.info("Producer for %s stopped", label)


def _open_camera_filtered(
    vid: int,
    pid: int,
    serial: str,
    interface: int,
    label: str,
) -> UVCCamera:
    devices = find_uvc_devices(vid, pid)
    if not devices:
        raise UVCError(f"No cameras found for VID:PID {vid:04x}:{pid:04x}")
    target_index = None
    for idx, dev in enumerate(devices):
        device_serial = None
        try:
            if dev.iSerialNumber:
                device_serial = usb.util.get_string(dev, dev.iSerialNumber)
        except Exception:  # pragma: no cover - libusb quirks
            device_serial = None
        if device_serial == serial:
            target_index = idx
            break
    if target_index is None:
        raise UVCError(f"No camera with serial {serial} for VID:PID {vid:04x}:{pid:04x}")
    return UVCCamera.open(vid=vid, pid=pid, device_index=target_index, interface=interface)


def _open_camera(args: argparse.Namespace, label: str) -> UVCCamera:
    if args.device_id:
        vid, pid = args.device_id
        serial = getattr(args, f"{label}_device_sn")
        if not serial:
            raise UVCError(f"--{label}-device-sn is required when --device-id is provided")
        return _open_camera_filtered(vid, pid, serial, args.interface, label=label)
    index = getattr(args, f"{label}_index")
    return UVCCamera.open(device_index=index, interface=args.interface)


def _parse_args() -> argparse.Namespace:
    codec_choices = ["auto", "yuyv", "mjpeg", "frame_based", "h264", "h265"]
    decoder_choices = ["auto", "none", "pyav", "gstreamer"]
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
    parser.add_argument(
        "--codec",
        default="mjpeg",
        choices=codec_choices,
        help="Codec to request on both cameras",
    )
    parser.add_argument(
        "--decoder",
        default="auto",
        choices=decoder_choices,
        help="Decoder selection for compressed payloads",
    )
    parser.add_argument("--stream-queue", type=int, default=4, help="Internal queue size inside UVCCamera.stream")
    parser.add_argument("--queue-size", type=int, default=3, help="Max buffered frames per eye")
    parser.add_argument(
        "--sync-window-ms",
        type=float,
        default=2.0,
        help="Maximum delta (ms) used during start-up alignment",
    )
    parser.add_argument(
        "--max-runtime-delta-ms",
        type=float,
        default=0.0,
        help="Optional delta cap after calibration (0 disables runtime enforcement)",
    )
    parser.add_argument(
        "--drop-window-ms",
        type=float,
        default=0.0,
        help="Discard frames older than this window (0 disables)",
    )
    parser.add_argument("--poll-timeout-ms", type=float, default=5.0, help="Max wait for an empty queue (milliseconds)")
    parser.add_argument("--print-deltas", action="store_true", help="Print timestamp deltas for each pair")
    parser.add_argument("--display", action="store_true", help="Show OpenCV window with stereo preview")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    logging.basicConfig(level=args.log_level.upper())

    if args.device_id:
        args.device_id = parse_device_id(args.device_id)

    args.codec = _normalise_codec(args.codec)
    args.decoder = _normalise_decoder(args.decoder)
    args.poll_timeout = max(args.poll_timeout_ms / 1000.0, 0.001)
    args.sync_window = max(args.sync_window_ms / 1000.0, 0.0)
    args.runtime_window = max(args.max_runtime_delta_ms / 1000.0, 0.0)
    args.drop_window = args.drop_window_ms / 1000.0 if args.drop_window_ms > 0 else None
    return args


def _consume_latest_frame(
    current: Optional[TimestampedFrame],
    source: queue.Queue,
    poll_timeout: float,
) -> Optional[TimestampedFrame]:
    """Return the newest available frame from *source*, keeping latency minimal."""

    item = current
    if item is None:
        try:
            item = source.get(timeout=poll_timeout)
        except queue.Empty:
            return None

    while True:
        try:
            item = source.get_nowait()
        except queue.Empty:
            break
    return item


def _fetch_frame(
    current: Optional[TimestampedFrame],
    source: queue.Queue,
    poll_timeout: float,
    drain: bool,
) -> Optional[TimestampedFrame]:
    """Return next frame, draining backlog only when *drain* is True."""

    if drain:
        return _consume_latest_frame(current, source, poll_timeout)

    if current is not None:
        return current
    try:
        return source.get(timeout=poll_timeout)
    except queue.Empty:
        return None


def main() -> int:
    args = _parse_args()

    try:
        left_cam = _open_camera(args, "left")
        right_cam = _open_camera(args, "right")
    except UVCError as exc:
        LOG.error("Unable to open cameras: %s", exc)
        return 1

    LOG.info("Left : %s", describe_device(left_cam.device))
    LOG.info("Right: %s", describe_device(right_cam.device))

    left_queue: queue.Queue = queue.Queue(maxsize=args.queue_size)
    right_queue: queue.Queue = queue.Queue(maxsize=args.queue_size)
    stop_event = threading.Event()

    left_thread = threading.Thread(
        target=frame_producer,
        args=(left_cam, args, left_queue, stop_event, "left"),
        name="left-producer",
        daemon=True,
    )
    right_thread = threading.Thread(
        target=frame_producer,
        args=(right_cam, args, right_queue, stop_event, "right"),
        name="right-producer",
        daemon=True,
    )
    left_thread.start()
    right_thread.start()

    if args.display:
        if cv2 is None:
            raise RuntimeError("OpenCV is required when --display is specified")
        cv2.namedWindow("stereo", cv2.WINDOW_NORMAL)

    left_frame: Optional[TimestampedFrame] = None
    right_frame: Optional[TimestampedFrame] = None
    calibrated = False

    try:
        while not stop_event.is_set():
            left_frame = _fetch_frame(left_frame, left_queue, args.poll_timeout, drain=not calibrated)
            right_frame = _fetch_frame(right_frame, right_queue, args.poll_timeout, drain=not calibrated)

            now = time.monotonic()
            if args.drop_window:
                if left_frame and now - left_frame.host_timestamp > args.drop_window:
                    left_frame = None
                if right_frame and now - right_frame.host_timestamp > args.drop_window:
                    right_frame = None

            if left_frame is None or right_frame is None:
                if not left_thread.is_alive() or not right_thread.is_alive():
                    break
                continue

            delta = left_frame.host_timestamp - right_frame.host_timestamp
            if not calibrated and args.sync_window and abs(delta) > args.sync_window:
                if left_frame.host_timestamp < right_frame.host_timestamp:
                    left_frame = None
                else:
                    right_frame = None
                continue

            if not calibrated:
                calibrated = True
                LOG.info("Calibration locked (Δ=%.3f ms)", delta * 1000)
            elif args.runtime_window and abs(delta) > args.runtime_window:
                if left_frame.host_timestamp < right_frame.host_timestamp:
                    left_frame = None
                else:
                    right_frame = None
                continue

            if args.print_deltas:
                pts_info = ""
                if left_frame.device_timestamp is not None and right_frame.device_timestamp is not None:
                    pts_info = (
                        f"(PTS L={left_frame.device_timestamp:.6f}s "
                        f"R={right_frame.device_timestamp:.6f}s)"
                    )
                print(
                    f"Δ={delta*1000:+.3f} ms "
                    f"(host L={left_frame.host_timestamp:.6f}s R={right_frame.host_timestamp:.6f}s) "
                    f"{pts_info}"
                )
            if args.display:
                try:
                    left_bgr = left_frame.frame.to_bgr()
                    right_bgr = right_frame.frame.to_bgr()
                except RuntimeError as exc:  # numpy/cv2 errors
                    LOG.warning("Conversion failed: %s", exc)
                    left_frame = None
                    right_frame = None
                    continue
                stereo = np.hstack((left_bgr, right_bgr))
                cv2.imshow("stereo", stereo)
                key = cv2.waitKey(1) & 0xFF
                if key in (ord("q"), 27):
                    break
            left_frame = None
            right_frame = None
    except KeyboardInterrupt:
        LOG.info("Interrupted by user")
    finally:
        stop_event.set()
        left_thread.join(timeout=1)
        right_thread.join(timeout=1)
        left_cam.close()
        right_cam.close()
        if args.display and cv2 is not None:
            cv2.destroyAllWindows()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
