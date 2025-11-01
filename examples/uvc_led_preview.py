#!/usr/bin/env python3
"""Preview a MJPEG stream and switch off the LED after a delay."""

from __future__ import annotations

import argparse
import logging
import pathlib
import sys
import threading
import time
from typing import Optional

import cv2

ROOT = pathlib.Path(__file__).resolve().parents[1]
try:
    from libusb_uvc import CodecPreference, UVCCamera, UVCError, describe_device
except ImportError:  # pragma: no cover
    sys.path.insert(0, str(ROOT / "src"))
    from libusb_uvc import CodecPreference, UVCCamera, UVCError, describe_device

LOG = logging.getLogger("led_preview")


def _disable_led(camera: UVCCamera, control_names: list[str]) -> None:
    for name in control_names:
        try:
            camera.set_control(name, 0)
            LOG.info("LED control '%s' set to 0", name)
            return
        except Exception as exc:  # pragma: no cover
            LOG.debug("Failed to set control %s: %s", name, exc)
    LOG.info("LED control not available or could not be modified")


def main() -> int:
    parser = argparse.ArgumentParser(description="MJPEG preview with automatic LED shutdown")
    parser.add_argument("--vid", type=lambda s: int(s, 0), help="Vendor ID (hex OK)")
    parser.add_argument("--pid", type=lambda s: int(s, 0), help="Product ID (hex OK)")
    parser.add_argument("--device-index", type=int, default=0, help="Index within detected devices")
    parser.add_argument("--interface", type=int, default=1, help="Streaming interface number")
    parser.add_argument("--fps", type=float, default=30.0, help="Target frame rate")
    parser.add_argument("--duration", type=float, default=15.0, help="Preview duration in seconds")
    parser.add_argument("--led-delay", type=float, default=10.0, help="Seconds before LED control is set to 0")
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

            control_names = [
                entry.name
                for entry in camera.enumerate_controls(refresh=True)
                if entry.name.lower() == "led control"
            ]

            stream = camera.stream(
                width=1920,
                height=1080,
                codec=CodecPreference.MJPEG,
                frame_rate=args.fps if args.fps > 0 else None,
                skip_initial=2,
                queue_size=4,
                duration=args.duration,
            )

            led_timer: Optional[threading.Timer] = None
            if control_names and args.led_delay > 0:
                led_timer = threading.Timer(args.led_delay, _disable_led, args=(camera, control_names))
                led_timer.daemon = True
                led_timer.start()
                LOG.info("LED disable scheduled in %.1f seconds", args.led_delay)

            with stream as frames:
                start = time.time()
                window = "LED Preview"
                cv2.namedWindow(window, cv2.WINDOW_NORMAL)
                try:
                    for frame in frames:
                        try:
                            bgr = frame.to_bgr()
                        except RuntimeError as exc:
                            LOG.warning("Failed to decode frame: %s", exc)
                            continue

                        cv2.imshow(window, bgr)
                        key = cv2.waitKey(1) & 0xFF
                        if key in (ord("q"), 27):
                            break
                        if args.duration and (time.time() - start) >= args.duration:
                            break
                finally:
                    cv2.destroyWindow(window)
                    if led_timer is not None:
                        led_timer.cancel()
            return 0
    except UVCError as exc:
        print(f"Streaming failed: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
