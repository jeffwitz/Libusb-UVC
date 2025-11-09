from libusb_uvc.decoders import _H264Normalizer, _H265Normalizer, _extract_h264_nalus


def test_extract_h264_nalus_annexb():
    data = b"\x00\x00\x00\x01\x67\x42\x00\x1f\x00\x00\x00\x01\x68\xce\x06\xe2"
    nalus = list(_extract_h264_nalus(data))
    assert len(nalus) == 2
    assert nalus[0][0] == 0x67
    assert nalus[1][0] == 0x68


def test_extract_h264_nalus_avc():
    data = b"\x00\x00\x00\x04\x67\x42\x00\x1f\x00\x00\x00\x04\x68\xce\x06\xe2"
    nalus = list(_extract_h264_nalus(data, avc_length_size=4))
    assert len(nalus) == 2
    assert nalus[0][0] == 0x67
    assert nalus[1][0] == 0x68


def test_normalizer_waits_for_config():
    normalizer = _H264Normalizer()
    # P-slice before config -> ignored
    assert normalizer.feed(b"\x00\x00\x00\x01\x41\x9a") is None
    # Provide SPS/PPS
    assert normalizer.feed(b"\x00\x00\x00\x01\x67\x42\x00\x1f\x00\x00\x00\x01\x68\xce\x06\xe2") is None
    # First IDR should inject config
    output = normalizer.feed(b"\x00\x00\x00\x01\x65\x88")
    assert output is not None
    assert output.count(b"\x00\x00\x00\x01") == 3  # SPS + PPS + IDR
    # Subsequent P-slice should now pass through
    out2 = normalizer.feed(b"\x00\x00\x00\x01\x41\x9a")
    assert out2 is not None
    assert out2.startswith(b"\x00\x00\x00\x01\x41")


def _make_hevc_nal(nal_type: int, payload: bytes = b"\x01\x02") -> bytes:
    header0 = (nal_type << 1) & 0x7E
    header1 = 1  # nuh_temporal_id_plus1 = 1
    return bytes([header0, header1]) + payload


def _make_annexb_sequence(nal_types):
    chunks = []
    for ntype in nal_types:
        chunks.append(b"\x00\x00\x00\x01" + _make_hevc_nal(ntype))
    return b"".join(chunks)


def test_hevc_normalizer_injects_parameter_sets():
    normalizer = _H265Normalizer()
    # Trailing frame before config should be dropped
    assert normalizer.feed(_make_annexb_sequence([1])) is None
    # Provide VPS/SPS/PPS
    assert normalizer.feed(_make_annexb_sequence([32])) is None
    assert normalizer.feed(_make_annexb_sequence([33])) is None
    assert normalizer.feed(_make_annexb_sequence([34])) is None
    # IDR should inject VPS/SPS/PPS
    output = normalizer.feed(_make_annexb_sequence([19]))
    assert output is not None
    assert output.count(b"\x00\x00\x00\x01") == 4
    # Subsequent trailing slice should pass through
    trailing = normalizer.feed(_make_annexb_sequence([1]))
    assert trailing is not None
    assert trailing.startswith(b"\x00\x00\x00\x01")
