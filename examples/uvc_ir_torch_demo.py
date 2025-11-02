#!/usr/bin/env python3
"""Preview the IR stream and sweep the LED control intensity."""

from __future__ import annotations

import argparse
import logging
import pathlib
import sys
import threading
import time
from typing import List, Optional

import cv2

ROOT = pathlib.Path(__file__).resolve().parents[1]
try:
    from libusb_uvc import CodecPreference, UVCCamera, UVCError, describe_device
except ImportError:  # pragma: no cover - editable install fallback
    sys.path.insert(0, str(ROOT / "src"))
    from libusb_uvc import CodecPreference, UVCCamera, UVCError, describe_device


LOG = logging.getLogger("ir_torch_demo")


def _cycle_led(camera: UVCCamera, control_names: List[str], stop_event: threading.Event) -> None:
    """Continuously sweep the LED control value while the preview runs."""

    if not control_names:
        LOG.info("No LED control exposed on this camera")
        return

    name = control_names[0]
    sequence = [0, 25, 50, 75, 100]
    LOG.info("Cycling LED control '%s' through %s", name, sequence)

    while not stop_event.is_set():
        for percentage in sequence:
            if stop_event.is_set():
                break
            try:
                camera.set_control(name, percentage)
                LOG.debug("Set %s=%s", name, percentage)
            except Exception as exc:  # pragma: no cover - hardware-specific failures
                LOG.warning("Failed to set %s to %s: %s", name, percentage, exc)
                stop_event.set()
                break
            time.sleep(2.0)

    # Restore LED to default when possible.
    try:
        camera.set_control(name, 0)
    except Exception:
        pass


def main() -> int:
    parser = argparse.ArgumentParser(description="View the IR stream while cycling LED intensity")
    parser.add_argument("--vid", type=lambda x: int(x, 0), help="Vendor ID filter")
    parser.add_argument("--pid", type=lambda x: int(x, 0), help="Product ID filter")
    parser.add_argument("--device-index", type=int, default=0, help="Index within detected devices")
    parser.add_argument("--interface", type=int, default=3, help="IR streaming interface number")
    parser.add_argument("--width", type=int, default=400, help="IR frame width")
    parser.add_argument("--height", type=int, default=400, help="IR frame height")
    parser.add_argument("--fps", type=float, default=15.0, help="Target frame rate")
    parser.add_argument("--duration", type=float, default=30.0, help="Preview duration (seconds)")
    parser.add_argument(
        "--codec",
        choices=[CodecPreference.AUTO, CodecPreference.YUYV, CodecPreference.MJPEG],
        default=CodecPreference.YUYV,
        help="Preferred codec for the IR stream",
    )
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
            print(f"Using device: {describe_device(camera.device)} (interface {camera.interface_number})")

            controls = camera.enumerate_controls(refresh=True)
            led_names = [entry.name for entry in controls if entry.name.lower() == "led control"]
            if not led_names:
                LOG.warning("No 'LED Control' found; IR torch may not be adjustable on this device")

            stream = camera.stream(
                width=args.width,
                height=args.height,
                codec=args.codec,
                frame_rate=args.fps if args.fps > 0 else None,
                queue_size=4,
                skip_initial=2,
                timeout_ms=2000,
                duration=args.duration,
            )

            stop_event = threading.Event()
            worker: Optional[threading.Thread] = None
            if led_names:
                worker = threading.Thread(
                    target=_cycle_led, args=(camera, led_names, stop_event), name="ir-led-cycle", daemon=True
                )
                worker.start()

            with stream as frames:
                start = time.time()
                try:
                    cv2.namedWindow("IR Preview", cv2.WINDOW_NORMAL)
                except cv2.error as exc:
                    LOG.error("Failed to create OpenCV window: %s", exc)
                    stop_event.set()
                    if worker:
                        worker.join(timeout=1)
                    return 1

                try:
                    for frame in frames:
                        try:
                            bgr = frame.to_bgr()
                        except RuntimeError as exc:
                            LOG.warning("Frame conversion failed: %s", exc)
                            continue

                        cv2.imshow("IR Preview", bgr)
                        key = cv2.waitKey(1) & 0xFF
                        if key in (ord("q"), 27):
                            break
                        if args.duration and (time.time() - start) >= args.duration:
                            break
                finally:
                    cv2.destroyWindow("IR Preview")
                    stop_event.set()
                    if worker:
                        worker.join(timeout=1)

    except UVCError as exc:
        print(f"Streaming failed: {exc}")
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
