"""v0.7.1: VBR bitrate parsing regression.

Real-device probe of the user's DS-8632N-I8 (192.168.10.9) found every
VBR channel reporting ``video_bitrate_kbps = None``.

Root cause: ``_parse_streaming_detail`` only looked for
``videoAverageBitrate`` / ``constantBitRate``. Those are CBR-mode fields.
A VBR stream carries neither — the device reports
``<videoQualityControlType>VBR</videoQualityControlType>`` with
``<vbrUpperCap>16384</vbrUpperCap>`` instead. So the bitrate sensor showed
"unknown" on every VBR channel while working fine on CBR ones.

The fix adds ``vbrUpperCap`` as a final fallback. It is the configured
ceiling (the "码率上限" in the device web UI), not the instantaneous
rate, but it is the closest meaningful configured value and beats None.
"""

from __future__ import annotations

from xml.etree import ElementTree as ET

from custom_components.hikvision_isapi_performance.coordinator import (
    _parse_streaming_detail,
)
from custom_components.hikvision_isapi_performance.isapi_client import (
    _strip_xmlns,
)


def _parse_root(xml_str: str) -> ET.Element:
    return ET.fromstring(_strip_xmlns(xml_str))


# Real DS-8632N-I8 channel 101, verbatim from the 2026-09-26 probe
# (192.168.10.9__ISAPI_Streaming_channels). VBR, 4K, no constantBitRate.
VBR_XML = """<StreamingChannelList xmlns="http://www.isapi.org/ver20/XMLSchema" version="2.0">
<StreamingChannel>
<id>101</id>
<channelName>101</channelName>
<enabled>true</enabled>
<Video>
<enabled>true</enabled>
<dynVideoInputChannelID>1</dynVideoInputChannelID>
<videoCodecType>H.264</videoCodecType>
<videoResolutionWidth>3840</videoResolutionWidth>
<videoResolutionHeight>2160</videoResolutionHeight>
<videoQualityControlType>VBR</videoQualityControlType>
<fixedQuality>90</fixedQuality>
<vbrUpperCap>16384</vbrUpperCap>
<vbrLowerCap>32</vbrLowerCap>
<maxFrameRate>2500</maxFrameRate>
<GovLength>50</GovLength>
</Video>
<Audio>
<enabled>true</enabled>
<audioInputChannelID>1</audioInputChannelID>
<audioCompressionType>G.711alaw</audioCompressionType>
</Audio>
</StreamingChannel>
</StreamingChannelList>"""

# Real DS-FB2127 channel 1, CBR — must keep returning 2048, not regress.
CBR_XML = """<StreamingChannelList xmlns="http://www.hikvision.com/ver20/XMLSchema" version="2.0">
<StreamingChannel>
<id>1</id>
<channelName>摄像机06</channelName>
<enabled>true</enabled>
<Video>
<enabled>true</enabled>
<videoInputChannelID>1</videoInputChannelID>
<videoCodecType>H.264</videoCodecType>
<videoResolutionWidth>1920</videoResolutionWidth>
<videoResolutionHeight>1080</videoResolutionHeight>
<constantBitRate>2048</constantBitRate>
<maxFrameRate>3000</maxFrameRate>
</Video>
<Audio>
<audioCompressionType>G.711alaw</audioCompressionType>
</Audio>
</StreamingChannel>
</StreamingChannelList>"""


def test_vbr_stream_reports_upper_cap_as_bitrate():
    """VBR channel must surface vbrUpperCap rather than None."""
    detail = _parse_streaming_detail(_parse_root(VBR_XML))
    assert detail["video_bitrate_kbps"] == 16384
    assert detail["video_codec"] == "H.264"
    assert detail["video_resolution"] == "3840x2160"
    assert detail["video_frame_rate"] == 25.0
    assert detail["audio_codec"] == "G.711alaw"


def test_vbr_bitrate_propagates_to_per_channel_list():
    """The channels[] list carries the same fallback value."""
    detail = _parse_streaming_detail(_parse_root(VBR_XML))
    channels = detail.get("channels", [])
    assert channels, "per-channel list should be populated"
    assert channels[0]["video_bitrate_kbps"] == 16384


def test_cbr_stream_still_uses_constant_bitrate():
    """CBR must keep resolving from constantBitRate — no regression."""
    detail = _parse_streaming_detail(_parse_root(CBR_XML))
    assert detail["video_bitrate_kbps"] == 2048


def test_cbr_takes_precedence_over_vbr_upper_cap():
    """If both are present, constantBitRate wins (it is the real value)."""
    xml = """<StreamingChannelList>
<StreamingChannel>
<id>1</id>
<Video>
<videoCodecType>H.265</videoCodecType>
<constantBitRate>4096</constantBitRate>
<vbrUpperCap>8192</vbrUpperCap>
</Video>
</StreamingChannel>
</StreamingChannelList>"""
    detail = _parse_streaming_detail(_parse_root(xml))
    assert detail["video_bitrate_kbps"] == 4096


def test_stream_with_no_bitrate_fields_at_all():
    """A stream with none of the three fields yields None, not a crash."""
    xml = """<StreamingChannelList>
<StreamingChannel>
<id>1</id>
<Video>
<videoCodecType>H.264</videoCodecType>
<videoResolutionWidth>1280</videoResolutionWidth>
<videoResolutionHeight>720</videoResolutionHeight>
</Video>
</StreamingChannel>
</StreamingChannelList>"""
    detail = _parse_streaming_detail(_parse_root(xml))
    assert detail["video_bitrate_kbps"] is None
    assert detail["video_resolution"] == "1280x720"
