#!/usr/bin/env python3
"""Capture synchronised pairs from two identical UVC cameras."""

from __future__ import annotations

import argparse
import logging
import time

from uvc_cli import configure_logging, ensure_repo_import, parse_device_id

ensure_repo_import()

from libusb_uvc import CodecPreference, DecoderPreference  # type: ignore  # pylint: disable=wrong-import-position
from libusb_uvc.stereo import StereoCameraConfig, StereoCapture

try:  # Optional preview dependency
    import cv2  # type: ignore
except Exception:  # pragma: no cover - optional feature
    cv2 = None

LOG = logging.getLogger("stereo_capture")


def _build_camera_config(args, serial: str) -> StereoCameraConfig:
    frame_rate = args.fps if args.fps and args.fps > 0 else None
    return StereoCameraConfig(
        vid=args.vid,
        pid=args.pid,
        device_sn=serial,
        interface=args.interface,
        width=args.width,
        height=args.height,
        codec=args.codec,
        decoder=args.decoder,
        frame_rate=frame_rate,
        strict_fps=args.strict_fps,
        queue_size=args.queue_size,
        skip_initial=args.skip_frames,
        timeout_ms=args.timeout,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Synchronise two UVC cameras for stereo capture")
    parser.add_argument(
        "--device-id",
        required=True,
        help="VID:PID shared by both cameras (hex or decimal, e.g. 32e4:9415)",
    )
    parser.add_argument("--left-device-sn", required=True, help="USB serial number of the left camera")
    parser.add_argument("--right-device-sn", required=True, help="USB serial number of the right camera")
    parser.add_argument("--interface", type=int, default=1, help="Video Streaming interface to use")
    parser.add_argument("--width", type=int, default=1920, help="Frame width")
    parser.add_argument("--height", type=int, default=1080, help="Frame height")
    parser.add_argument("--fps", type=float, default=15.0, help="Target frame rate (Hz)")
    parser.add_argument("--skip-frames", type=int, default=2, help="Frames to skip at stream start")
    parser.add_argument("--timeout", type=int, default=3000, help="Transfer timeout (ms)")
    parser.add_argument(
        "--codec",
        choices=[
            CodecPreference.AUTO,
            CodecPreference.YUYV,
            CodecPreference.MJPEG,
            CodecPreference.FRAME_BASED,
            CodecPreference.H264,
            CodecPreference.H265,
        ],
        default=CodecPreference.AUTO,
        help="Force a specific codec when multiple are available",
    )
    parser.add_argument(
        "--decoder",
        choices=[
            DecoderPreference.AUTO,
            DecoderPreference.NONE,
            DecoderPreference.PYAV,
            DecoderPreference.GSTREAMER,
        ],
        default=DecoderPreference.AUTO,
        help="Select a decoder backend for frame-based formats",
    )
    parser.add_argument("--strict-fps", action="store_true", help="Require an exact FPS match during PROBE")
    parser.add_argument("--sync-window-ms", type=float, default=2.0, help="Maximum PTS delta between paired frames")
    parser.add_argument("--drop-window-ms", type=float, default=10.0, help="Discard frames older than this window")
    parser.add_argument("--queue-size", type=int, default=6, help="Internal per-stream queue size")
    parser.add_argument("--duration", type=float, help="Stop automatically after the given seconds")
    parser.add_argument("--max-pairs", type=int, help="Stop after emitting N stereo pairs")
    parser.add_argument("--log-level", default="INFO")
    parser.add_argument("--print-deltas", action="store_true", help="Print per-pair delta values")
    parser.add_argument(
        "--prefer-hw-pts",
        action="store_true",
        help="Pair frames using hardware PTS when available (default: host clock)",
    )
    parser.add_argument(
        "--calibration-pairs",
        type=int,
        default=120,
        help="Maximum frames discarded during initial alignment (0 = unlimited)",
    )
    parser.add_argument(
        "--display",
        action="store_true",
        help="Show side-by-side OpenCV windows (requires cv2)",
    )
    args = parser.parse_args()

    vid, pid = parse_device_id(args.device_id)
    args.vid = vid
    args.pid = pid

    configure_logging(args.log_level, name="stereo_capture")

    left_cfg = _build_camera_config(args, args.left_device_sn)
    right_cfg = _build_camera_config(args, args.right_device_sn)

    pair_limit = args.max_pairs if args.max_pairs and args.max_pairs > 0 else None
    started_at = time.time()

    try:
        with StereoCapture(
            left_cfg,
            right_cfg,
            sync_window_ms=args.sync_window_ms,
            drop_window_ms=args.drop_window_ms,
            prefer_hardware_pts=args.prefer_hw_pts,
            calibration_pairs=args.calibration_pairs,
        ) as capture:
            count = 0
            for stereo_frame in capture:
                count += 1
                if args.print_deltas:
                    print(
                        f"Pair #{count:04d} Δ={stereo_frame.delta_ms:+.3f} ms "
                        f"(PTS L={stereo_frame.timestamp_left:.6f}s R={stereo_frame.timestamp_right:.6f}s)"
                    )
                if args.display:
                    _show_pair(stereo_frame)
                if pair_limit and count >= pair_limit:
                    break
                if args.duration and (time.time() - started_at) >= args.duration:
                    break
    except KeyboardInterrupt:
        LOG.info("Stereo capture interrupted by user")
    stats = capture.stats if "capture" in locals() else None

    if stats:
        print(
            f"Stereo stats: pairs={stats.paired} "
            f"left-dropped={stats.left_dropped} right-dropped={stats.right_dropped} "
            f"avg-delta={stats.avg_delta_ms or 0:.3f} ms max-delta={stats.max_delta_ms:.3f} ms"
        )

    return 0


def _show_pair(frame) -> None:
    if cv2 is None:
        raise RuntimeError("OpenCV is required for --display")
    try:
        left = frame.left.to_bgr()
        right = frame.right.to_bgr()
    except Exception as exc:
        LOG.warning("Frame conversion failed: %s", exc)
        return
    cv2.namedWindow("stereo-left", cv2.WINDOW_NORMAL)
    cv2.namedWindow("stereo-right", cv2.WINDOW_NORMAL)
    cv2.imshow("stereo-left", left)
    cv2.imshow("stereo-right", right)
    key = cv2.waitKey(1) & 0xFF
    if key in (ord("q"), 27):
        raise KeyboardInterrupt


if __name__ == "__main__":
    raise SystemExit(main())
