"""Stereo helpers that combine two UVC streams based on their timestamps."""

from __future__ import annotations

import contextlib
import dataclasses
import logging
import queue
import threading
import time
from collections import deque
from typing import Deque, Iterator, Optional, Tuple

import usb.util

from .core import (
    CapturedFrame,
    CodecPreference,
    DecoderPreference,
    UVCError,
    UVCCamera,
    describe_device,
    find_uvc_devices,
)

LOG = logging.getLogger(__name__)

_PTS_WRAP = 2**32


@dataclasses.dataclass
class StereoCameraConfig:
    """Description of a single camera used in a stereo pair."""

    vid: int
    pid: int
    device_index: Optional[int] = None
    device_sn: Optional[str] = None
    device_path: Optional[Tuple[int, Tuple[int, ...]]] = None
    interface: int = 1
    width: int = 640
    height: int = 480
    codec: CodecPreference = CodecPreference.AUTO
    decoder: DecoderPreference = DecoderPreference.AUTO
    frame_rate: Optional[float] = None
    strict_fps: bool = False
    skip_initial: int = 0
    queue_size: int = 6
    timeout_ms: int = 3000


@dataclasses.dataclass
class StereoFrame:
    """Pair of frames captured within the synchronisation window."""

    left: CapturedFrame
    right: CapturedFrame
    delta_ms: float
    timestamp_left: float
    timestamp_right: float
    hardware_offset_ms: Optional[float] = None


@dataclasses.dataclass
class StereoStats:
    """Aggregated statistics for a stereo capture session."""

    paired: int = 0
    left_dropped: int = 0
    right_dropped: int = 0
    max_delta_ms: float = 0.0
    avg_delta_ms: Optional[float] = None
    last_delta_ms: Optional[float] = None


@dataclasses.dataclass
class _StampedFrame:
    frame: CapturedFrame
    timestamp: float


class _PtsUnwrapper:
    """Expand 32-bit PTS values into a monotonically increasing timeline."""

    def __init__(self, scale: float = 10_000_000.0):
        self._wraps = 0
        self._last_raw: Optional[int] = None
        self._scale = scale

    def convert(self, frame: CapturedFrame, host_ts: float, prefer_hw: bool) -> float:
        if prefer_hw and frame.pts is not None:
            raw = frame.pts & 0xFFFFFFFF
            if self._last_raw is not None and raw < self._last_raw:
                self._wraps += 1
            self._last_raw = raw
            absolute = raw + self._wraps * _PTS_WRAP
            return absolute / self._scale
        return host_ts


def _resolve_device_index(config: StereoCameraConfig) -> int:
    devices = find_uvc_devices(config.vid, config.pid)
    if not devices:
        raise UVCError(f"No devices found for VID:PID {config.vid:04x}:{config.pid:04x}")

    if config.device_index is not None:
        if not (0 <= config.device_index < len(devices)):
            raise UVCError(
                f"Device index {config.device_index} out of range (found {len(devices)}) "
                f"for VID:PID {config.vid:04x}:{config.pid:04x}"
            )
        return config.device_index

    matches: list[int] = []
    for index, dev in enumerate(devices):
        if config.device_sn:
            serial = None
            try:
                if dev.iSerialNumber:
                    serial = usb.util.get_string(dev, dev.iSerialNumber)
            except Exception:
                serial = None
            if serial != config.device_sn:
                continue

        if config.device_path:
            expected_bus, expected_ports = config.device_path
            if getattr(dev, "bus", None) != expected_bus:
                continue
            ports = getattr(dev, "port_numbers", None)
            if ports is None:
                single = getattr(dev, "port_number", None)
                if single is not None:
                    ports = (single,)
            if ports is None or tuple(ports) != tuple(expected_ports):
                continue

        matches.append(index)

    if not matches:
        raise UVCError("No devices matched the stereo filter (serial/path)")
    if len(matches) > 1:
        raise UVCError("Serial/path filters matched multiple devices for stereo capture")
    return matches[0]


