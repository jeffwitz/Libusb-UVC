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

from uvc_cli import ensure_repo_import

ensure_repo_import()

from libusb_uvc import CodecPreference, UVCCamera, UVCError, describe_device  # type: ignore  # pylint: disable=wrong-import-position

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
    parser.add_argument("--width", type=int, default=640, help="Frame width")
    parser.add_argument("--height", type=int, default=480, help="Frame height")
    parser.add_argument("--fps", type=float, default=15.0, help="Expected frame rate")
    codec_choices = [c.name.lower() for c in CodecPreference]
    parser.add_argument(
        "--codec",
        default="mjpeg",
        choices=codec_choices,
        help="Codec to request on both cameras",
    )
    parser.add_argument("--max-ts-diff", type=float, default=0.033, help="Max timestamp delta (s) for pairing")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    logging.basicConfig(level=args.log_level.upper())
    args.codec = CodecPreference[args.codec.upper()]

    try:
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

    cv2.namedWindow("stereo", cv2.WINDOW_NORMAL)

    left_frame: Optional[object] = None
    right_frame: Optional[object] = None

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
            else:
                right_frame = None

            key = cv2.waitKey(1) & 0xFF
            if key in (ord("q"), 27):
                break
    except KeyboardInterrupt:
        LOG.info("Interrupted by user")
    finally:
        stop_event.set()
        left_thread.join(timeout=1)
        right_thread.join(timeout=1)
        left_cam.close()
        right_cam.close()
        cv2.destroyAllWindows()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
