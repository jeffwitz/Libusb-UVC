"""Decoders for compressed UVC video payloads.

This module provides a lightweight abstraction around video decoding backends
so that callers can consume H.264/H.265 (frame-based) payloads without being
coupled to a single library.  Backends are discovered lazily and can be
extended in the future (e.g. Media Foundation, VideoToolbox).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Iterable, List, Optional, Tuple

import logging
import threading

try:  # Optional dependency for array conversion when using backends
    import numpy as _np  # type: ignore
except Exception:  # pragma: no cover - optional dependency
    _np = None

LOG = logging.getLogger(__name__)

_H264_START_CODE = b"\x00\x00\x00\x01"


class DecoderError(RuntimeError):
    """Base class for decoder-related exceptions."""


class DecoderUnavailable(DecoderError):
    """Raised when a requested backend cannot be constructed."""


class DecoderBackend(ABC):
    """Common interface implemented by every decoder backend."""

    def __init__(self, format_name: str) -> None:
        self._format_name = format_name

    @abstractmethod
    def decode_packet(self, packet: bytes) -> List[object]:
        """Decode a compressed packet and return a list of RGB frames."""

    def flush(self) -> List[object]:  # pragma: no cover - default implementation
        """Return remaining frames after the stream ends."""

        return []


def _select_gstreamer_pipeline(codec: str) -> Tuple[str, Optional[str]]:
    """Return (pipeline_description, caps) for the requested codec."""

    codec = codec.lower()
    if codec in {"mjpeg", "jpeg", "jpg"}:
        pipeline = (
            "appsrc name=src is-live=true format=time do-timestamp=true "
            "! jpegdec ! videoconvert ! video/x-raw,format=RGB "
            "! appsink name=sink sync=false drop=true max-buffers=1"
        )
        caps = "image/jpeg"
        return pipeline, caps

    if codec == "h264":
        pipeline = (
            "appsrc name=src is-live=true format=time do-timestamp=true "
            "! h264parse config-interval=-1 "
            "! avdec_h264 "
            "! videoconvert ! video/x-raw,format=RGB "
            "! appsink name=sink sync=false drop=true max-buffers=1"
        )
        caps = "video/x-h264,stream-format=byte-stream,alignment=au"
        return pipeline, caps

    if codec in {"h265", "hevc"}:
        pipeline = (
            "appsrc name=src is-live=true format=time do-timestamp=true "
            "! h265parse config-interval=-1 "
            "! avdec_h265 "
            "! videoconvert ! video/x-raw,format=RGB "
            "! appsink name=sink sync=false drop=true max-buffers=1"
        )
        caps = "video/x-h265,stream-format=byte-stream,alignment=au"
        return pipeline, caps

    raise DecoderUnavailable(f"GStreamer backend does not recognise codec '{codec}'")


def _extract_h264_nalus(data: bytes, *, avc_length_size: int = 0) -> Iterable[bytes]:
    """Yield raw H.264 NAL units from *data*.

    When *avc_length_size* is non-zero the payload is interpreted as AVC format
    (length-prefixed). Otherwise Annex B start codes are used.
    """

    if avc_length_size:
        total = len(data)
        offset = 0
        while offset + avc_length_size <= total:
            nal_size = int.from_bytes(data[offset : offset + avc_length_size], "big", signed=False)
            offset += avc_length_size
            if nal_size <= 0 or offset + nal_size > total:
                break
            yield data[offset : offset + nal_size]
            offset += nal_size
        return

    # Annex B parsing
    total = len(data)
    offset = 0
    while offset < total:
        next_start = data.find(_H264_START_CODE, offset)
        if next_start == -1:
            if offset == 0:
                return
            yield data[offset:total]
            break
        if next_start == offset:
            offset += len(_H264_START_CODE)
            continue
        yield data[offset:next_start]
        offset = next_start + len(_H264_START_CODE)
    else:
        if offset < total:
            yield data[offset:total]


class _H264Normalizer:
    """Normalise H.264 payloads to Annex B and reuse cached SPS/PPS."""

    def __init__(self) -> None:
        self._sps: List[bytes] = []
        self._pps: List[bytes] = []
        self._have_idr = False
        self._avc_length_size: Optional[int] = None

    def _detect_layout(self, payload: bytes) -> int:
        if payload.startswith(_H264_START_CODE) or payload.find(_H264_START_CODE, 1) != -1:
            return 0
        for length_size in (4, 3, 2, 1):
            if len(payload) <= length_size:
                continue
            nal_size = int.from_bytes(payload[:length_size], "big", signed=False)
            if 0 < nal_size <= len(payload) - length_size:
                return length_size
        return 0

    def feed(self, payload: bytes) -> Optional[bytes]:
        if not payload:
            return None

        if self._avc_length_size is None:
            self._avc_length_size = self._detect_layout(payload)

        nalus = list(_extract_h264_nalus(payload, avc_length_size=self._avc_length_size))
        if not nalus:
            return None

        out: List[bytes] = []
        produced = False

        for nal in nalus:
            if not nal:
                continue
            nal_type = nal[0] & 0x1F
            if nal_type == 7:  # SPS
                self._sps = [nal]
                continue
            if nal_type == 8:  # PPS
                self._pps = [nal]
                continue
            if nal_type == 5:  # IDR
                if not self._sps or not self._pps:
                    return None
                out.extend(self._sps)
                out.extend(self._pps)
                out.append(nal)
                self._have_idr = True
                produced = True
            else:
                if not self._have_idr:
                    continue
                out.append(nal)
                produced = True

        if not produced:
            return None

        return b"".join(_H264_START_CODE + nal for nal in out)


class _GStreamerDecoder(DecoderBackend):
    """GStreamer-based backend for MJPEG and frame-based codecs."""

    def __init__(self, format_name: str) -> None:  # pragma: no cover - optional dependency
        super().__init__(format_name)
        if _np is None:
            raise DecoderUnavailable("numpy is required for the GStreamer backend")

        try:
            import gi  # type: ignore

            gi.require_version("Gst", "1.0")
            from gi.repository import Gst, GLib  # type: ignore
        except (ImportError, ValueError) as exc:
            raise DecoderUnavailable("GStreamer (python-gi) is not available") from exc

        self._Gst = Gst
        self._GLib = GLib
        Gst.init(None)

        codec_name = _normalise_codec_name(format_name)
        pipeline_desc, caps_string = _select_gstreamer_pipeline(codec_name)

        try:
            self._pipeline = Gst.parse_launch(pipeline_desc)
        except GLib.Error as exc:
            raise DecoderUnavailable(f"Failed to create GStreamer pipeline: {exc}") from exc

        self._appsrc = self._pipeline.get_by_name("src")
        self._appsink = self._pipeline.get_by_name("sink")
        if self._appsrc is None or self._appsink is None:
            raise DecoderUnavailable("GStreamer pipeline is missing required elements")

        self._appsink.set_property("emit-signals", False)

        if caps_string:
            caps = Gst.Caps.from_string(caps_string)
            self._appsrc.set_property("caps", caps)

        self._lock = threading.Lock()
        self._timestamp = 0
        self._frame_duration = Gst.SECOND // 30  # default, adjusted dynamically when available
        self._timeout_ns = int(0.5 * Gst.SECOND)
        self._normalizer = _H264Normalizer() if codec_name == "h264" else None

        state_change = self._pipeline.set_state(Gst.State.PLAYING)
        if state_change == Gst.StateChangeReturn.FAILURE:
            self.close()
            raise DecoderUnavailable("Failed to start GStreamer pipeline")

    def _build_buffer(self, packet: bytes):
        buf = self._Gst.Buffer.new_allocate(None, len(packet), None)
        buf.fill(0, packet)
        buf.pts = self._timestamp
        buf.dts = self._timestamp
        buf.duration = self._frame_duration
        self._timestamp += self._frame_duration
        return buf

    def _pull_sample(self, timeout_ns: int):
        try:
            sample = self._appsink.emit("try-pull-sample", timeout_ns)
        except AttributeError:
            pull_try = getattr(self._appsink, "try_pull_sample", None)
            if pull_try is not None:
                return pull_try(timeout_ns)
            pull = getattr(self._appsink, "pull_sample", None)
            if pull is not None:
                return pull()
            sample = None
        return sample

    def decode_packet(self, packet: bytes) -> List[object]:  # pragma: no cover - gst optional
        with self._lock:
            if self._pipeline is None:
                return []

            if self._normalizer is not None:
                packet = self._normalizer.feed(packet)
                if packet is None:
                    return []

            buf = self._build_buffer(packet)
            flow_ret = self._appsrc.emit("push-buffer", buf)
            if flow_ret != self._Gst.FlowReturn.OK:
                LOG.debug("GStreamer push-buffer returned %s", flow_ret)
                return []

            sample = self._pull_sample(self._timeout_ns)
            if sample is None:
                return []

            try:
                caps = sample.get_caps()
                structure = caps.get_structure(0) if caps and caps.get_size() else None
                width = structure.get_value("width") if structure else None
                height = structure.get_value("height") if structure else None
                if not width or not height:
                    return []

                buffer = sample.get_buffer()
                success, map_info = buffer.map(self._Gst.MapFlags.READ)
                if not success:
                    return []
                try:
                    array = _np.frombuffer(map_info.data, dtype=_np.uint8)
                    try:
                        array = array.reshape((height, width, 3))
                    except ValueError:
                        return []
                    frame = array.copy()
                finally:
                    buffer.unmap(map_info)
            finally:
                sample = None

        return [frame]

    def flush(self) -> List[object]:  # pragma: no cover - gst optional
        frames: List[object] = []
        if self._appsink is None:
            return frames
        while True:
            sample = self._pull_sample(0)
            if sample is None:
                break
            try:
                caps = sample.get_caps()
                structure = caps.get_structure(0) if caps and caps.get_size() else None
                width = structure.get_value("width") if structure else None
                height = structure.get_value("height") if structure else None
                if not width or not height:
                    continue
                buffer = sample.get_buffer()
                success, map_info = buffer.map(self._Gst.MapFlags.READ)
                if not success:
                    continue
                try:
                    array = _np.frombuffer(map_info.data, dtype=_np.uint8)
                    try:
                        array = array.reshape((height, width, 3))
                    except ValueError:
                        continue
                    frames.append(array.copy())
                finally:
                    buffer.unmap(map_info)
            finally:
                sample = None
        return frames

    def close(self) -> None:  # pragma: no cover - gst optional
        with self._lock:
            if self._pipeline is not None:
                self._pipeline.set_state(self._Gst.State.NULL)
                self._pipeline = None
                self._appsrc = None
                self._appsink = None

    def __del__(self):  # pragma: no cover - best-effort cleanup
        try:
            self.close()
        except Exception:
            pass


class _PyAVDecoder(DecoderBackend):
    """PyAV/libav backend for software decoding."""

    def __init__(self, format_name: str) -> None:  # pragma: no cover - exercised conditionally
        super().__init__(format_name)
        try:
            import av  # type: ignore
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise DecoderUnavailable("PyAV is not installed") from exc

        self._av = av
        codec_name = _normalise_codec_name(format_name)
        try:
            self._codec = av.CodecContext.create(codec_name, "r")
        except av.AVError as exc:  # pragma: no cover - depends on installation
            raise DecoderUnavailable(f"PyAV cannot create codec '{codec_name}'") from exc
        self._normalizer = _H264Normalizer() if codec_name == "h264" else None

    def decode_packet(self, packet: bytes) -> List[object]:  # pragma: no cover - backend optional
        if self._normalizer is not None:
            packet = self._normalizer.feed(packet)
            if packet is None:
                return []
        av_packet = self._av.packet.Packet(packet)
        frames: List[object] = []
        for frame in self._codec.decode(av_packet):
            try:
                array = frame.to_ndarray(format="rgb24")
            except Exception as exc:  # pragma: no cover
                LOG.debug("PyAV failed to convert frame: %s", exc)
                continue
            frames.append(array)
        return frames

    def flush(self) -> List[object]:  # pragma: no cover - backend optional
        frames: List[object] = []
        try:
            remaining = self._codec.decode(None)
        except Exception:  # pragma: no cover
            remaining = []
        for frame in remaining:
            try:
                frames.append(frame.to_ndarray(format="rgb24"))
            except Exception:
                continue
        return frames


def _normalise_codec_name(format_name: str) -> str:
    lowered = format_name.lower()
    if "264" in lowered:
        return "h264"
    if "265" in lowered or "hevc" in lowered:
        return "hevc"
    return lowered


_BACKEND_REGISTRY = {
    "gstreamer": _GStreamerDecoder,
    "pyav": _PyAVDecoder,
}
_DEFAULT_ORDER = ["gstreamer", "pyav"]
DEFAULT_BACKEND_ORDER = tuple(_DEFAULT_ORDER)


def create_decoder_backend(format_name: str, preference: Optional[Iterable[str]] = None) -> DecoderBackend:
    """Return the first available backend for *format_name*.

    *preference* can be an iterable of backend names (``"pyav"``, ``"gstreamer"``).
    When omitted, the default discovery order is used.
    """

    names: List[str] = []
    if preference:
        for name in preference:
            if name == "auto":
                continue
            if name in _BACKEND_REGISTRY and name not in names:
                names.append(name)
            else:
                LOG.debug("Unknown decoder backend '%s' ignored", name)
    if not names:
        names = list(_DEFAULT_ORDER)

    errors: List[str] = []
    for name in names:
        backend_cls = _BACKEND_REGISTRY.get(name)
        if backend_cls is None:
            continue
        try:
            backend = backend_cls(format_name)
            try:
                setattr(backend, "backend_name", name)
            except Exception:  # pragma: no cover - defensive
                pass
            return backend
        except DecoderUnavailable as exc:
            errors.append(f"{name}: {exc}")
            LOG.debug("Decoder backend %s unavailable: %s", name, exc)
    raise DecoderUnavailable("; ".join(errors) if errors else "No decoder backend available")


__all__ = [
    "DecoderBackend",
    "DecoderUnavailable",
    "create_decoder_backend",
    "DEFAULT_BACKEND_ORDER",
]