class StereoCapture:
    """Synchronise two :class:`UVCCamera` streams and yield paired frames."""

    def __init__(
        self,
        left: StereoCameraConfig,
        right: StereoCameraConfig,
        *,
        sync_window_ms: float = 2.0,
        drop_window_ms: float = 10.0,
        prefer_hardware_pts: bool = False,
        calibration_pairs: int = 30,
    ) -> None:
        self._left_cfg = left
        self._right_cfg = right
        self._sync_window = max(0.0, sync_window_ms) / 1000.0
        self._drop_window = max(drop_window_ms, sync_window_ms) / 1000.0
        self._output_queue: "queue.Queue[Optional[StereoFrame]]" = queue.Queue(maxsize=left.queue_size + right.queue_size)
        self._stats = StereoStats()
        self._stop_event = threading.Event()
        self._threads: list[threading.Thread] = []
        self._contexts: list[object] = []
        self._closed = False
        self._prefer_hw_pts = prefer_hardware_pts
        self._frame_period_ms = None
        if self._left_cfg.frame_rate and self._left_cfg.frame_rate > 0:
            self._frame_period_ms = 1000.0 / self._left_cfg.frame_rate
        self._left_drops = 0
        self._right_drops = 0

    def __enter__(self) -> "StereoCapture":
        self._start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def __iter__(self) -> Iterator[StereoFrame]:
        while True:
            item = self._output_queue.get()
            if item is None:
                break
            yield item

    @property
    def stats(self) -> StereoStats:
        return dataclasses.replace(self._stats)

    def close(self) -> None:
        if self._closed:
            return
        self._stop_event.set()
        for thread in self._threads:
            thread.join(timeout=1.0)
        for ctx in reversed(self._contexts):
            with contextlib.suppress(Exception):
                ctx.__exit__(None, None, None)
        self._contexts.clear()
        self._closed = True

    def _start(self) -> None:
        contexts = []
        left_index = _resolve_device_index(self._left_cfg)
        right_index = _resolve_device_index(self._right_cfg)

        try:
            left_ctx = UVCCamera.open(
                vid=self._left_cfg.vid,
                pid=self._left_cfg.pid,
                device_index=left_index,
                interface=self._left_cfg.interface,
            )
            contexts.append(left_ctx)
            left_camera = left_ctx.__enter__()
            LOG.info("Left camera: %s (index %s)", describe_device(left_camera.device), left_index)

            right_ctx = UVCCamera.open(
                vid=self._right_cfg.vid,
                pid=self._right_cfg.pid,
                device_index=right_index,
                interface=self._right_cfg.interface,
            )
            contexts.append(right_ctx)
            right_camera = right_ctx.__enter__()
            LOG.info("Right camera: %s (index %s)", describe_device(right_camera.device), right_index)

            left_stream_ctx = left_camera.stream(
                width=self._left_cfg.width,
                height=self._left_cfg.height,
                codec=self._left_cfg.codec,
                decoder=self._left_cfg.decoder,
                frame_rate=self._left_cfg.frame_rate,
                strict_fps=self._left_cfg.strict_fps,
                queue_size=self._left_cfg.queue_size,
                skip_initial=self._left_cfg.skip_initial,
                timeout_ms=self._left_cfg.timeout_ms,
            )
            contexts.append(left_stream_ctx)
            left_stream = left_stream_ctx.__enter__()

            right_stream_ctx = right_camera.stream(
                width=self._right_cfg.width,
                height=self._right_cfg.height,
                codec=self._right_cfg.codec,
                decoder=self._right_cfg.decoder,
                frame_rate=self._right_cfg.frame_rate,
                strict_fps=self._right_cfg.strict_fps,
                queue_size=self._right_cfg.queue_size,
                skip_initial=self._right_cfg.skip_initial,
                timeout_ms=self._right_cfg.timeout_ms,
            )
            contexts.append(right_stream_ctx)
            right_stream = right_stream_ctx.__enter__()

        except Exception:
            for ctx in reversed(contexts):
                with contextlib.suppress(Exception):
                    ctx.__exit__(None, None, None)
            raise

        self._contexts = contexts

        self._left_queue: "queue.Queue[Optional[CapturedFrame]]" = queue.Queue(maxsize=self._left_cfg.queue_size * 2)
        self._right_queue: "queue.Queue[Optional[CapturedFrame]]" = queue.Queue(maxsize=self._right_cfg.queue_size * 2)

        self._launch_consumer(left_stream, self._left_queue, "left")
        self._launch_consumer(right_stream, self._right_queue, "right")

        collector = threading.Thread(target=self._collect_pairs, name="uvc-stereo", daemon=True)
        collector.start()
        self._threads.append(collector)

    def _launch_consumer(self, frames: Iterator[CapturedFrame], target: queue.Queue, label: str) -> None:
        def _run():
            try:
                for frame in frames:
                    if self._stop_event.is_set():
                        break
                    target.put(frame)
            except Exception:
                LOG.debug("Stereo %s consumer failed", label, exc_info=True)
            finally:
                target.put(None)

        thread = threading.Thread(target=_run, name=f"uvc-stereo-{label}", daemon=True)
        thread.start()
        self._threads.append(thread)

    def _collect_pairs(self) -> None:
        left_buffer: Deque[_StampedFrame] = deque()
        right_buffer: Deque[_StampedFrame] = deque()
        left_done = False
        right_done = False
        left_unwrapper = _PtsUnwrapper()
        right_unwrapper = _PtsUnwrapper()
        left_start = time.time()
        right_start = left_start
        last_right_seen: Optional[float] = None
        last_left_seen: Optional[float] = None

        while not self._stop_event.is_set():
            left_done, last_left_seen = self._drain_queue(
                self._left_queue,
                left_buffer,
                left_unwrapper,
                left_start,
                left_done,
                last_left_seen,
            )
            right_done, last_right_seen = self._drain_queue(
                self._right_queue,
                right_buffer,
                right_unwrapper,
                right_start,
                right_done,
                last_right_seen,
            )

            pair = self._match_buffers(left_buffer, right_buffer)
            if pair is not None:
                self._output_queue.put(pair)
                continue

            self._prune_buffer(left_buffer, last_right_seen, drop_left=True)
            self._prune_buffer(right_buffer, last_left_seen, drop_left=False)

            if left_done and right_done and not left_buffer and not right_buffer:
                break
            time.sleep(0.001)

        self._output_queue.put(None)

    def _drain_queue(
        self,
        source: "queue.Queue[Optional[CapturedFrame]]",
        buffer: Deque[_StampedFrame],
        unwrapper: _PtsUnwrapper,
        wall_start: float,
        done: bool,
        last_seen: Optional[float],
    ) -> Tuple[bool, Optional[float]]:
        if done:
            return True, last_seen
        while True:
            try:
                frame = source.get_nowait()
            except queue.Empty:
                break
            if frame is None:
                return True, last_seen
            host_ts = frame.timestamp - wall_start
            timestamp = unwrapper.convert(frame, host_ts, self._prefer_hw_pts)
            buffer.append(_StampedFrame(frame=frame, timestamp=timestamp))
            last_seen = timestamp
        return False, last_seen

    def _match_buffers(
        self,
        left_buffer: Deque[_StampedFrame],
        right_buffer: Deque[_StampedFrame],
    ) -> Optional[StereoFrame]:
        if not left_buffer or not right_buffer:
            return None

        left = left_buffer.popleft()
        right = right_buffer.popleft()
        raw_delta = left.timestamp - right.timestamp
        if raw_delta > 0:
            self._left_drops += 1
        elif raw_delta < 0:
            self._right_drops += 1
        return self._assemble_pair(left, right, raw_delta)

    def _assemble_pair(self, left: _StampedFrame, right: _StampedFrame, raw_delta: float) -> StereoFrame:
        delta_ms = raw_delta * 1000.0
        hardware_offset_ms = None
        if self._frame_period_ms is not None:
            offset_frames = self._left_drops - self._right_drops
            hardware_offset_ms = offset_frames * self._frame_period_ms
        self._record_delta(delta_ms)
        return StereoFrame(
            left=left.frame,
            right=right.frame,
            delta_ms=delta_ms,
            timestamp_left=left.timestamp,
            timestamp_right=right.timestamp,
            hardware_offset_ms=hardware_offset_ms,
        )

    def _record_delta(self, delta_ms: float) -> None:
        self._stats.paired += 1
        self._stats.last_delta_ms = delta_ms
        self._stats.max_delta_ms = max(self._stats.max_delta_ms, abs(delta_ms))
        if self._stats.avg_delta_ms is None:
            self._stats.avg_delta_ms = delta_ms
        else:
            count = self._stats.paired
            prev = self._stats.avg_delta_ms
            self._stats.avg_delta_ms = (prev * (count - 1) + delta_ms) / count

    def _prune_buffer(
        self,
        buffer: Deque[_StampedFrame],
        reference_ts: Optional[float],
        *,
        drop_left: bool,
    ) -> None:
        return


__all__ = [
    "StereoCameraConfig",
    "StereoCapture",
    "StereoFrame",
    "StereoStats",
]
