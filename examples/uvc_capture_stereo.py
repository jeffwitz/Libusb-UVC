#!/usr/bin/env python3
"""Stereo capture variant with dynamic calibration and pairing modes."""

from __future__ import annotations

import argparse
import contextlib
import logging
import queue
import threading
import time
from dataclasses import dataclass
from numbers import Number
from typing import Optional

import numpy as np
import psutil
import usb.util

try:  # Optional dependency for preview
    import cv2
except Exception:  # pragma: no cover - OpenCV not always present
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

LOG = logging.getLogger("stereo_preview_v3")
PTS_TICK_HZ = 48_000_000.0


@dataclass
class FramePacket:
    frame: object
    host_ts: float
    pts: Optional[float]


def _normalise_codec(value: str) -> CodecPreference:
    token = value.strip().replace("-", "_").upper()
    return getattr(CodecPreference, token)


def _normalise_decoder(value: str) -> DecoderPreference:
    token = value.strip().upper()
    return getattr(DecoderPreference, token)


def _open_camera_filtered(vid: int, pid: int, serial: str, interface: int, label: str) -> UVCCamera:
    devices = find_uvc_devices(vid, pid)
    if not devices:
        raise UVCError(f"No cameras found for VID:PID {vid:04x}:{pid:04x}")
    for index, dev in enumerate(devices):
        device_serial = None
        try:
            if dev.iSerialNumber:
                device_serial = usb.util.get_string(dev, dev.iSerialNumber)
        except Exception:
            device_serial = None
        if device_serial == serial:
            return UVCCamera.open(vid=vid, pid=pid, device_index=index, interface=interface)
    raise UVCError(f"No camera with serial {serial} for VID:PID {vid:04x}:{pid:04x}")


def _select_camera(args: argparse.Namespace, label: str) -> UVCCamera:
    if args.device_id:
        vid, pid = args.device_id
        serial = getattr(args, f"{label}_device_sn")
        if not serial:
            raise UVCError(f"--{label}-device-sn is required when --device-id is provided")
        return _open_camera_filtered(vid, pid, serial, args.interface, label)
    index = getattr(args, f"{label}_index")
    return UVCCamera.open(device_index=index, interface=args.interface)


def _pts_to_seconds(value: Optional[Number]) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, (int, np.integer)):
        return float(value) / PTS_TICK_HZ
    return float(value)


def frame_producer(
    camera: UVCCamera,
    frame_queue: queue.Queue,
    stop_event: threading.Event,
    start_barrier: threading.Barrier,
    start_event: threading.Event,
    args: argparse.Namespace,
    label: str,
    core_id: Optional[int] = None,
    start_delay: float = 0.0,
) -> None:
    """Continuously capture frames and relay them to the consumer queue."""

    proc = psutil.Process()
    original_affinity = None
    try:
        if core_id is not None:
            try:
                original_affinity = proc.cpu_affinity()
                proc.cpu_affinity([core_id])
                LOG.info("%s pinned to CPU core %s", label, core_id)
            except (psutil.Error, ValueError) as exc:
                LOG.warning("Failed to set affinity for %s: %s", label, exc)

        stream = camera.stream(
            width=args.width,
            height=args.height,
            codec=args.codec,
            decoder=args.decoder,
            frame_rate=args.fps if args.fps > 0 else None,
            queue_size=args.stream_queue,
        )
        start_barrier.wait()
        start_event.wait()
        if start_delay > 0:
            LOG.info("%s delaying post-barrier start by %.3f ms", label, start_delay * 1000)
            time.sleep(start_delay)

        with stream as frames:
            for frame in frames:
                if stop_event.is_set():
                    break
                packet = FramePacket(
                    frame=frame,
                    host_ts=time.monotonic(),
                    pts=getattr(frame, "pts", getattr(frame, "timestamp", None)),
                )
                try:
                    frame_queue.put_nowait(packet)
                except queue.Full:
                    try:
                        frame_queue.get_nowait()
                    except queue.Empty:
                        pass
                    frame_queue.put_nowait(packet)
    except Exception as exc:  # pragma: no cover - diagnostic path
        LOG.exception("Producer %s failed: %s", label, exc)
    finally:
        stop_event.set()
        if original_affinity is not None:
            with contextlib.suppress(psutil.Error):
                proc.cpu_affinity(original_affinity)
        LOG.info("Producer %s stopped", label)


