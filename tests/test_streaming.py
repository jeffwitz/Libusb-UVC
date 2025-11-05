"""Unit tests targeting the streaming pipeline with the emulator."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from libusb_uvc import CodecPreference, UVCCamera, UVCError, StreamFormat, FrameInfo

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


def test_read_frame_without_configure_raises(camera: UVCCamera):
    camera._format = None  # type: ignore[attr-defined]
    camera._frame = None  # type: ignore[attr-defined]
    camera._endpoint_address = None  # type: ignore[attr-defined]
    with pytest.raises(UVCError):
        camera.read_frame()
