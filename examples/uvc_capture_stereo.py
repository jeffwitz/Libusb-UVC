#!/usr/bin/env python3
"""Stereo capture variant with dynamic calibration and pairing modes."""

from __future__ import annotations

import argparse
import contextlib
import csv
import logging
import queue
import threading
import time
from dataclasses import dataclass
from numbers import Number
from pathlib import Path
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
    decoded_frame: Optional[np.ndarray] = None


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
    probe_barrier: threading.Barrier,
    commit_barrier: threading.Barrier,
    start_event: threading.Event,
    restart_event: threading.Event,
    args: argparse.Namespace,
    label: str,
    core_id: Optional[int] = None,
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

        while not stop_event.is_set():
            try:
                # 1. PROBE (Variable time)
                stream_format, frame = camera.select_stream(
                    width=args.width,
                    height=args.height,
                    codec=args.codec,
                )
                negotiation = camera.probe_stream(stream_format, frame, frame_rate=args.fps if args.fps > 0 else None)
                
                # 2. SYNC
                try:
                    probe_barrier.wait(timeout=5.0)
                except threading.BrokenBarrierError:
                    if stop_event.is_set():
                        break
                    probe_barrier.reset()
                    continue

                # 3. COMMIT (Fast & Deterministic)
                # Optional per-camera delay lets us intentionally skew the COMMIT
                # time between left/right devices to probe hardware behaviour.
                commit_delay = 0.0
                if label == "left":
                    commit_delay = getattr(args, "left_commit_delay", 0.0)
                elif label == "right":
                    commit_delay = getattr(args, "right_commit_delay", 0.0)
                if commit_delay > 0:
                    time.sleep(commit_delay)

                camera.commit_stream(negotiation)
                try:
                    commit_barrier.wait(timeout=5.0)
                except threading.BrokenBarrierError:
                    if stop_event.is_set():
                        break
                    commit_barrier.reset()
                    continue

                # 4. STREAM (Skip configuration)
                if label == "left":
                    record_path = getattr(args, "record_left", None)
                elif label == "right":
                    record_path = getattr(args, "record_right", None)
                else:
                    record_path = None

                stream = camera.stream(
                    width=args.width,
                    height=args.height,
                    codec=args.codec,
                    decoder=args.decoder,
                    frame_rate=args.fps if args.fps > 0 else None,
                    queue_size=args.stream_queue,
                    record_to=record_path,
                    configure=False,  # Already configured
                )
                
                start_event.wait()

                with stream as frames:
                    for frame in frames:
                        if stop_event.is_set() or restart_event.is_set():
                            break
                        
                        decoded = None
                        if args.display and cv2 is not None:
                            # Prefer decoded RGB frames from a backend (PyAV/GStreamer)
                            # when available, and fall back to to_bgr() only for
                            # formats that support inline conversion.
                            try:
                                base = getattr(frame, "decoded", None)
                                if base is not None:
                                    import numpy as _np

                                    arr = _np.array(base, copy=False)
                                    if arr.ndim == 3 and arr.shape[2] == 3:
                                        decoded = cv2.cvtColor(arr, cv2.COLOR_RGB2BGR)
                                if decoded is None:
                                    # Safe fallback for uncompressed / MJPEG
                                    decoded = frame.to_bgr()
                            except RuntimeError as exc:
                                # Unsupported codec for conversion (e.g. frame-based H.264
                                # without a working decoder).  Skip preview for this frame
                                # but keep it in the synchronisation pipeline.
                                LOG.debug("Preview conversion failed for %s: %s", label, exc)
                                decoded = None

                        packet = FramePacket(
                            frame=frame,
                            host_ts=time.perf_counter(),
                            pts=frame.pts,
                            decoded_frame=decoded,
                        )
                        try:
                            frame_queue.put_nowait(packet)
                        except queue.Full:
                            try:
                                frame_queue.get_nowait()
                            except queue.Empty:
                                pass
                            frame_queue.put_nowait(packet)
                
                if stop_event.is_set():
                    break
                
                if restart_event.is_set():
                    LOG.info("%s restarting stream...", label)
                    # Wait for main thread to clear the restart signal before retrying
                    while restart_event.is_set() and not stop_event.is_set():
                        time.sleep(0.1)
                    continue

            except Exception as exc:
                LOG.error("Producer %s loop error: %s", label, exc)
                if stop_event.is_set():
                    break
                time.sleep(1.0)

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
    parser = argparse.ArgumentParser(description="Stereo preview (deterministic start)")
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
    parser.add_argument(
        "--left-core",
        type=int,
        default=None,
        help="CPU core to pin the left camera thread to",
    )
    parser.add_argument(
        "--right-core",
        type=int,
        default=None,
        help="CPU core to pin the right camera thread to",
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
    parser.add_argument(
        "--stats-interval",
        type=int,
        default=0,
        help="Pairs between stats logs (0 disables)",
    )
    parser.add_argument("--display", action="store_true", help="Show OpenCV preview")
    parser.add_argument("--duration", type=float, help="Automatically stop after the given seconds")
    parser.add_argument("--log-level", default="INFO")

    # Optional COMMIT skew (applied before sending the UVC COMMIT command).
    # Positive values sleep on the given side, effectively delaying when that
    # camera starts streaming relative to its peer.
    parser.add_argument(
        "--left-commit-delay-ms",
        type=float,
        default=0.0,
        help="Extra delay before COMMIT on the left camera (ms)",
    )
    parser.add_argument(
        "--right-commit-delay-ms",
        type=float,
        default=0.0,
        help="Extra delay before COMMIT on the right camera (ms)",
    )
    parser.add_argument(
        "--record-left",
        type=Path,
        help="Write compressed payloads from the left camera to this file (no re-encoding)",
    )
    parser.add_argument(
        "--record-right",
        type=Path,
        help="Write compressed payloads from the right camera to this file (no re-encoding)",
    )
    parser.add_argument(
        "--csv",
        type=Path,
        help="Write paired timestamp measurements (host & PTS) to this CSV file",
    )
    
    # Restart-to-Sync arguments
    parser.add_argument(
        "--restart-threshold-ms",
        type=float,
        default=0.0,
        help="Max allowed host delta at startup (ms). If exceeded, restart streams. 0 disables.",
    )
    parser.add_argument(
        "--max-retries",
        type=int,
        default=5,
        help="Maximum number of restart attempts",
    )
    parser.add_argument(
        "--verdict-pairs",
        type=int,
        default=10,
        help="Number of frame pairs used to estimate startup offset for restart logic",
    )
    parser.add_argument(
        "--warmup-frames",
        type=int,
        default=20,
        help="Number of frames to ignore before checking alignment",
    )

    args = parser.parse_args()
    logging.basicConfig(level=args.log_level.upper())

    if args.device_id:
        args.device_id = parse_device_id(args.device_id)

    args.codec = _normalise_codec(args.codec)
    args.decoder = _normalise_decoder(args.decoder)
    # Recording requires a decoder backend for MJPEG/H.26x so that the recorder
    # can mux the compressed payloads into AVI/MKV containers without
    # re-encoding. When the user requests recording but leaves the decoder at
    # 'auto', default to PyAV as in uvc_capture_video.py.
    if (args.record_left or args.record_right) and args.decoder == DecoderPreference.AUTO:
        args.decoder = DecoderPreference.PYAV
    args.pairing_mode = args.pairing_mode.lower()
    args.restart_threshold = args.restart_threshold_ms / 1000.0 if args.restart_threshold_ms > 0 else None
    # Store delays in seconds for internal use; keep CLI units in ms.
    args.left_commit_delay = max(0.0, args.left_commit_delay_ms / 1000.0)
    args.right_commit_delay = max(0.0, args.right_commit_delay_ms / 1000.0)
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
    
    # Barriers for synchronized PROBE and COMMIT
    probe_barrier = threading.Barrier(2)
    commit_barrier = threading.Barrier(2)
    start_event = threading.Event()
    restart_event = threading.Event()

    left_thread = threading.Thread(
        target=frame_producer,
        args=(
            left_cam,
            left_queue,
            stop_event,
            probe_barrier,
            commit_barrier,
            start_event,
            restart_event,
            args,
            "left",
            args.left_core,
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
            probe_barrier,
            commit_barrier,
            start_event,
            restart_event,
            args,
            "right",
            args.right_core,
        ),
        name="right-producer",
        daemon=True,
    )
    left_thread.start()
    right_thread.start()

    # Wait for threads to be ready (optional, threads manage their own barriers)
    time.sleep(1.0) 
    start_event.set()
    deadline = time.time() + args.duration if args.duration and args.duration > 0 else None

    left_frame: Optional[FramePacket] = None
    right_frame: Optional[FramePacket] = None
    drain_latest = args.pairing_mode == "latest"

    pair_count = 0
    drop_left = 0
    drop_right = 0
    stats_next = args.stats_interval if args.stats_interval else None

    # Target delta is always 0.0 for deterministic start; kept as a
    # separate variable to allow future tuning of the pairing offset.
    target_delta = 0.0
    
    # Verdict Phase variables
    verdict_pairs_needed = args.verdict_pairs if args.restart_threshold else 0
    warmup_frames = args.warmup_frames
    warmup_done = False
    verdict_deltas = []
    retries_remaining = args.max_retries

    # CSV export state (independent of video recording). When enabled, we log
    # the per-pair host and PTS timestamps, normalised so that t=0 corresponds
    # to the first accepted pair.
    csv_rows: list[list[str]] = []
    csv_t0_host: Optional[float] = None
    csv_t0_pts_left: Optional[float] = None
    csv_t0_pts_right: Optional[float] = None

    if args.display and cv2 is None:
        raise RuntimeError("OpenCV is required when --display is specified")
    if args.display:
        cv2.namedWindow("stereo3", cv2.WINDOW_NORMAL)

    try:
        while not stop_event.is_set():
            if deadline and time.time() >= deadline:
                LOG.info("Duration %.2fs reached; stopping capture", args.duration)
                break
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

            # Verdict Phase Logic
            if verdict_pairs_needed > 0:
                if not warmup_done and warmup_frames > 0:
                    warmup_frames -= 1
                    # During warmup, we just consume frames.
                    # We can optionally print deltas to see the "settling" process
                    if args.print_deltas:
                        print(f"[Warmup {args.warmup_frames - warmup_frames}/{args.warmup_frames}] Δhost={(host_delta)*1000:+.3f} ms")
                else:
                    verdict_deltas.append(host_delta)
                    verdict_pairs_needed -= 1
                    if verdict_pairs_needed == 0:
                        avg_delta = sum(verdict_deltas) / len(verdict_deltas)
                        LOG.info("Verdict: avg_delta=%.3f ms (threshold=%.3f ms)", avg_delta * 1000, args.restart_threshold * 1000)
                        warmup_done = True
                        
                        if abs(avg_delta) > args.restart_threshold:
                            if retries_remaining > 0:
                                LOG.warning("Alignment failed. RESTARTING streams... (%d retries left)", retries_remaining)
                                retries_remaining -= 1
                                
                                # Trigger restart
                                restart_event.set()
                                start_event.clear()
                                
                                # Wait a bit for threads to react
                                time.sleep(0.5)
                                
                                # Flush queues
                                _flush_queue(left_queue)
                                _flush_queue(right_queue)
                                left_frame = None
                                right_frame = None
                                verdict_deltas = []
                                verdict_pairs_needed = args.verdict_pairs
                                warmup_frames = 0  # No additional warmup after first run
                                
                                # Reset barriers (threads handle reset, but we ensure state is clean)
                                # Release restart signal
                                restart_event.clear()
                                start_event.set()
                                continue
                            else:
                                LOG.error("Max retries reached. Proceeding with best effort.")
                        else:
                            LOG.info("Alignment accepted.")

            effective_delta = host_delta - target_delta
            if abs(effective_delta) > args.max_ts_diff:
                if effective_delta < 0:
                    # Left frame is earlier; drop it so right can catch up.
                    left_frame = None
                    drop_left += 1
                else:
                    # Right frame is earlier; drop it so left can catch up.
                    right_frame = None
                    drop_right += 1
                continue

            if args.csv and verdict_pairs_needed == 0:
                # Establish host time zero on the earliest timestamp in the
                # first accepted pair.
                if csv_t0_host is None:
                    csv_t0_host = min(left_frame.host_ts, right_frame.host_ts)
                t_left_host = left_frame.host_ts - csv_t0_host
                t_right_host = right_frame.host_ts - csv_t0_host

                # Normalise PTS timelines independently for each camera so that
                # t=0 matches the first observed PTS on that side, when
                # available. When PTS is absent or malformed we leave the field
                # blank in the CSV.
                t_left_pts_rel: Optional[float] = None
                t_right_pts_rel: Optional[float] = None
                if left_pts_sec is not None:
                    if csv_t0_pts_left is None:
                        csv_t0_pts_left = left_pts_sec
                    t_left_pts_rel = left_pts_sec - csv_t0_pts_left
                if right_pts_sec is not None:
                    if csv_t0_pts_right is None:
                        csv_t0_pts_right = right_pts_sec
                    t_right_pts_rel = right_pts_sec - csv_t0_pts_right

                delta_host_ms = host_delta * 1000.0
                delta_pts_ms = pts_delta * 1000.0 if pts_delta is not None else None

                row = [
                    str(pair_count + 1),
                    f"{t_left_host:.9f}",
                    f"{t_right_host:.9f}",
                    f"{delta_host_ms:.3f}",
                    "" if t_left_pts_rel is None else f"{t_left_pts_rel:.9f}",
                    "" if t_right_pts_rel is None else f"{t_right_pts_rel:.9f}",
                    "" if delta_pts_ms is None else f"{delta_pts_ms:.3f}",
                ]
                csv_rows.append(row)

            if args.print_deltas and verdict_pairs_needed == 0:
                message = f"Δhost={(host_delta)*1000:+.3f} ms"
                if pts_delta is not None:
                    message += f" ΔPTS={pts_delta*1000:+.3f} ms"
                print(message)

            if args.display:
                try:
                    left_bgr = left_frame.decoded_frame
                    right_bgr = right_frame.decoded_frame

                    # If producer did not pre-decode (or decoding failed),
                    # attempt a local conversion but keep it best-effort so
                    # unsupported codecs do not break synchronisation.
                    if left_bgr is None:
                        try:
                            base = getattr(left_frame.frame, "decoded", None)
                            if base is not None:
                                import numpy as _np

                                arr = _np.array(base, copy=False)
                                if arr.ndim == 3 and arr.shape[2] == 3:
                                    left_bgr = cv2.cvtColor(arr, cv2.COLOR_RGB2BGR)
                            if left_bgr is None:
                                left_bgr = left_frame.frame.to_bgr()
                        except RuntimeError as exc:
                            LOG.debug("Left preview conversion failed: %s", exc)
                            left_bgr = None

                    if right_bgr is None:
                        try:
                            base = getattr(right_frame.frame, "decoded", None)
                            if base is not None:
                                import numpy as _np

                                arr = _np.array(base, copy=False)
                                if arr.ndim == 3 and arr.shape[2] == 3:
                                    right_bgr = cv2.cvtColor(arr, cv2.COLOR_RGB2BGR)
                            if right_bgr is None:
                                right_bgr = right_frame.frame.to_bgr()
                        except RuntimeError as exc:
                            LOG.debug("Right preview conversion failed: %s", exc)
                            right_bgr = None

                    if left_bgr is None or right_bgr is None:
                        # No usable preview for this pair; keep stats/deltas only.
                        left_frame = None
                        right_frame = None
                        continue
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
                    target_delta * 1000,
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

        if args.csv and csv_rows:
            header = [
                "pair_index",
                "t_left_host_s",
                "t_right_host_s",
                "delta_host_ms",
                "t_left_pts_s",
                "t_right_pts_s",
                "delta_pts_ms",
            ]
            try:
                with open(args.csv, "w", newline="", encoding="utf-8") as fh:
                    writer = csv.writer(fh)
                    writer.writerow(header)
                    writer.writerows(csv_rows)
                LOG.info("Wrote stereo timing CSV to %s", args.csv)
            except Exception as exc:
                LOG.error("Failed to write CSV to %s: %s", args.csv, exc)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
