#!/usr/bin/env python3
"""Disable auto exposure and sweep exposure time across the advertised range."""

from __future__ import annotations

import argparse
import logging
from typing import List, Optional

import cv2
import usb.core

from uvc_cli import (
    add_device_arguments,
    add_streaming_arguments,
    apply_device_filters,
    configure_logging,
    ensure_repo_import,
    resolve_device_index,
)

ensure_repo_import()

from libusb_uvc import (  # type: ignore  # pylint: disable=wrong-import-position
    CodecPreference,
    ControlEntry,
    DecoderPreference,
    UVCCamera,
    UVCError,
    describe_device,
)

LOG = logging.getLogger("exposure_sweep")


def find_control(entries: List[ControlEntry], *names: str) -> Optional[ControlEntry]:
    lower_map = {entry.name.lower(): entry for entry in entries}
    for name in names:
        entry = lower_map.get(name.lower())
        if entry:
            return entry
    for entry in entries:
        if "exposure" in entry.name.lower() and "auto" in entry.name.lower():
            return entry
    return None


def build_exposure_sweep(
    ctrl: ControlEntry,
    frames: int,
    *,
    min_exposure_us: int,
    max_exposure_us: int,
    exposure_unit_us: int,
) -> List[int]:
    if ctrl.minimum is None or ctrl.maximum is None:
        raise ValueError("Exposure control does not report min/max")

    if min_exposure_us <= 0 or max_exposure_us <= min_exposure_us:
        raise ValueError("Invalid exposure range")

    step = ctrl.step or 1
    # Convert requested microsecond range to control units.
    start_units = int(round(min_exposure_us / exposure_unit_us))
    end_units = int(round(max_exposure_us / exposure_unit_us))
    requested_start_units = start_units
    requested_end_units = end_units
    if end_units < start_units:
        start_units, end_units = end_units, start_units

    # Clamp to the device's advertised range.
    start_units = max(ctrl.minimum, min(ctrl.maximum, start_units))
    end_units = max(ctrl.minimum, min(ctrl.maximum, end_units))

    # If clamping collapsed the range to a single value, still produce a list.
    if end_units <= start_units:
        return [max(ctrl.minimum, min(ctrl.maximum, start_units))]

    span = end_units - start_units
    steps = max(2, frames)
    values: List[int] = []
    for i in range(steps):
        frac = i / (steps - 1)
        raw = start_units + span * frac
        value = int(round(raw / step)) * step
        value = max(start_units, min(end_units, value))
        values.append(value)

    # enforce endpoints to match requested range
    values[0] = start_units
    values[-1] = end_units

    if start_units != requested_start_units or end_units != requested_end_units:
        effective_min_us = start_units * exposure_unit_us
        effective_max_us = end_units * exposure_unit_us
        LOG.warning(
            "Requested exposure [%d, %d] µs constrained to [%d, %d] µs "
            "by device range (control units [%d, %d])",
            min_exposure_us,
            max_exposure_us,
            effective_min_us,
            effective_max_us,
            start_units,
            end_units,
        )
    return values