def _parse_args() -> argparse.Namespace:
    codec_choices = ["auto", "yuyv", "mjpeg", "frame_based", "h264", "h265"]
    decoder_choices = ["auto", "none", "pyav", "gstreamer"]
    parser = argparse.ArgumentParser(description="Stereo preview (dynamic pairing prototype)")
    parser.add_argument("--left-index", type=int, default=0, help="Device index of the left camera")
    parser.add_argument("--right-index", type=int, default=1, help="Device index of the right camera")
    parser.add_argument("--device-id", help="VID:PID shared by both cameras (hex or decimal)")
    parser.add_argument("--left-device-sn", help="Serial number of the left camera")
    parser.add_argument("--right-device-sn", help="Serial number of the right camera")
    parser.add_argument("--left-core", type=int, help="CPU core index for the left producer thread")
    parser.add_argument("--right-core", type=int, help="CPU core index for the right producer thread")
    parser.add_argument("--left-start-delay-ms", type=float, default=0.0, help="Startup delay for left (ms)")
    parser.add_argument("--right-start-delay-ms", type=float, default=0.0, help="Startup delay for right (ms)")
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
    parser.add_argument("--queue-size", type=int, default=3, help="Buffered frames per camera in the consumer")
    parser.add_argument("--max-ts-diff", type=float, default=0.020, help="Host delta tolerance during pairing (s)")
    parser.add_argument(
        "--pairing-mode",
        choices=["fifo", "latest"],
        default="latest",
        help="Queue consumption strategy",
    )
    parser.add_argument("--print-deltas", action="store_true", help="Print pairing deltas")
    parser.add_argument("--target-delta-ms", type=float, help="Expected steady-state host delta (ms)")
    parser.add_argument(
        "--calibration-pairs",
        type=int,
        default=20,
        help="Number of initial pairs to average before locking target delta (0 disables)",
    )
    parser.add_argument(
        "--stats-interval",
        type=int,
        default=0,
        help="Pairs between stats logs (0 disables)",
    )
    parser.add_argument("--display", action="store_true", help="Show OpenCV preview")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()
    logging.basicConfig(level=args.log_level.upper())

    if args.device_id:
        args.device_id = parse_device_id(args.device_id)

    args.codec = _normalise_codec(args.codec)
    args.decoder = _normalise_decoder(args.decoder)
    args.left_start_delay = max(args.left_start_delay_ms, 0.0) / 1000.0
    args.right_start_delay = max(args.right_start_delay_ms, 0.0) / 1000.0
    args.pairing_mode = args.pairing_mode.lower()
    args.target_delta = args.target_delta_ms / 1000.0 if args.target_delta_ms is not None else None
    args.calibration_pairs = max(args.calibration_pairs, 0)
    return args


def _drain_queue(
    current: Optional[FramePacket],
    source: queue.Queue,
    *,
    drain: bool,
) -> Optional[FramePacket]:
    if not drain:
        if current is not None:
            return current
        try:
            return source.get(timeout=0.05)
        except queue.Empty:
            return None

    item = current
    if item is None:
        try:
            item = source.get(timeout=0.05)
        except queue.Empty:
            return None
    while True:
        try:
            item = source.get_nowait()
        except queue.Empty:
            break
    return item


def _flush_queue(buffer: queue.Queue) -> None:
    while True:
        try:
            buffer.get_nowait()
        except queue.Empty:
            break


