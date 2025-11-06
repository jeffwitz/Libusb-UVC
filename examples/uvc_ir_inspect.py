#!/usr/bin/env python3
"""Inspect and capture from the infrared interface of a dual-sensor UVC camera."""

from __future__ import annotations

import argparse
import logging
import pathlib
import sys
from typing import Iterable, Optional

ROOT = pathlib.Path(__file__).resolve().parents[1]
try:
    from libusb_uvc import (
        CodecPreference,
        CapturedFrame,
        UVCCamera,
        UVCError,
        describe_device,
    )
except ImportError:  # pragma: no cover
    sys.path.insert(0, str(ROOT / "src"))
    from libusb_uvc import (
        CodecPreference,
        CapturedFrame,
        UVCCamera,
        UVCError,
        describe_device,
    )


LOG = logging.getLogger("ir_inspect")


def _format_metadata(meta: dict) -> str:
    if not meta:
        return ""
    items = []
    for key, value in sorted(meta.items()):
        if isinstance(value, bytes):
            items.append(f"{key}=0x{value.hex()}")
        else:
            items.append(f"{key}={value}")
    return f"metadata: {', '.join(items)}"


def list_controls(camera: UVCCamera) -> None:
    print("=== Video Control (VC) Interfaces ===")
    controls = camera.enumerate_controls(refresh=True)
    if not controls:
        print("  No validated controls found.")
        return

    def key(entry):
        return (entry.interface_number, entry.unit_id, entry.selector)

    for entry in sorted(controls, key=key):
        details = [f"info=0x{entry.info:02x}"]
        if entry.minimum is not None:
            details.append(f"min={entry.minimum}")
        if entry.maximum is not None:
            details.append(f"max={entry.maximum}")
        if entry.step is not None:
            details.append(f"step={entry.step}")
        if entry.default is not None:
            details.append(f"def={entry.default}")
        if entry.length is not None:
            details.append(f"len={entry.length}")
        meta_desc = _format_metadata(entry.metadata)
        meta_line = f"      {meta_desc}" if meta_desc else None

        print(
            f"  Interface {entry.interface_number} — Unit {entry.unit_id} "
            f"({entry.type}) selector {entry.selector}: {entry.name}"
        )
        print(f"      {', '.join(details)}")
        if meta_line:
            print(meta_line)


def save_frame(frame: CapturedFrame, destination: pathlib.Path, index: int) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    raw_path = destination / f"ir_frame_{index:03d}.raw"
    raw_path.write_bytes(frame.payload)
    LOG.info("Saved raw payload to %s (%d bytes)", raw_path, len(frame.payload))

    # Attempt a best-effort RGB conversion for convenience.
    try:
        rgb = frame.to_rgb()
    except Exception as exc:  # pragma: no cover - depends on optional deps
        LOG.debug("Failed to convert frame %s to RGB: %s", index, exc)
        return

    try:
        from PIL import Image

        img_path = destination / f"ir_frame_{index:03d}.png"
        Image.fromarray(rgb).save(img_path)
        LOG.info("Saved converted PNG to %s", img_path)
    except Exception as exc:  # pragma: no cover - optional dependency
        LOG.debug("Failed to save PNG for frame %s: %s", index, exc)


def capture_ir_stream(
    camera: UVCCamera,
    *,
    width: int,
    height: int,
    codec: CodecPreference,
    fps: Optional[float],
    frames: int,
    output: Optional[pathlib.Path],
    timeout_ms: int,
) -> Iterable[CapturedFrame]:
    stream = camera.stream(
        width=width,
        height=height,
        codec=codec,
        frame_rate=fps if fps and fps > 0 else None,
        strict_fps=False,
        queue_size=frames + 2,
        skip_initial=2,
        timeout_ms=timeout_ms,
        duration=max(timeout_ms / 1000.0, 1.0),
    )

    with stream as iterator:
        for index, frame in enumerate(iterator, start=1):
            yield frame
            if output is not None:
                save_frame(frame, output, index)
            if frames and index >= frames:
                break


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Capture and inspect the infrared interface of a UVC camera"
    )
    parser.add_argument("--vid", type=lambda x: int(x, 0), help="Vendor ID filter")
    parser.add_argument("--pid", type=lambda x: int(x, 0), help="Product ID filter")
    parser.add_argument("--device-index", type=int, default=0, help="Index of the matching device")
    parser.add_argument("--interface", type=int, default=3, help="IR streaming interface number")
    parser.add_argument("--width", type=int, default=400, help="IR frame width")
    parser.add_argument("--height", type=int, default=400, help="IR frame height")
    parser.add_argument("--fps", type=float, default=15.0, help="Target IR frame rate")
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
        default=CodecPreference.YUYV,
        help="Preferred codec when configuring the IR stream",
    )
    parser.add_argument("--frames", type=int, default=1, help="Number of IR frames to capture")
    parser.add_argument(
        "--output-dir",
        type=pathlib.Path,
        help="Optional directory where raw (and PNG when possible) frames are saved",
    )
    parser.add_argument("--timeout", type=int, default=5000, help="Read timeout in milliseconds")
    parser.add_argument("--log-level", default="INFO", help="Logging verbosity")
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
            print(f"Streaming interface: {camera.interface_number}")
            list_controls(camera)

            if args.frames > 0:
                print("\n=== IR Stream ===")
                try:
                    for index, frame in enumerate(
                        capture_ir_stream(
                            camera,
                            width=args.width,
                            height=args.height,
                            codec=args.codec,
                            fps=args.fps,
                            frames=args.frames,
                            output=args.output_dir,
                            timeout_ms=max(1000, args.timeout),
                        ),
                        start=1,
                    ):
                        pts = frame.pts if frame.pts is not None else "?"
                        print(
                            f"  Frame #{index}: payload={len(frame.payload)} bytes "
                            f"fid={frame.fid} pts={pts}"
                        )
                except UVCError as exc:
                    print(f"Failed to capture IR stream: {exc}")
                    return 1

    except UVCError as exc:
        print(f"Unable to open IR interface: {exc}")
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
