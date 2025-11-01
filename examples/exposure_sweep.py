#!/usr/bin/env python3
"""Disable auto exposure and sweep exposure time across the advertised range."""

from __future__ import annotations

import argparse
import logging
import pathlib
import sys
from typing import List, Optional

import cv2
import usb.core

ROOT = pathlib.Path(__file__).resolve().parents[1]
try:
    from libusb_uvc import CodecPreference, ControlEntry, UVCCamera, UVCError, describe_device
except ImportError:  # pragma: no cover
    sys.path.insert(0, str(ROOT / "src"))
    from libusb_uvc import CodecPreference, ControlEntry, UVCCamera, UVCError, describe_device

LOG = logging.getLogger("exposure_sweep")


def find_control(camera: UVCCamera, *names: str) -> Optional[ControlEntry]:
    entries = camera.enumerate_controls(refresh=True)
    lower_map = {entry.name.lower(): entry for entry in entries}
    for name in names:
        entry = lower_map.get(name.lower())
        if entry:
            return entry
    for entry in entries:
        if "exposure" in entry.name.lower() and "auto" in entry.name.lower():
            return entry
    return None


def build_exposure_sweep(ctrl: ControlEntry, frames: int) -> List[int]:
    if ctrl.minimum is None or ctrl.maximum is None:
        raise ValueError("Exposure control does not report min/max")
    step = ctrl.step or 1
    span = ctrl.maximum - ctrl.minimum
    steps = max(2, frames)
    values: List[int] = []
    for i in range(steps):
        frac = i / (steps - 1)
        raw = ctrl.minimum + span * frac
        value = int(round(raw / step)) * step
        value = max(ctrl.minimum, min(ctrl.maximum, value))
        if values and value == values[-1]:
            continue
        values.append(value)
    return values


def main() -> int:
    parser = argparse.ArgumentParser(description="Sweep Exposure Time, Absolute over multiple frames")
    parser.add_argument("--vid", type=lambda s: int(s, 0), help="Vendor ID filter")
    parser.add_argument("--pid", type=lambda s: int(s, 0), help="Product ID filter")
    parser.add_argument("--device-index", type=int, default=0, help="Index of the matching device")
    parser.add_argument("--interface", type=int, default=1, help="Video streaming interface number")
    parser.add_argument("--width", type=int, default=1920, help="Frame width")
    parser.add_argument("--height", type=int, default=1080, help="Frame height")
    parser.add_argument("--frames", type=int, default=300, help="Number of frames for the sweep")
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

            auto_ctrl = find_control(
                camera,
                "Auto Exposure Mode",
                "Exposure Auto",
                "Exposure, Auto",
            )
            if auto_ctrl and auto_ctrl.is_writable():
                try:
                    camera.set_control(auto_ctrl, 1)  # Manual Mode
                    LOG.info("Set auto exposure mode to Manual (%s)", auto_ctrl.name)
                except (UVCError, usb.core.USBError) as exc:
                    LOG.warning("Failed to set auto exposure mode (%s)", exc)

            priority_ctrl = find_control(camera, "Exposure Auto Priority")
            if priority_ctrl and priority_ctrl.is_writable():
                try:
                    camera.set_control(priority_ctrl, 0)
                    LOG.info("Disabled exposure auto priority")
                except (UVCError, usb.core.USBError) as exc:
                    LOG.debug("Unable to clear exposure priority: %s", exc)

            exposure_ctrl = find_control(camera, "Exposure Time, Absolute")
            if not exposure_ctrl or not exposure_ctrl.is_writable():
                print("Exposure control not available or not writable on this device.")
                return 1

            sweep = build_exposure_sweep(exposure_ctrl, args.frames)
            LOG.info("Sweeping exposure from %s to %s in %d steps", sweep[0], sweep[-1], len(sweep))
            current_value = sweep[0]
            try:
                camera.set_control(exposure_ctrl, current_value)
            except (UVCError, usb.core.USBError) as exc:
                LOG.error("Unable to set initial exposure value: %s", exc)
                return 1

            font = cv2.FONT_HERSHEY_SIMPLEX
            color = (0, 255, 0)

            stream = camera.stream(
                width=args.width,
                height=args.height,
                codec=CodecPreference.MJPEG,
                frame_rate=None,
                strict_fps=False,
                skip_initial=10,
                queue_size=4,
                timeout_ms=2000,
            )

            window = None
            try:
                try:
                    window = "Exposure Sweep"
                    cv2.namedWindow(window, cv2.WINDOW_NORMAL)
                except cv2.error as exc:
                    LOG.warning("OpenCV window creation failed (%s); running headless", exc)
                    window = None

                with stream as frames:
                    next_index = 0
                    measured_value: Optional[int] = current_value
                    for idx, frame in enumerate(frames):
                        try:
                            readback = camera.get_control(exposure_ctrl)
                            if isinstance(readback, int):
                                measured_value = readback
                        except (UVCError, usb.core.USBError) as exc:
                            LOG.debug("Exposure readback failed: %s", exc)
                        value = measured_value if measured_value is not None else current_value
                        millis = value / 10000.0 if isinstance(value, int) else None

                        if window:
                            bgr = frame.to_bgr()
                            label = (
                                f"Exposure: {value} ({millis:.2f} ms)"
                                if millis is not None
                                else f"Exposure: {value}"
                            )
                            cv2.putText(bgr, label, (30, 50), font, 1.0, color, 2, cv2.LINE_AA)
                            cv2.putText(
                                bgr,
                                f"Frame {idx + 1}/{len(sweep)}",
                                (30, 100),
                                font,
                                0.9,
                                color,
                                2,
                                cv2.LINE_AA,
                            )
                            cv2.imshow(window, bgr)
                            key = cv2.waitKey(1) & 0xFF
                            if key in (ord("q"), 27):
                                break
                        else:
                            if millis is not None:
                                LOG.info("Frame %d/%d exposure %.2f ms", idx + 1, len(sweep), millis)

                        if next_index < len(sweep) - 1:
                            next_index += 1
                            current_value = sweep[next_index]
                            try:
                                camera.set_control(exposure_ctrl, current_value)
                            except (UVCError, usb.core.USBError) as exc:
                                LOG.warning("Failed to set exposure step %d: %s", next_index, exc)
                                break
                        else:
                            break
            finally:
                if window:
                    cv2.destroyWindow(window)
                if auto_ctrl and auto_ctrl.is_writable() and auto_ctrl.default is not None:
                    try:
                        camera.set_control(auto_ctrl, auto_ctrl.default)
                    except Exception:
                        pass
                if exposure_ctrl.default is not None:
                    try:
                        camera.set_control(exposure_ctrl, exposure_ctrl.default)
                    except Exception:
                        pass
            return 0
    except UVCError as exc:
        print(f"Failed to initialize or stream from camera: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
