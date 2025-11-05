"""Decoders for compressed UVC video payloads.

This module provides a lightweight abstraction around video decoding backends
so that callers can consume H.264/H.265 (frame-based) payloads without being
coupled to a single library.  Backends are discovered lazily and can be
extended in the future (e.g. Media Foundation, VideoToolbox).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Iterable, List, Optional

import logging

LOG = logging.getLogger(__name__)


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


class _GStreamerDecoder(DecoderBackend):
    """GStreamer-based backend (placeholder until fully implemented)."""

    def __init__(self, format_name: str) -> None:  # pragma: no cover - not exercised yet
        super().__init__(format_name)
        try:
            import gi  # type: ignore

            gi.require_version("Gst", "1.0")
            from gi.repository import Gst  # noqa: F401  # type: ignore
        except (ImportError, ValueError) as exc:
            raise DecoderUnavailable("GStreamer (python-gi) is not available") from exc

        raise DecoderUnavailable("GStreamer backend not implemented yet")

    def decode_packet(self, packet: bytes) -> List[object]:  # pragma: no cover
        raise DecoderUnavailable("GStreamer backend not implemented yet")


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

    def decode_packet(self, packet: bytes) -> List[object]:  # pragma: no cover - backend optional
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


_BACKENDS = [_GStreamerDecoder, _PyAVDecoder]


def create_decoder_backend(format_name: str) -> DecoderBackend:
    """Return the first available backend for *format_name*."""

    errors: List[str] = []
    for backend_cls in _BACKENDS:
        try:
            return backend_cls(format_name)
        except DecoderUnavailable as exc:
            errors.append(str(exc))
            LOG.debug("Decoder backend %s unavailable: %s", backend_cls.__name__, exc)
    raise DecoderUnavailable(
        "; ".join(errors) if errors else "No decoder backend available"
    )


__all__ = [
    "DecoderBackend",
    "DecoderUnavailable",
    "create_decoder_backend",
]
