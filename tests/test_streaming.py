"""Unit tests targeting the streaming pipeline with the emulator."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from libusb_uvc import CodecPreference, UVCCamera, UVCError, StreamFormat, FrameInfo
from libusb_uvc.core import FrameStream, FrameAssemblyResult, _strip_mjpeg_app_markers
from libusb_uvc.decoders import RecorderBackend
from libusb_uvc.core import FrameAssemblyResult, FrameStream

from .mocks import MockUsbDevice, StreamingInterfaceAdapter
from .uvc_emulator import UvcEmulatorLogic

PROFILE_PATH = Path(__file__).parent / "data" / "sample_camera_profile.json"


def sha1(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


@pytest.fixture()
def emulator() -> UvcEmulatorLogic:
    return UvcEmulatorLogic(PROFILE_PATH)


@pytest.fixture()
def mock_device(emulator: UvcEmulatorLogic) -> MockUsbDevice:
    return MockUsbDevice(emulator)


@pytest.fixture()
def camera(emulator: UvcEmulatorLogic, mock_device: MockUsbDevice) -> UVCCamera:
    mock_interface = mock_device._configurations[0]._interfaces[0]  # type: ignore[attr-defined]
    format_defs = emulator._formats  # type: ignore[attr-defined]
    formats = []
    for fmt_def in format_defs:
        fmt = StreamFormat(
            description=fmt_def.subtype.upper(),
            format_index=fmt_def.format_index,
            subtype=fmt_def.uvc_subtype,
            guid=b"EMUL" + b"\x00" * 12,
        )
        for frame_def in fmt_def.frames:
            intervals = list(frame_def.intervals) or [fmt_def.default_interval]
            frame = FrameInfo(
                frame_index=frame_def.frame_index,
                width=frame_def.width,
                height=frame_def.height,
                default_interval=intervals[0],
                intervals_100ns=intervals,
                max_frame_size=emulator._max_frame_size,  # type: ignore[attr-defined]
            )
            fmt.frames.append(frame)
        formats.append(fmt)

    adapter = StreamingInterfaceAdapter(mock_interface, formats)
    cam = UVCCamera(mock_device, adapter)  # type: ignore[arg-type]
    return cam


def test_configure_stream_sets_endpoint_and_payload(camera: UVCCamera):
    fmt, frame = camera.interface.formats[0], camera.interface.formats[0].frames[0]
    info = camera.configure_stream(fmt, frame)
    assert camera.endpoint_address is not None
    assert camera.max_payload_size and camera.max_payload_size > 0
    assert info["selected_alt"] == 0


def test_read_frame_matches_emulator_payload(camera: UVCCamera):
    fmt, frame = camera.interface.formats[0], camera.interface.formats[0].frames[0]
    camera.configure_stream(fmt, frame)
    captured = camera.read_frame()
    expected = Path("tests/data/test_video.mjpeg").read_bytes()
    assert captured.payload.startswith(b"\xff\xd8")
    assert sha1(captured.payload) == sha1(expected)
    stats = camera.get_stream_stats()
    assert stats.frames_completed >= 1
    assert stats.bytes_delivered >= len(captured.payload)


def test_read_frame_without_configure_raises(camera: UVCCamera):
    camera._format = None  # type: ignore[attr-defined]
    camera._frame = None  # type: ignore[attr-defined]
    camera._endpoint_address = None  # type: ignore[attr-defined]
    with pytest.raises(UVCError):
        camera.read_frame()


def test_strip_mjpeg_app_markers():
    payload = (
        b"\xff\xd8"  # SOI
        b"\xff\xe0\x00\x06ABC"  # APP0 segment
        b"\xff\xdb\x00\x02\x00"  # DQT (kept)
        b"\xff\xda\x00\x02\x00"  # SOS
        b"\xff\xd9"  # EOI
    )
    stripped = _strip_mjpeg_app_markers(payload)
    assert stripped.startswith(b"\xff\xd8")
    assert b"ABC" not in stripped  # APP segment removed
    assert stripped.endswith(b"\xff\xd9")


def test_frame_stream_records_mjpeg_payload(camera: UVCCamera):
    fmt = camera.interface.formats[0]
    frame = fmt.frames[0]

    class DummyRecorder(RecorderBackend):
        def __init__(self) -> None:
            self.payloads = []

        def submit(self, payload: bytes, *, fid: int, pts: Optional[int]) -> None:
            self.payloads.append((payload, fid, pts))

        def close(self) -> None:
            pass

    stream = FrameStream(
        camera=camera,
        stream_format=fmt,
        frame=frame,
        frame_rate=15.0,
        strict_fps=False,
        queue_size=1,
        skip_initial=0,
        transfers=1,
        packets_per_transfer=1,
        timeout_ms=1000,
        duration=None,
        decoder_preference=None,
        record_path="dummy.avi",
    )
    recorder = DummyRecorder()
    stream._recorder = recorder  # type: ignore[attr-defined]

    payload = bytearray(
        b"\xff\xd8"
        b"\xff\xe1\x00\x06XYZ"
        b"\xff\xda\x00\x02\x00\x11\x22"
        b"\xff\xd9"
    )
    result = FrameAssemblyResult(
        payload=payload,
        fid=5,
        pts=123,
        reason="eof",
        error=False,
        duration=0.01,
    )
    stream._handle_frame_result(result)

    assert recorder.payloads, "Recorder did not receive any payload"
    recorded, fid, pts = recorder.payloads[0]
    assert fid == 5
    assert pts == 123
    assert recorded.startswith(b"\xff\xd8")
    assert recorded.endswith(b"\xff\xd9")
    assert b"XYZ" not in recorded  # APP segment stripped


def test_frame_stream_reports_stats(camera: UVCCamera):
    fmt, frame = camera.interface.formats[0], camera.interface.formats[0].frames[0]
    stream = FrameStream(
        camera=camera,
        stream_format=fmt,
        frame=frame,
        frame_rate=None,
        strict_fps=False,
        queue_size=1,
        skip_initial=0,
        transfers=1,
        packets_per_transfer=1,
        timeout_ms=1000,
        duration=None,
        decoder_preference=CodecPreference.AUTO,
    )
    result = FrameAssemblyResult(
        payload=bytearray(b"\x00" * 128),
        fid=1,
        pts=None,
        reason="test",
        error=False,
        duration=0.01,
    )
    stream._handle_frame_result(result)
    stats = stream.stats
    assert stats.frames_completed == 1
    assert stats.bytes_delivered == 128
    assert stats.last_frame_duration_s == 0.01