def main() -> int:
    parser = argparse.ArgumentParser(description="Sweep Exposure Time, Absolute over multiple frames")
    add_device_arguments(parser, default_index=0)
    add_streaming_arguments(
        parser,
        width_default=1920,
        height_default=1080,
        fps_default=0.0,
        skip_default=10,
        timeout_default=2000,
        codec_choices=[
            CodecPreference.AUTO,
            CodecPreference.YUYV,
            CodecPreference.MJPEG,
            CodecPreference.FRAME_BASED,
            CodecPreference.H264,
            CodecPreference.H265,
        ],
        codec_default=CodecPreference.MJPEG,
        codec_help="Preferred codec (default MJPEG for lower bandwidth)",
        include_decoder=True,
        decoder_choices=[
            DecoderPreference.AUTO,
            DecoderPreference.NONE,
            DecoderPreference.PYAV,
            DecoderPreference.GSTREAMER,
        ],
        decoder_default=DecoderPreference.AUTO,
    )
    parser.add_argument(
        "--frames",
        "--steps",
        dest="frames",
        type=int,
        default=300,
        help="Number of frames / exposure steps for the sweep",
    )
    parser.add_argument(
        "--min-exposure-us",
        type=int,
        default=100,
        help="Minimum exposure time in microseconds for the sweep (default 100us)",
    )
    parser.add_argument(
        "--max-exposure-us",
        type=int,
        default=20000,
        help="Maximum exposure time in microseconds for the sweep (default 20000us == 20ms)",
    )
    parser.add_argument(
        "--min-ms",
        type=float,
        dest="min_ms",
        help="Minimum exposure time in milliseconds (overrides --min-exposure-us)",
    )
    parser.add_argument(
        "--max-ms",
        type=float,
        dest="max_ms",
        help="Maximum exposure time in milliseconds (overrides --max-exposure-us)",
    )
    parser.add_argument(
        "--exposure-unit-us",
        type=int,
        default=100,
        help="Exposure control unit in microseconds (UVC default is 100us per unit)",
    )
    parser.add_argument(
        "--no-display",
        action="store_true",
        help="Disable OpenCV window to minimise timing overhead during analysis",
    )
    parser.add_argument(
        "--log-timing",
        action="store_true",
        help="Log per-frame timing: inter-frame delta and instantaneous FPS",
    )
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    apply_device_filters(args)
    resolve_device_index(args)

    configure_logging(args.log_level, name="exposure_sweep")

    try:
        with UVCCamera.open(
            vid=args.vid,
            pid=args.pid,
            device_index=args.device_index,
            interface=args.interface,
        ) as camera:
            print(f"Using device: {describe_device(camera.device)}")

            try:
                controls = camera.enumerate_controls(refresh=True)
            except RuntimeError as exc:
                print(f"Unable to enumerate controls: {exc}")
                print("Hint: enable auto-detach or detach the kernel driver manually.")
                return 1

            auto_ctrl = find_control(
                controls,
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

            priority_ctrl = find_control(controls, "Exposure Auto Priority")
            if priority_ctrl and priority_ctrl.is_writable():
                try:
                    camera.set_control(priority_ctrl, 0)
                    LOG.info("Disabled exposure auto priority")
                except (UVCError, usb.core.USBError) as exc:
                    LOG.debug("Unable to clear exposure priority: %s", exc)

            exposure_ctrl = find_control(controls, "Exposure Time, Absolute")
            if not exposure_ctrl or not exposure_ctrl.is_writable():
                print("Exposure control not available or not writable on this device.")
                return 1

            min_exposure_us = args.min_exposure_us
            max_exposure_us = args.max_exposure_us
            if args.min_ms is not None:
                min_exposure_us = int(round(args.min_ms * 1000.0))
            if args.max_ms is not None:
                max_exposure_us = int(round(args.max_ms * 1000.0))

            sweep = build_exposure_sweep(
                exposure_ctrl,
                args.frames,
                min_exposure_us=min_exposure_us,
                max_exposure_us=max_exposure_us,
                exposure_unit_us=args.exposure_unit_us,
            )
            LOG.info("Sweeping exposure from %s to %s in %d steps", sweep[0], sweep[-1], len(sweep))
            current_value = sweep[0]
            try:
                camera.set_control(exposure_ctrl, current_value)
            except (UVCError, usb.core.USBError) as exc:
                LOG.error("Unable to set initial exposure value: %s", exc)
                return 1

            font = cv2.FONT_HERSHEY_SIMPLEX
            color = (0, 255, 0)

            frame_rate = args.fps if args.fps > 0 else None
            stream = camera.stream(
                width=args.width,
                height=args.height,
                codec=args.codec,
                decoder=args.decoder,
                frame_rate=frame_rate,
                strict_fps=args.strict_fps,
                skip_initial=max(0, args.skip_frames),
                queue_size=4,
                timeout_ms=max(args.timeout, 1000),
            )

            window = None
            prev_ts: Optional[float] = None
            try:
                if not args.no_display:
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
                        ts = getattr(frame, "timestamp", None)
                        delta_ms: Optional[float] = None
                        fps_inst: Optional[float] = None
                        if isinstance(ts, (int, float)) and prev_ts is not None:
                            delta_s = ts - prev_ts
                            if delta_s > 0:
                                delta_ms = delta_s * 1000.0
                                fps_inst = 1.0 / delta_s
                        if isinstance(ts, (int, float)):
                            prev_ts = ts

                        try:
                            readback = camera.get_control(exposure_ctrl)
                            if isinstance(readback, int):
                                measured_value = readback
                        except (UVCError, usb.core.USBError) as exc:
                            LOG.debug("Exposure readback failed: %s", exc)
                        value = measured_value if measured_value is not None else current_value
                        millis = (
                            (value * args.exposure_unit_us) / 1000.0 if isinstance(value, int) else None
                        )

                        if window:
                            bgr = frame.to_bgr()
                            label = (
                                f"Exposure: {value} ({millis:.2f} ms)"
                                if millis is not None
                                else f"Exposure: {value}"
                            )
                            cv2.putText(bgr, label, (30, 50), font, 1.0, color, 2, cv2.LINE_AA)
                            if delta_ms is not None and fps_inst is not None:
                                timing_label = f"Δt={delta_ms:.3f} ms  FPS={fps_inst:.2f}"
                                cv2.putText(
                                    bgr,
                                    timing_label,
                                    (30, 90),
                                    font,
                                    0.8,
                                    color,
                                    2,
                                    cv2.LINE_AA,
                                )
                            cv2.putText(
                                bgr,
                                f"Frame {idx + 1}/{len(sweep)}",
                                (30, 130),
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
                            if millis is not None and args.log_timing:
                                LOG.info(
                                    "Frame %d/%d exposure=%.2f ms delta=%.3f ms fps=%.2f",
                                    idx + 1,
                                    len(sweep),
                                    millis,
                                    delta_ms if delta_ms is not None else float("nan"),
                                    fps_inst if fps_inst is not None else float("nan"),
                                )

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
