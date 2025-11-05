import libusb_uvc as uvc

from libusb_uvc import (
    DecoderPreference,
    FrameInfo,
    FrameStream,
    StreamFormat,
    VS_FORMAT_FRAME_BASED,
    VS_FORMAT_MJPEG,
)
from libusb_uvc.decoders import _select_gstreamer_pipeline


class DummyCamera:
    """Minimal stub satisfying FrameStream initialisation requirements."""

    def __init__(self) -> None:
        self.interface_number = 0


def _make_frame_info() -> FrameInfo:
    return FrameInfo(
        frame_index=1,
        width=640,
        height=480,
        default_interval=333333,
        intervals_100ns=[333333],
        max_frame_size=614400,
    )


def _make_stream_format(subtype: int, description: str) -> StreamFormat:
    return StreamFormat(
        description=description,
        format_index=1,
        subtype=subtype,
        guid=b"\x00" * 16,
    )


def test_normalise_decoder_preference_variants():
    normal = uvc._normalise_decoder_preference

    assert normal(None) == []
    assert normal(DecoderPreference.AUTO) == []
    assert normal("auto") == []

    assert normal(DecoderPreference.NONE) is None
    assert normal(" none  ") is None

    assert normal(DecoderPreference.PYAV) == ["pyav"]
    assert normal("pyav,gstreamer") == ["pyav", "gstreamer"]
    assert normal(["PyAV", "pyav", "gstreamer"]) == ["pyav", "gstreamer"]


def test_frame_stream_mjpeg_auto_skips_decoder(monkeypatch):
    called = False

    def fake_backend(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("Decoder should not be created for MJPEG in auto mode")

    monkeypatch.setattr(uvc, "create_decoder_backend", fake_backend)

    fmt = _make_stream_format(VS_FORMAT_MJPEG, "MJPG")
    frame = _make_frame_info()

    stream = FrameStream(
        camera=DummyCamera(),
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
        decoder_preference=DecoderPreference.AUTO,
    )

    assert stream._decoder is None
    assert called is False


def test_frame_stream_mjpeg_with_explicit_decoder(monkeypatch):
    captured = {}

    class DummyBackend:
        backend_name = "pyav"

        def decode_packet(self, _payload):
            return []

        def flush(self):
            return []

    def fake_backend(description, preference=None):
        captured["description"] = description
        captured["preference"] = list(preference) if preference else None
        return DummyBackend()

    monkeypatch.setattr(uvc, "create_decoder_backend", fake_backend)

    fmt = _make_stream_format(VS_FORMAT_MJPEG, "MJPEG")
    frame = _make_frame_info()

    stream = FrameStream(
        camera=DummyCamera(),
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
        decoder_preference=DecoderPreference.PYAV,
    )

    assert captured["preference"] == ["pyav"]
    assert captured["description"].lower().startswith("mjpeg")
    assert stream._decoder is not None
    assert stream._decoder_backend_name == "pyav"


def test_frame_stream_installs_requested_decoder(monkeypatch):
    captured = {}

    class DummyBackend:
        backend_name = "pyav"

        def decode_packet(self, _payload):
            return []

        def flush(self):
            return []

    def fake_backend(description, preference=None):
        captured["description"] = description
        captured["preference"] = list(preference) if preference else None
        return DummyBackend()

    monkeypatch.setattr(uvc, "create_decoder_backend", fake_backend)

    fmt = _make_stream_format(VS_FORMAT_FRAME_BASED, "H.264")
    frame = _make_frame_info()

    stream = FrameStream(
        camera=DummyCamera(),
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
        decoder_preference=DecoderPreference.PYAV,
    )

    assert captured["preference"] == ["pyav"]
    assert captured["description"] == "H.264"
    assert stream._decoder is not None
    assert stream._decoder_backend_name == "pyav"


def test_select_gstreamer_pipeline_configs():
    pipeline, caps = _select_gstreamer_pipeline("mjpeg")
    assert "jpegdec" in pipeline
    assert caps == "image/jpeg"

    pipeline, caps = _select_gstreamer_pipeline("h264")
    assert "h264parse" in pipeline and "avdec_h264" in pipeline
    assert "video/x-h264" in caps

    pipeline, caps = _select_gstreamer_pipeline("hevc")
    assert "h265parse" in pipeline and "avdec_h265" in pipeline
    assert "video/x-h265" in caps
