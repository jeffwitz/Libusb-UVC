#!/usr/bin/env python3
"""Stereo phase synchronisation via firmware nudges.

This helper opens two identical UVC cameras, forces manual exposure on both
and pairs frames using timestamp-aware queues. Whenever the timestamp delta
exceeds a tolerance window, the leading camera is "nudged" by rapidly sending
redundant SET_CUR requests for the Exposure control (toggle nominal and
nominal+Δ). The firmware must service these I2C transactions synchronously,
which creates a small delay that helps bring both streams back into phase.
"""

from __future__ import annotations

import argparse
import contextlib
import logging
import math
import queue
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Optional, Tuple, List, Deque

import usb.core

from uvc_cli import ensure_repo_import, parse_device_id

ensure_repo_import()

from libusb_uvc import (  # type: ignore  # pylint: disable=wrong-import-position
    CodecPreference,
    ControlEntry,
    DecoderPreference,
    UVCCamera,
    UVCError,
    describe_device,
    find_uvc_devices,
    list_streaming_interfaces,
)

LOG = logging.getLogger("stereo_phase_sync")


@dataclass
class FramePacket:
    """Lightweight container for frames in the synchronisation loop."""

    frame: object
    timestamp: float


@dataclass
class ExposurePhaseConfig:
    """Exposure-related knobs for one camera."""

    auto_ctrl: Optional[ControlEntry]
    priority_ctrl: Optional[ControlEntry]
    exposure_ctrl: ControlEntry
    original_exposure: Optional[int]
    nominal_exposure: int


def _normalise_codec(value: str) -> CodecPreference:
    token = value.strip().replace("-", "_").upper()
    return getattr(CodecPreference, token)


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


def _find_control(entries: List[ControlEntry], *names: str) -> Optional[ControlEntry]:
    """Locate a control entry by exact or fuzzy name."""

    lower_map = {entry.name.lower(): entry for entry in entries}
    for name in names:
        entry = lower_map.get(name.lower())
        if entry:
            return entry
    for entry in entries:
        if "exposure" in entry.name.lower() and "auto" in entry.name.lower():
            return entry
    return None


def _estimate_advertised_fps(camera: UVCCamera, args: argparse.Namespace) -> Optional[float]:
    """Best-effort FPS estimate from the camera's descriptors.

    This does not force a particular frame rate; it inspects the advertised
    intervals for the requested resolution on the selected interface and
    returns the highest FPS available for that mode.
    """

    try:
        interfaces = list_streaming_interfaces(camera.device)
        interface = interfaces.get(args.interface)
        if interface is None:
            return None
        match = interface.find_frame(args.width, args.height)
        if match is None:
            return None
        _, frame = match
        fps_values = [fps for fps in frame.intervals_hz() if fps and fps > 0]
        if not fps_values:
            return None
        return max(fps_values)
    except Exception as exc:  # pragma: no cover - defensive logging
        LOG.debug("Failed to estimate advertised FPS: %s", exc)
        return None


