#!/usr/bin/env python3
"""Stream MJPEG video and trigger a still image capture on the same device."""

from __future__ import annotations

import argparse
import logging
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
try:
    from libusb_uvc import CodecPreference, UVCCamera, UVCError, describe_device
except ImportError:  # pragma: no cover
    sys.path.insert(0, str(ROOT / "src"))
    from libusb_uvc import CodecPreference, UVCCamera, UVCError, describe_device

from uvc_capture_still import save_payload  # reuse helper


LOG = logging.getLogger("capture_still_live")


def stream_worker(camera: UVCCamera, stop_event: threading.Event, log_interval: float = 5.0) -> None:
    """Background thread that keeps a MJPEG stream running."""

    stream = camera.stream(
        width=1280,
        height=720,
        codec=CodecPreference.MJPEG,
        frame_rate=15,
        queue_size=4,
        skip_initial=2,
        timeout_ms=1000,
    )

    with stream as frames:
        last_log = time.time()
        for frame in frames:
            if stop_event.is_set():
                break
            now = time.time()
            if now - last_log > log_interval:
                LOG.debug(
                    "Streaming keep-alive: frame %sx%s (%d bytes)",
                    frame.frame.width,
                    frame.frame.height,
                    len(frame.payload),
                )
                last_log = now


def main() -> int:
    parser = argparse.ArgumentParser(description="Keep a MJPEG stream alive while capturing a still image")
    parser.add_argument("--vid", type=lambda s: int(s, 0), help="Vendor ID (hex ok)")
    parser.add_argument("--pid", type=lambda s: int(s, 0), help="Product ID (hex ok)")
    parser.add_argument("--device-index", type=int, default=0, help="Index within detected devices")
    parser.add_argument("--interface", type=int, default=1, help="Video streaming interface number")
    parser.add_argument("--width", type=int, help="Still image width (defaults to max supported)")
    parser.add_argument("--height", type=int, help="Still image height")
    parser.add_argument("--timeout", type=int, default=10000, help="Still capture timeout in milliseconds")
    parser.add_argument("--output", type=Path, required=True, help="Destination file (e.g. still.tiff)")
    parser.add_argument("--log-level", default="INFO")
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

            stop_event = threading.Event()
            worker = threading.Thread(target=stream_worker, args=(camera, stop_event), daemon=True)
            worker.start()

            try:
                time.sleep(2.0)

                info = camera.configure_still_image(
                    width=args.width,
                    height=args.height,
                    codec=CodecPreference.MJPEG,
                )
                LOG.info("Still PROBE/COMMIT info: %s", info)
                time.sleep(1.0)

                frame = camera.capture_still_image(timeout_ms=max(1000, args.timeout))
                LOG.info(
                    "Captured still frame %sx%s (%d bytes)",
                    frame.frame.width,
                    frame.frame.height,
                    len(frame.payload),
                )
                save_payload(args.output, frame.payload, frame.format, frame.frame)
            finally:
                stop_event.set()
                worker.join(timeout=2.0)

    except UVCError as exc:
        print(f"Failed to capture still image: {exc}")
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