def main() -> int:
    args = _parse_args()

    try:
        left_cam = _select_camera(args, "left")
        right_cam = _select_camera(args, "right")
    except UVCError as exc:
        LOG.error("Unable to open cameras: %s", exc)
        return 1

    LOG.info("Left : %s", describe_device(left_cam.device))
    LOG.info("Right: %s", describe_device(right_cam.device))

    left_queue: queue.Queue = queue.Queue(maxsize=args.queue_size)
    right_queue: queue.Queue = queue.Queue(maxsize=args.queue_size)
    stop_event = threading.Event()
    start_barrier = threading.Barrier(3)
    start_event = threading.Event()

    left_thread = threading.Thread(
        target=frame_producer,
        args=(
            left_cam,
            left_queue,
            stop_event,
            start_barrier,
            start_event,
            args,
            "left",
            args.left_core,
            args.left_start_delay,
        ),
        name="left-producer",
        daemon=True,
    )
    right_thread = threading.Thread(
        target=frame_producer,
        args=(
            right_cam,
            right_queue,
            stop_event,
            start_barrier,
            start_event,
            args,
            "right",
            args.right_core,
            args.right_start_delay,
        ),
        name="right-producer",
        daemon=True,
    )
    left_thread.start()
    right_thread.start()

    start_barrier.wait()
    time.sleep(0.2)
    _flush_queue(left_queue)
    _flush_queue(right_queue)
    start_event.set()

    left_frame: Optional[FramePacket] = None
    right_frame: Optional[FramePacket] = None
    drain_latest = args.pairing_mode == "latest"

    calibration_remaining = args.calibration_pairs
    accumulated_delta = 0.0
    pair_count = 0
    drop_left = 0
    drop_right = 0
    stats_next = args.stats_interval if args.stats_interval else None

    target_delta = args.target_delta
    if args.display and cv2 is None:
        raise RuntimeError("OpenCV is required when --display is specified")
    if args.display:
        cv2.namedWindow("stereo3", cv2.WINDOW_NORMAL)

    try:
        while not stop_event.is_set():
            left_frame = _drain_queue(left_frame, left_queue, drain=drain_latest)
            right_frame = _drain_queue(right_frame, right_queue, drain=drain_latest)

            if left_frame is None or right_frame is None:
                if not left_thread.is_alive() or not right_thread.is_alive():
                    break
                continue

            host_delta = left_frame.host_ts - right_frame.host_ts
            left_pts_sec = _pts_to_seconds(left_frame.pts)
            right_pts_sec = _pts_to_seconds(right_frame.pts)
            pts_delta = None
            if left_pts_sec is not None and right_pts_sec is not None:
                pts_delta = left_pts_sec - right_pts_sec

            if target_delta is None and calibration_remaining > 0:
                accumulated_delta += host_delta
                calibration_remaining -= 1
                if calibration_remaining == 0:
                    target_delta = accumulated_delta / max(args.calibration_pairs, 1)
                    LOG.info("Calibration locked target delta at %.3f ms", target_delta * 1000)

            effective_delta = host_delta - (target_delta or 0.0)
            if abs(effective_delta) > args.max_ts_diff:
                if effective_delta < 0:
                    left_frame = None
                    drop_left += 1
                else:
                    right_frame = None
                    drop_right += 1
                continue

            if args.print_deltas:
                message = f"Δhost={(host_delta)*1000:+.3f} ms"
                if target_delta:
                    message += f" (centered {effective_delta*1000:+.3f} ms)"
                if pts_delta is not None:
                    message += f" ΔPTS={pts_delta*1000:+.3f} ms"
                print(message)

            if args.display:
                try:
                    left_bgr = left_frame.frame.to_bgr()
                    right_bgr = right_frame.frame.to_bgr()
                except RuntimeError as exc:
                    LOG.warning("Failed to convert frame: %s", exc)
                    left_frame = None
                    right_frame = None
                    continue
                stereo = np.hstack((left_bgr, right_bgr))
                cv2.imshow("stereo3", stereo)
                key = cv2.waitKey(1) & 0xFF
                if key in (ord("q"), 27):
                    break

            left_frame = None
            right_frame = None
            pair_count += 1

            if stats_next and pair_count % stats_next == 0:
                LOG.info(
                    "Pairs=%d drops(L=%d R=%d) target=%.3f ms",
                    pair_count,
                    drop_left,
                    drop_right,
                    (target_delta or 0.0) * 1000,
                )
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