def _prepare_exposure_config(
    camera: UVCCamera,
    *,
    nominal_ms: float,
    exposure_unit_us: int,
) -> ExposurePhaseConfig:
    """Disable auto-exposure and configure the nominal exposure value."""

    controls = camera.enumerate_controls(refresh=True)

    auto_ctrl = _find_control(
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
            LOG.warning("Failed to set auto exposure mode on %s: %s", auto_ctrl.name, exc)
    else:
        LOG.warning("No writable auto-exposure control found; exposure timing may not be stable")

    priority_ctrl = _find_control(controls, "Exposure Auto Priority")
    if priority_ctrl and priority_ctrl.is_writable():
        try:
            camera.set_control(priority_ctrl, 0)
            LOG.info("Disabled exposure auto priority")
        except (UVCError, usb.core.USBError) as exc:
            LOG.debug("Unable to clear exposure priority: %s", exc)

    # Some vendors expose an additional AE/AG toggle (often labelled "AEAG").
    # Try to disable it so that manual exposure takes full effect.
    aeag_ctrl: Optional[ControlEntry] = None
    for entry in controls:
        if "aeag" in entry.name.lower():
            aeag_ctrl = entry
            break
    if aeag_ctrl and aeag_ctrl.is_writable():
        try:
            camera.set_control(aeag_ctrl, 0)
            LOG.info("Disabled AE/AG control (%s)", aeag_ctrl.name)
        except (UVCError, usb.core.USBError) as exc:
            LOG.debug("Unable to disable AE/AG control %s: %s", aeag_ctrl.name, exc)

    exposure_ctrl = _find_control(controls, "Exposure Time, Absolute")
    if not exposure_ctrl or not exposure_ctrl.is_writable():
        raise UVCError("Exposure control not available or not writable on this device")

    try:
        original_exposure = camera.get_control(exposure_ctrl)
    except (UVCError, usb.core.USBError):
        original_exposure = None

    def ms_to_units(ms: float) -> int:
        """Convert milliseconds to control units, mirroring exposure_sweep logic."""

        us = ms * 1000.0
        # Convert to exposure control units (default 100us per unit).
        raw_units = us / float(exposure_unit_us)

        # Quantise to the device's advertised step in units, if present.
        step_units = exposure_ctrl.step or 1
        value = int(round(raw_units / step_units)) * step_units

        # Clamp to the control's advertised range.
        if exposure_ctrl.minimum is not None:
            value = max(exposure_ctrl.minimum, value)
        if exposure_ctrl.maximum is not None:
            value = min(exposure_ctrl.maximum, value)
        return value

    nominal_val = ms_to_units(nominal_ms)

    # For logging: derive effective exposure times from the quantised units.
    nominal_ms_effective = (nominal_val * exposure_unit_us) / 1000.0

    try:
        camera.set_control(exposure_ctrl, nominal_val)
        LOG.info(
            "Set nominal exposure to %d (requested=%.2f ms, effective=%.2f ms, unit=%dus)",
            nominal_val,
            nominal_ms,
            nominal_ms_effective,
            exposure_unit_us,
        )
    except (UVCError, usb.core.USBError) as exc:
        LOG.warning("Failed to set nominal exposure: %s", exc)

    return ExposurePhaseConfig(
        auto_ctrl=auto_ctrl,
        priority_ctrl=priority_ctrl,
        exposure_ctrl=exposure_ctrl,
        original_exposure=original_exposure if isinstance(original_exposure, int) else None,
        nominal_exposure=nominal_val,
    )


def heavy_nudge(
    camera: UVCCamera,
    config: ExposurePhaseConfig,
    *,
    iterations: int,
    step_units: int,
    label: str,
) -> bool:
    """Spam SET_CUR Exposure toggles to stall the firmware for a few milliseconds."""

    ctrl = config.exposure_ctrl
    base = config.nominal_exposure
    ctrl_step = ctrl.step or 1
    increment = max(step_units, ctrl_step, 1)
    bump = base + increment
    if ctrl.maximum is not None:
        bump = min(ctrl.maximum, bump)

    if bump == base:
        LOG.debug(
            "Skipping heavy nudge on %s: exposure control has no headroom (base=%d increment=%d)",
            label,
            base,
            increment,
        )
        return False

    try:
        for _ in range(iterations):
            camera.set_control(ctrl, bump)
            camera.set_control(ctrl, base)
    except (UVCError, usb.core.USBError) as exc:
        LOG.warning("Heavy nudge on %s failed: %s", label, exc)
        return False

    LOG.debug(
        "Heavy nudge applied on %s (base=%d bump=%d iterations=%d)",
        label,
        base,
        bump,
        iterations,
    )
    return True


def frame_producer(
    camera: UVCCamera,
    frame_queue: queue.Queue,
    stop_event: threading.Event,
    args: argparse.Namespace,
    label: str,
) -> None:
    """Continuously capture frames and push them into a queue."""

    try:
        stream = camera.stream(
            width=args.width,
            height=args.height,
            codec=args.codec,
            decoder=DecoderPreference.NONE,
            frame_rate=args.fps if args.fps > 0 else None,
            strict_fps=False,
            queue_size=args.stream_queue,
            timeout_ms=args.timeout_ms,
        )
        with stream as frames:
            for frame in frames:
                if stop_event.is_set():
                    break
                packet = FramePacket(frame=frame, timestamp=frame.timestamp)
                try:
                    frame_queue.put_nowait(packet)
                except queue.Full:
                    # Drop the oldest and insert the most recent.
                    with contextlib.suppress(queue.Empty):
                        frame_queue.get_nowait()
                    frame_queue.put_nowait(packet)
    except UVCError as exc:
        LOG.error("Producer %s error: %s", label, exc)
    finally:
        stop_event.set()
        LOG.info("Producer %s stopped", label)


def _match_pair(
    left_buffer: Deque[FramePacket],
    right_buffer: Deque[FramePacket],
    sync_window_s: float,
) -> Tuple[Optional[Tuple[FramePacket, FramePacket, float]], int, int]:
    """Match the closest pair of frames within the synchronisation window.

    Returns (pair, dropped_left, dropped_right) where *pair* is either
    (left_frame, right_frame, delta_seconds) or None if no synchronisable pair
    could be formed from the current buffers.
    """

    dropped_left = 0
    dropped_right = 0

    while left_buffer and right_buffer:
        left = left_buffer[0]
        right = right_buffer[0]
        delta = left.timestamp - right.timestamp
        if abs(delta) <= sync_window_s:
            left_buffer.popleft()
            right_buffer.popleft()
            return (left, right, delta), dropped_left, dropped_right
        if delta > 0:
            # Right frame is earlier; drop it so left can catch up.
            right_buffer.popleft()
            dropped_right += 1
        else:
            left_buffer.popleft()
            dropped_left += 1

    return None, dropped_left, dropped_right


def _parse_args() -> argparse.Namespace:
    codec_choices = ["auto", "yuyv", "mjpeg", "frame_based", "h264", "h265"]
    parser = argparse.ArgumentParser(description="Stereo phase synchronisation via firmware nudging")
    parser.add_argument("--left-index", type=int, default=0, help="Device index of the left camera")
    parser.add_argument("--right-index", type=int, default=1, help="Device index of the right camera")
    parser.add_argument("--device-id", help="VID:PID shared by both cameras (hex or decimal)")
    parser.add_argument("--left-device-sn", help="Serial number of the left camera")
    parser.add_argument("--right-device-sn", help="Serial number of the right camera")
    parser.add_argument("--interface", type=int, default=1, help="UVC interface number to claim")
    parser.add_argument("--width", type=int, default=640, help="Frame width")
    parser.add_argument("--height", type=int, default=480, help="Frame height")
    parser.add_argument(
        "--fps",
        type=float,
        default=0.0,
        help=(
            "Optional frame rate hint for negotiation (Hz). When set to 0, the "
            "camera's advertised default is used and the helper estimates the "
            "effective FPS from the descriptors."
        ),
    )
    parser.add_argument(
        "--codec",
        default="mjpeg",
        choices=codec_choices,
        help="Codec to request on both cameras",
    )
    parser.add_argument("--stream-queue", type=int, default=4, help="Internal queue size inside UVCCamera.stream")
    parser.add_argument("--queue-size", type=int, default=3, help="Buffered frames per camera in the consumer")
    parser.add_argument("--timeout-ms", type=int, default=2000, help="Read timeout per USB transfer")

    # Phase-sliding parameters
    parser.add_argument(
        "--tolerance-ms",
        type=float,
        default=3.0,
        help="Allowed timestamp delta before triggering a heavy nudge (ms)",
    )
    parser.add_argument(
        "--nominal-exposure-ms",
        type=float,
        default=10.0,
        help="Nominal exposure time to apply to both cameras (ms)",
    )
    parser.add_argument(
        "--exposure-unit-us",
        type=int,
        default=100,
        help="Exposure control unit in microseconds (UVC default is 100us per unit)",
    )
    parser.add_argument(
        "--nudge-iterations",
        type=int,
        default=15,
        help="Number of exposure toggle pairs per heavy nudge (higher = stronger delay)",
    )
    parser.add_argument(
        "--nudge-step-units",
        type=int,
        default=2,
        help="Minimum exposure control units to add during a nudge (clamped to device step size)",
    )
    parser.add_argument(
        "--nudge-gain",
        type=float,
        default=0.2,
        help="Heavy nudges issued per millisecond of phase error beyond tolerance (0 disables the closed-loop accumulator)",
    )
    parser.add_argument(
        "--nudge-max-per-cycle",
        type=int,
        default=2,
        help="Hard limit on heavy nudges issued during a single control cycle",
    )
    parser.add_argument(
        "--nudge-ramp-max",
        type=float,
        default=0.1,
        help="Maximum ramp scaling factor allowed during calibration",
    )
    parser.add_argument(
        "--nudge-ramp-start",
        type=float,
        default=0.02,
        help="Initial scaling factor for nudge gain/iterations during calibration (0-1)",
    )
    parser.add_argument(
        "--nudge-ramp-step",
        type=float,
        default=0.01,
        help="Increment applied to the ramp scaling factor up to --nudge-ramp-max",
    )
    parser.add_argument(
        "--nudge-ramp-lock",
        type=float,
        default=2.0,
        help="|Δ| below this threshold (ms) stops further ramping on the current leader",
    )
    parser.add_argument(
        "--nudge-ramp-decay",
        type=float,
        default=0.5,
        help="Multiplier applied to the previous leader's ramp after a sign change (0-1)",
    )
    parser.add_argument(
        "--calibration-pairs",
        type=int,
        default=50,
        help="Number of initial frame pairs used to estimate startup offset (buffer alignment happens at most once in this window)",
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=0.0,
        help="Total run time in seconds (0 for infinite)",
    )
    parser.add_argument(
        "--sync-window-ms",
        type=float,
        default=5.0,
        help="Maximum host timestamp delta accepted when pairing frames (ms)",
    )
    parser.add_argument(
        "--monitor-window-ms",
        type=float,
        default=50.0,
        help=(
            "Maximum host timestamp delta allowed between buffers in 'monitor' mode before "
            "dropping the leading frame (ms)"
        ),
    )
    parser.add_argument(
        "--pairing-mode",
        choices=["sync", "soft-sync", "fifo", "monitor"],
        default="sync",
        help=(
            "Frame pairing strategy: 'sync' drops frames to keep |Δ| within "
            "sync-window-ms (lower jitter, lower effective FPS); "
            "'soft-sync' uses timestamp-aware pairing during calibration but "
            "preserves frames afterwards (records the physical offset); "
            "'fifo' uses pure FIFO pairing without additional drops; "
            "'monitor' pairs every frame FIFO-style but retains the buffer-alignment/calibration pipeline so nudges run on all frames without timestamp gating."
        ),
    )
    parser.add_argument("--log-level", default="INFO")
    parser.add_argument("--print-deltas", action="store_true", help="Print timestamp deltas for each pair")
    args = parser.parse_args()

    if args.nudge_iterations <= 0:
        parser.error("--nudge-iterations must be positive")
    if args.nudge_step_units <= 0:
        parser.error("--nudge-step-units must be positive")
    if args.nudge_gain < 0.0:
        parser.error("--nudge-gain must be non-negative")
    if args.nudge_max_per_cycle <= 0:
        parser.error("--nudge-max-per-cycle must be positive")
    if args.pairing_mode == "monitor" and args.monitor_window_ms <= 0.0:
        parser.error("--monitor-window-ms must be positive in monitor mode")
    if not 0.0 <= args.nudge_ramp_max <= 1.0:
        parser.error("--nudge-ramp-max must be between 0 and 1")
    if args.nudge_ramp_start < 0.0 or args.nudge_ramp_start > 1.0:
        parser.error("--nudge-ramp-start must be between 0 and 1")
    if args.nudge_ramp_step < 0.0:
        parser.error("--nudge-ramp-step must be non-negative")
    if not 0.0 <= args.nudge_ramp_decay <= 1.0:
        parser.error("--nudge-ramp-decay must be between 0 and 1")
    if args.nudge_ramp_lock < 0.0:
        parser.error("--nudge-ramp-lock must be non-negative")

    logging.basicConfig(level=args.log_level.upper())

    if args.device_id:
        args.device_id = parse_device_id(args.device_id)

    args.codec = _normalise_codec(args.codec)
    return args


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

    # Derive an effective FPS for sliding heuristics. We prefer an explicit
    # hint from --fps when provided, otherwise we inspect the descriptors for
    # the requested resolution.
    effective_fps: Optional[float] = args.fps if args.fps and args.fps > 0.0 else None
    if effective_fps is None:
        effective_fps = _estimate_advertised_fps(left_cam, args)
    if effective_fps:
        LOG.info("Using %.3f fps for pairing heuristics", effective_fps)
        frame_period_ms: Optional[float] = 1000.0 / effective_fps
    else:
        LOG.info("Effective FPS could not be determined; using conservative sync defaults")
        frame_period_ms = None

    # Stash the effective FPS for later use in calibration/soft-sync.
    setattr(args, "effective_fps", effective_fps)

    # Configure exposure controls on both cameras.
    try:
        left_exposure = _prepare_exposure_config(
            left_cam,
            nominal_ms=args.nominal_exposure_ms,
            exposure_unit_us=args.exposure_unit_us,
        )
        right_exposure = _prepare_exposure_config(
            right_cam,
            nominal_ms=args.nominal_exposure_ms,
            exposure_unit_us=args.exposure_unit_us,
        )
    except UVCError as exc:
        LOG.error("Failed to configure exposure controls: %s", exc)
        left_cam.close()
        right_cam.close()
        return 1

    # Queues and threads
    left_queue: queue.Queue = queue.Queue(maxsize=args.queue_size)
    right_queue: queue.Queue = queue.Queue(maxsize=args.queue_size)
    stop_event = threading.Event()

    left_thread = threading.Thread(
        target=frame_producer,
        args=(left_cam, left_queue, stop_event, args, "left"),
        name="left-producer",
        daemon=True,
    )
    right_thread = threading.Thread(
        target=frame_producer,
        args=(right_cam, right_queue, stop_event, args, "right"),
        name="right-producer",
        daemon=True,
    )
    left_thread.start()
    right_thread.start()

    left_buffer: Deque[FramePacket] = deque()
    right_buffer: Deque[FramePacket] = deque()
    pairing_mode = args.pairing_mode
    pairing_sync = pairing_mode == "sync"
    pairing_fifo = pairing_mode == "fifo"
    pairing_soft = pairing_mode == "soft-sync"
    pairing_monitor = pairing_mode == "monitor"

    adjustments = 0
    calibration_pairs = max(0, args.calibration_pairs)
    calibration_deltas: list[float] = []
    calibration_done = calibration_pairs == 0
    post_stats_enabled = False
    post_pairs = 0
    post_sum_delta_ms = 0.0
    post_sum_sq_delta_ms = 0.0
    # Global stats across the whole run (including calibration).
    global_pairs = 0
    global_sum_delta_ms = 0.0
    global_sum_sq_delta_ms = 0.0
    # Per-side frame accounting for sync-level drops (global).
    total_left_frames = 0
    total_right_frames = 0
    drops_left = 0
    drops_right = 0
    # Post-calibration sync-level drop accounting (for reporting).
    sync_total_left_frames = 0
    sync_total_right_frames = 0
    sync_drops_left = 0
    sync_drops_right = 0
    nudges_applied = 0
    nudge_events = 0
    nudge_integral_left = 0.0
    nudge_integral_right = 0.0
    monitor_alignment_done = not pairing_monitor
    ramp_max = min(1.0, max(0.0, args.nudge_ramp_max))
    ramp_start = min(ramp_max, max(0.0, args.nudge_ramp_start))
    scale_map = {
        "left": ramp_start,
        "right": ramp_start,
    }
    leader_locked = {
        "left": False,
        "right": False,
    }
    prev_leader_label: Optional[str] = None
    prev_delta_sign: Optional[int] = None
    nudging_active = not calibration_done

    # Buffer-level phase correction: after calibration, when an integer frame
    # offset is detected, we drop a small number of frames from the leading
    # side to align the pair indices before steady-state pairing.
    buffer_shift_leader: Optional[str] = None
    buffer_shift_remaining = 0
    buffer_shift_total = 0

    # Baseline for human-readable timestamps in --print-deltas output.
    # We subtract the first observed host timestamp so that printed times
    # start near 0 instead of using large epoch-based values.
    print_t0_host: Optional[float] = None

    # Separate timing for the main loop and the post-calibration acquisition
    # window. The user-visible --duration applies to the post-calibration
    # phase only: calibration will run until it completes, regardless of this
    # timer.
    loop_start = time.perf_counter()
    run_start: Optional[float] = None

    try:
        while not stop_event.is_set():
            now = time.perf_counter()
            # Duration guard applies only to the post-calibration acquisition
            # window so that the requested run time reflects the steady-state
            # behaviour after calibration has finished.
            if args.duration > 0.0 and post_stats_enabled:
                if run_start is None:
                    run_start = now
                if now - run_start >= args.duration:
                    LOG.info(
                        "Duration %.1fs reached in post-calibration phase; stopping main loop",
                        args.duration,
                    )
                    break

            # Drain any newly arrived frames into the matching buffers.
            while True:
                try:
                    pkt = left_queue.get_nowait()
                except queue.Empty:
                    break
                left_buffer.append(pkt)
                total_left_frames += 1
                if post_stats_enabled:
                    sync_total_left_frames += 1
            while True:
                try:
                    pkt = right_queue.get_nowait()
                except queue.Empty:
                    break
                right_buffer.append(pkt)
                total_right_frames += 1
                if post_stats_enabled:
                    sync_total_right_frames += 1

            # If a calibration buffer shift is pending, drop frames from the
            # leading side until the requested offset is consumed. This aligns
            # the two buffers by an integer number of frames before any
            # steady-state pairing logic runs.
            if buffer_shift_remaining > 0 and buffer_shift_leader is not None:
                leader_buffer = left_buffer if buffer_shift_leader == "left" else right_buffer
                dropped_now = 0
                while buffer_shift_remaining > 0 and leader_buffer:
                    leader_buffer.popleft()
                    buffer_shift_remaining -= 1
                    dropped_now += 1
                if dropped_now > 0:
                    LOG.debug(
                        "Calibration buffer shift: dropped %d frame(s) from %s (remaining=%d of %d)",
                        dropped_now,
                        buffer_shift_leader,
                        buffer_shift_remaining,
                        buffer_shift_total,
                    )
                if buffer_shift_remaining > 0:
                    # Need more frames to complete the shift; keep filling
                    # buffers without forming pairs yet.
                    if not left_thread.is_alive() or not right_thread.is_alive():
                        break
                    time.sleep(0.001)
                    continue
                LOG.info(
                    "Calibration buffer shift completed: dropped %d frame(s) from %s to align buffers",
                    buffer_shift_total,
                    buffer_shift_leader,
                )
                # After buffer shift, start counting post-calibration stats.
                post_stats_enabled = True
                nudging_active = False

            # Pairing strategy:
            #
            # * 'sync'      – timestamp-aware pairing throughout the run using
            #                 _match_pair and dropping frames to keep |Δ| within
            #                 sync-window-ms.
            # * 'soft-sync' – behave like 'sync' during calibration, then use a
            #                 gentler timestamp-aware strategy based on the frame
            #                 period: FIFO pairing with an occasional drop on the
            #                 leading side when |Δ| exceeds roughly half a frame
            #                 period.
            # * 'fifo'      – pure FIFO pairing at all times; pairing ignores
            #                 timestamps beyond the global ordering.
            if not calibration_done:
                if pairing_sync or pairing_soft:
                    pair, dropped_l, dropped_r = _match_pair(
                        left_buffer,
                        right_buffer,
                        args.sync_window_ms / 1000.0,
                    )
                    drops_left += dropped_l
                    drops_right += dropped_r
                    if post_stats_enabled:
                        sync_drops_left += dropped_l
                        sync_drops_right += dropped_r
                    if pair is None:
                        if not left_thread.is_alive() or not right_thread.is_alive():
                            break
                        # No synchronisable pair yet; wait for more frames.
                        time.sleep(0.001)
                        continue
                    left_frame, right_frame, delta = pair
                elif pairing_monitor:
                    if not monitor_alignment_done:
                        pair, dropped_l, dropped_r = _match_pair(
                            left_buffer,
                            right_buffer,
                            args.monitor_window_ms / 1000.0,
                        )
                        drops_left += dropped_l
                        drops_right += dropped_r
                        if (dropped_l or dropped_r) and LOG.isEnabledFor(logging.INFO):
                            LOG.info(
                                "Monitor drop during initial alignment: left=%d right=%d (window=%.1f ms)",
                                dropped_l,
                                dropped_r,
                                args.monitor_window_ms,
                            )
                        if pair is None:
                            if not left_thread.is_alive() or not right_thread.is_alive():
                                break
                            time.sleep(0.001)
                            continue
                        left_frame, right_frame, delta = pair
                        monitor_alignment_done = True
                        left_buffer.clear()
                        right_buffer.clear()
                        LOG.info(
                            "Monitor initial alignment complete; switched to FIFO pairing and cleared buffered backlog"
                        )
                    else:
                        if not left_buffer or not right_buffer:
                            if not left_thread.is_alive() or not right_thread.is_alive():
                                break
                            time.sleep(0.001)
                            continue
                        left_frame = left_buffer.popleft()
                        right_frame = right_buffer.popleft()
                        delta = left_frame.timestamp - right_frame.timestamp
                else:
                    # 'fifo' calibration: pair FIFO without extra drops.
                    if not left_buffer or not right_buffer:
                        if not left_thread.is_alive() or not right_thread.is_alive():
                            break
                        time.sleep(0.001)
                        continue
                    left_frame = left_buffer.popleft()
                    right_frame = right_buffer.popleft()
                    delta = left_frame.timestamp - right_frame.timestamp
            else:
                if pairing_sync:
                    pair, dropped_l, dropped_r = _match_pair(
                        left_buffer,
                        right_buffer,
                        args.sync_window_ms / 1000.0,
                    )
                    drops_left += dropped_l
                    drops_right += dropped_r
                    if post_stats_enabled:
                        sync_drops_left += dropped_l
                        sync_drops_right += dropped_r
                    if pair is None:
                        if not left_thread.is_alive() or not right_thread.is_alive():
                            break
                        time.sleep(0.001)
                        continue
                    left_frame, right_frame, delta = pair
                elif pairing_soft:
                    # Post-calibration soft-sync: use FIFO pairing, but keep a small
                    # buffer and occasionally drop the leading side when the phase
                    # error exceeds roughly half a frame period. This keeps the
                    # streams close in phase without the aggressive dropping of
                    # full 'sync' mode.
                    if not left_buffer or not right_buffer:
                        if not left_thread.is_alive() or not right_thread.is_alive():
                            break
                        time.sleep(0.001)
                        continue

                    effective_fps = getattr(args, "effective_fps", None)
                    if not effective_fps or effective_fps <= 0.0:
                        # Without an effective frame rate we cannot derive a frame
                        # period; fall back to simple FIFO pairing.
                        left_frame = left_buffer.popleft()
                        right_frame = right_buffer.popleft()
                        delta = left_frame.timestamp - right_frame.timestamp
                    else:
                        frame_period_ms = 1000.0 / effective_fps
                        max_phase_error_ms = frame_period_ms / 2.0

                        head_left = left_buffer[0]
                        head_right = right_buffer[0]
                        current_delta = head_left.timestamp - head_right.timestamp
                        current_delta_ms = current_delta * 1000.0

                        if abs(current_delta_ms) > max_phase_error_ms:
                            # One side is ahead by more than ~half a frame; drop a
                            # single frame from the leading side and try again on
                            # the next loop iteration.
                            if current_delta_ms > 0.0:
                                # Right frame is earlier; drop it so left can catch up.
                                right_buffer.popleft()
                                drops_right += 1
                                if post_stats_enabled:
                                    sync_drops_right += 1
                            else:
                                left_buffer.popleft()
                                drops_left += 1
                                if post_stats_enabled:
                                    sync_drops_left += 1

                            if not left_thread.is_alive() or not right_thread.is_alive():
                                break
                            time.sleep(0.001)
                            continue

                        # Within the phase window: pair FIFO.
                        left_frame = left_buffer.popleft()
                        right_frame = right_buffer.popleft()
                        delta = left_frame.timestamp - right_frame.timestamp
                elif pairing_monitor:
                    if not left_buffer or not right_buffer:
                        if not left_thread.is_alive() or not right_thread.is_alive():
                            break
                        time.sleep(0.001)
                        continue
                    left_frame = left_buffer.popleft()
                    right_frame = right_buffer.popleft()
                    delta = left_frame.timestamp - right_frame.timestamp
                else:
                    # Pure FIFO after calibration.
                    if not left_buffer or not right_buffer:
                        if not left_thread.is_alive() or not right_thread.is_alive():
                            break
                        time.sleep(0.001)
                        continue
                    left_frame = left_buffer.popleft()
                    right_frame = right_buffer.popleft()
                    delta = left_frame.timestamp - right_frame.timestamp
            delta_ms = delta * 1000.0

            if (
                not calibration_done
                and pairing_monitor
                and frame_period_ms is not None
                and abs(delta_ms) > frame_period_ms
            ):
                leader_label = "left" if delta_ms < 0 else "right"
                leader_buffer = left_buffer if leader_label == "left" else right_buffer
                if leader_buffer:
                    leader_buffer.popleft()
                    LOG.info(
                        "Modulo drop: %s ahead by %.3f ms (frame period %.3f ms); discarding extra frame",
                        leader_label,
                        abs(delta_ms),
                        frame_period_ms,
                    )
                continue

            if args.print_deltas:
                if print_t0_host is None:
                    print_t0_host = min(left_frame.timestamp, right_frame.timestamp)
                left_rel = left_frame.timestamp - print_t0_host
                right_rel = right_frame.timestamp - print_t0_host
                print(
                    f"Δ={delta_ms:+.3f} ms "
                    f"(left_ts={left_rel:.6f}s right_ts={right_rel:.6f}s)"
                )

            # Global statistics, independent of calibration state.
            global_pairs += 1
            global_sum_delta_ms += delta_ms
            global_sum_sq_delta_ms += delta_ms * delta_ms

            if not calibration_done:
                current_leader_label: Optional[str] = None
                if delta_ms < 0.0:
                    current_leader_label = "left"
                elif delta_ms > 0.0:
                    current_leader_label = "right"

                sign = 0
                if delta_ms > 0.0:
                    sign = 1
                elif delta_ms < 0.0:
                    sign = -1
                if sign != 0 and prev_delta_sign is not None and sign != prev_delta_sign:
                    LOG.info("Calibration sign change detected; locking ramp direction")
                    if prev_leader_label and prev_leader_label in scale_map:
                        leader_locked[prev_leader_label] = True
                        scale_map[prev_leader_label] = max(
                            ramp_start,
                            scale_map[prev_leader_label] * args.nudge_ramp_decay,
                        )
                    if current_leader_label:
                        leader_locked[current_leader_label] = False
                        scale_map[current_leader_label] = ramp_start
                if sign != 0:
                    prev_delta_sign = sign

                if (
                    current_leader_label
                    and prev_leader_label is not None
                    and current_leader_label != prev_leader_label
                ):
                    leader_locked[prev_leader_label] = True
                    scale_map[prev_leader_label] = max(
                        ramp_start,
                        scale_map[prev_leader_label] * args.nudge_ramp_decay,
                    )
                    leader_locked[current_leader_label] = False
                if current_leader_label:
                    prev_leader_label = current_leader_label

                if (
                    current_leader_label
                    and abs(delta_ms) <= args.nudge_ramp_lock
                ):
                    leader_locked[current_leader_label] = True
                    scale_map[current_leader_label] = max(
                        ramp_start,
                        scale_map[current_leader_label] * args.nudge_ramp_decay,
                    )

                if (
                    nudging_active
                    and current_leader_label is not None
                    and current_leader_label in scale_map
                    and not leader_locked.get(current_leader_label, False)
                    and args.nudge_ramp_step > 0.0
                ):
                    new_scale = min(
                        ramp_max,
                        scale_map[current_leader_label] + args.nudge_ramp_step,
                    )
                    if new_scale > scale_map[current_leader_label]:
                        scale_map[current_leader_label] = new_scale
                        LOG.info(
                            "Increasing nudge ramp factor for %s to %.2f",
                            current_leader_label,
                            new_scale,
                        )

            # Closed-loop controller: overload the leading camera's VC bus when
            # the phase error exceeds the desired tolerance. Depending on
            # --nudge-gain we may batch several heavy nudges in one iteration.
            if nudging_active:
                error_ms = abs(delta_ms) - args.tolerance_ms
                if error_ms > 0.0:
                    leader_label = "left" if delta_ms < 0 else "right"
                    leader_cam = left_cam if leader_label == "left" else right_cam
                    leader_cfg = left_exposure if leader_label == "left" else right_exposure

                    accumulator = (
                        nudge_integral_left if leader_label == "left" else nudge_integral_right
                    )
                    bursts = 1
                    leader_scale = scale_map.get(leader_label, args.nudge_ramp_start)
                    effective_scale = max(0.0, min(1.0, leader_scale))
                    effective_gain = args.nudge_gain * effective_scale
                    if effective_scale <= 0.0 and effective_gain <= 0.0:
                        continue
                    effective_iterations = args.nudge_iterations
                    if effective_iterations > 0:
                        effective_iterations = max(
                            1,
                            int(math.ceil(effective_iterations * max(effective_scale, 0.01))),
                        )

                    if effective_gain > 0.0:
                        accumulator += error_ms * effective_gain
                        bursts = int(accumulator)
                        if bursts <= 0:
                            bursts = 1
                        bursts = min(bursts, args.nudge_max_per_cycle)

                    applied = 0
                    for _ in range(bursts):
                        if heavy_nudge(
                            leader_cam,
                            leader_cfg,
                            iterations=effective_iterations,
                            step_units=args.nudge_step_units,
                            label=leader_label,
                        ):
                            applied += 1
                        else:
                            break

                    if applied > 0:
                        nudges_applied += applied
                        nudge_events += 1
                        if args.nudge_gain > 0.0:
                            accumulator = max(0.0, accumulator - applied)
                            if leader_label == "left":
                                nudge_integral_left = accumulator
                            else:
                                nudge_integral_right = accumulator
                        if (
                            not calibration_done
                            and leader_label in scale_map
                            and not leader_locked.get(leader_label, False)
                        ):
                            scale_map[leader_label] = min(
                                ramp_max,
                                scale_map[leader_label] + args.nudge_ramp_step,
                            )
                        LOG.debug(
                            "Δ=%+.3f ms beyond tolerance %.3f ms; nudged %s camera x%d (error=%.3f ms, eff_gain=%.3f, eff_iter=%d, accumulator=%.3f)",
                            delta_ms,
                            args.tolerance_ms,
                            leader_label,
                            applied,
                            error_ms,
                            effective_gain,
                            effective_iterations,
                            accumulator,
                        )

            # Calibration phase: collect an initial window of deltas and, once
            # enough pairs have been gathered, derive an integer frame offset
            # between the two streams. We then adjust the buffers by dropping
            # whole frames on the leading side so that steady-state pairing
            # starts from a near-zero phase baseline.
            if not calibration_done:
                calibration_deltas.append(delta_ms)
                if len(calibration_deltas) < calibration_pairs:
                    continue

                n_calib = len(calibration_deltas)
                avg = sum(calibration_deltas) / n_calib
                if n_calib > 1:
                    var = sum((d - avg) ** 2 for d in calibration_deltas) / (n_calib - 1)
                    std = math.sqrt(var)
                else:
                    std = 0.0
                LOG.info(
                    "Calibration window: avg Δ=%.3f ms std=%.3f ms over %d pairs (tolerance=%.3f ms)",
                    avg,
                    std,
                    n_calib,
                    args.tolerance_ms,
                )

                # Decide which camera is ahead based on the average offset.
                leader = "left" if avg < 0 else "right"
                effective_fps = getattr(args, "effective_fps", None)
                frame_period_ms: Optional[float] = None
                if effective_fps and effective_fps > 0.0:
                    frame_period_ms = 1000.0 / effective_fps

                frames_offset = 0
                if frame_period_ms:
                    frames_offset = int(round(avg / frame_period_ms))

                if frames_offset != 0 and frame_period_ms:
                    frames_to_drop = abs(frames_offset)
                    # Guard against extreme offsets; these usually indicate a
                    # deeper transport issue rather than a simple phase error.
                    max_shift = 10
                    if frames_to_drop > max_shift:
                        LOG.warning(
                            "Computed frame offset %d exceeds safety limit (%d); "
                            "clamping to %d",
                            frames_to_drop,
                            max_shift,
                            max_shift,
                        )
                        frames_to_drop = max_shift
                    buffer_shift_leader = leader
                    buffer_shift_remaining = frames_to_drop
                    buffer_shift_total = frames_to_drop
                    LOG.info(
                        "Calibration suggests %d-frame offset (avg Δ=%.3f ms, frame_period=%.3f ms); "
                        "will drop %d frame(s) from %s buffer before steady-state pairing",
                        frames_offset,
                        avg,
                        frame_period_ms,
                        buffer_shift_total,
                        buffer_shift_leader,
                    )
                else:
                    if frame_period_ms is not None:
                        frame_period_desc = f"{frame_period_ms:.3f} ms"
                    else:
                        frame_period_desc = "unknown"
                    LOG.info(
                        "Calibration found no integer frame offset from avg Δ=%.3f ms "
                        "(frame_period=%s); keeping buffers aligned as-is",
                        avg,
                        frame_period_desc,
                    )

                calibration_done = True

                if buffer_shift_total == 0:
                    # No buffer shift needed; start post-calibration stats immediately.
                    post_stats_enabled = True
                    nudging_active = False
                    LOG.info(
                        "Calibration phase completed (no buffer shift); "
                        "entering steady-state pairing (mode=%s)",
                        args.pairing_mode,
                    )
                    if pairing_monitor:
                        LOG.info(
                            "Monitor mode: frame drops disabled after calibration; pairing FIFO on every frame"
                        )
                else:
                    LOG.info(
                        "Calibration phase completed; pending buffer shift of %d frame(s) on %s "
                        "before entering steady-state pairing (mode=%s)",
                        buffer_shift_total,
                        buffer_shift_leader,
                        args.pairing_mode,
                    )
                nudging_active = False
                continue

            # After calibration is complete we keep running the nudge
            # controller, but steady-state statistics only start once the
            # buffer-alignment barrier has been lifted.
            if post_stats_enabled:
                post_pairs += 1
                post_sum_delta_ms += delta_ms
                post_sum_sq_delta_ms += delta_ms * delta_ms
            continue

    except KeyboardInterrupt:
        LOG.info("Interrupted by user")
    finally:
        stop_event.set()
        left_thread.join(timeout=1)
        right_thread.join(timeout=1)

        # Best-effort restore of original exposure and auto mode.
        for cam, cfg, label in (
            (left_cam, left_exposure, "left"),
            (right_cam, right_exposure, "right"),
        ):
            try:
                if cfg.original_exposure is not None:
                    cam.set_control(cfg.exposure_ctrl, cfg.original_exposure)
                    LOG.info("Restored original exposure on %s", label)
            except Exception:
                pass

        left_cam.close()
        right_cam.close()

        if post_stats_enabled and post_pairs > 0:
            avg_post = post_sum_delta_ms / post_pairs
            if post_pairs > 1:
                mean_sq = post_sum_sq_delta_ms / post_pairs
                var_post = max(0.0, mean_sq - avg_post * avg_post)
                std_post = math.sqrt(var_post)
            else:
                std_post = 0.0
            LOG.info(
                "Post-calibration statistics: avg Δ=%.3f ms std=%.3f ms over %d pairs",
                avg_post,
                std_post,
                post_pairs,
            )

        # Always report overall statistics for the run, even if calibration
        # did not complete or no exposure adjustments were applied.
        if global_pairs > 0:
            avg_all = global_sum_delta_ms / global_pairs
            if global_pairs > 1:
                mean_sq_all = global_sum_sq_delta_ms / global_pairs
                var_all = max(0.0, mean_sq_all - avg_all * avg_all)
                std_all = math.sqrt(var_all)
            else:
                std_all = 0.0
            LOG.info(
                "Run statistics: avg Δ=%.3f ms std=%.3f ms over %d pairs",
                avg_all,
                std_all,
                global_pairs,
            )

        # Report sync-level drop ratios for the post-calibration phase only so
        # that callers can reason about the effective FPS impact of the pairing
        # strategy in steady state (calibration drops are part of the algorithm
        # and are not included here).
        if sync_total_left_frames > 0 or sync_total_right_frames > 0:
            total_frames = sync_total_left_frames + sync_total_right_frames
            total_drops = sync_drops_left + sync_drops_right
            drop_ratio = (total_drops / total_frames) if total_frames > 0 else 0.0
            left_ratio = (
                (sync_drops_left / sync_total_left_frames) if sync_total_left_frames > 0 else 0.0
            )
            right_ratio = (
                (sync_drops_right / sync_total_right_frames) if sync_total_right_frames > 0 else 0.0
            )
            LOG.info(
                "Sync drop statistics (post-calibration): left=%d/%d (%.2f%%) right=%d/%d (%.2f%%) combined=%d/%d (%.2f%%)",
                sync_drops_left,
                sync_total_left_frames,
                left_ratio * 100.0,
                sync_drops_right,
                sync_total_right_frames,
                right_ratio * 100.0,
                total_drops,
                total_frames,
                drop_ratio * 100.0,
            )

        LOG.info("Heavy nudges applied: %d across %d control events", nudges_applied, nudge_events)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
