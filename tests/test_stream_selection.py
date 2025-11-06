import libusb_uvc as uvc

from libusb_uvc import (
    CodecPreference,
    FrameInfo,
    StreamFormat,
    StreamingInterface,
    VS_FORMAT_FRAME_BASED,
    resolve_stream_preference,
)


def _make_frame(width=1920, height=1080):
    return FrameInfo(
        frame_index=1,
        width=width,
        height=height,
        default_interval=333333,
        intervals_100ns=[333333],
        max_frame_size=6_000_000,
    )


def _make_stream_interface(description: str) -> StreamingInterface:
    frame = _make_frame()
    fmt = StreamFormat(
        description=description,
        format_index=1,
        subtype=VS_FORMAT_FRAME_BASED,
        guid=b"\x00" * 16,
        frames=[frame],
    )
    return StreamingInterface(interface_number=1, formats=[fmt], alt_settings=[])


def test_resolve_stream_preference_h264():
    interface = _make_stream_interface("H.264 High Profile")
    stream_format, frame = resolve_stream_preference(
        interface,
        1920,
        1080,
        codec=CodecPreference.H264,
    )
    assert stream_format.subtype == VS_FORMAT_FRAME_BASED
    assert "264" in stream_format.description.lower()
    assert frame.width == 1920 and frame.height == 1080


def test_resolve_stream_preference_auto_falls_back_to_frame_based():
    interface = _make_stream_interface("HEVC / H.265")
    stream_format, frame = resolve_stream_preference(
        interface,
        1920,
        1080,
        codec=CodecPreference.AUTO,
    )
    assert stream_format.subtype == VS_FORMAT_FRAME_BASED
    assert frame.width == 1920 and frame.height == 1080
