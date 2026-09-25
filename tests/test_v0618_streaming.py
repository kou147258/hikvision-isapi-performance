"""Tests for v0.6.18 — streaming detail, encoding sensors, storage re-add.

User feedback after v0.6.17: storage and bitrate sensors were
deleted because the user's V4 NVR returned 4 Invalid Operation on
those endpoints. The user pointed out that storage / encoding /
bitrate information IS valuable — the sensors should be re-added
so users with working firmware see real values, while users on
the V4 NVR simply see "unknown" for those entries.

v0.6.18 also addresses a previously-uncovered part of the
``/Streaming/channels`` XML on the user's V5 IPC
(``DS-FB2127``): video codec, resolution, frame rate, audio
codec, and the per-channel video bitrate. These were parsed
but never exposed as entities.

The user's V5 IPC streaming XML excerpt::

    <StreamingChannel>
      <id>1</id>
      <Video>
        <videoCodecType>H.264</videoCodecType>
        <videoResolutionWidth>1920</videoResolutionWidth>
        <videoResolutionHeight>1080</videoResolutionHeight>
        <maxFrameRate>1600</maxFrameRate>
        <constantBitRate>2048</constantBitRate>
      </Video>
      <Audio>
        <audioCompressionType>G.711alaw</audioCompressionType>
      </Audio>
    </StreamingChannel>

Real values to expose:

- ``video_codec``         → "H.264"
- ``video_resolution``    → "1920x1080"
- ``video_frame_rate``    → 16.0 (Hikvision reports in hundredths)
- ``video_bitrate_kbps``  → 2048
- ``audio_codec``         → "G.711alaw"

Also re-adds the 4 storage sensors (``storage_total_gb`` /
``storage_used_gb`` / ``storage_free_gb`` / ``storage_usage_percent``)
in GB; user's V4 NVR firmware still returns "unknown" but
V5 NVRs populate correctly.
"""

from __future__ import annotations

import json
from pathlib import Path
from xml.etree import ElementTree as ET

from custom_components.hikvision_isapi_performance.coordinator import (
    _parse_streaming_detail,
)
from custom_components.hikvision_isapi_performance.isapi_client import (
    _strip_xmlns,
)


def _parse_root(xml_str: str) -> ET.Element:
    return ET.fromstring(_strip_xmlns(xml_str))


# ---- 1. _parse_streaming_detail on the user's V5 IPC real XML ----


def test_parse_streaming_detail_v5_ipc_primary_stream():
    """Real ``DS-FB2127`` channel 1 primary stream.

    maxFrameRate is 1600 (hundredths-of-fps) so the result is 16.0 fps.
    """
    xml = """<StreamingChannelList xmlns="http://www.hikvision.com/ver20/XMLSchema" version="2.0">
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
<maxFrameRate>1600</maxFrameRate>
</Video>
<Audio>
<enabled>true</enabled>
<audioInputChannelID>1</audioInputChannelID>
<audioCompressionType>G.711alaw</audioCompressionType>
</Audio>
</StreamingChannel>
</StreamingChannelList>"""
    detail = _parse_streaming_detail(_parse_root(xml))
    assert detail["video_codec"] == "H.264"
    assert detail["video_resolution"] == "1920x1080"
    assert detail["video_resolution_width"] == 1920
    assert detail["video_resolution_height"] == 1080
    assert detail["video_frame_rate"] == 16.0
    assert detail["video_bitrate_kbps"] == 2048
    assert detail["audio_codec"] == "G.711alaw"
    assert detail["video_input_channel_id"] == 1
    assert detail["first_channel_id"] == "1"


def test_parse_streaming_detail_v5_ipc_sub_stream_channel_2():
    """Real sub-stream (704x480, 30 fps) on the same IPC."""
    xml = """<StreamingChannelList xmlns="http://www.hikvision.com/ver20/XMLSchema" version="2.0">
<StreamingChannel>
<id>2</id>
<Video>
<videoCodecType>H.264</videoCodecType>
<videoResolutionWidth>704</videoResolutionWidth>
<videoResolutionHeight>480</videoResolutionHeight>
<constantBitRate>1024</constantBitRate>
<maxFrameRate>3000</maxFrameRate>
</Video>
<Audio>
<audioCompressionType>G.711alaw</audioCompressionType>
</Audio>
</StreamingChannel>
</StreamingChannelList>"""
    detail = _parse_streaming_detail(_parse_root(xml))
    assert detail["video_resolution"] == "704x480"
    assert detail["video_frame_rate"] == 30.0
    assert detail["video_bitrate_kbps"] == 1024
    assert detail["first_channel_id"] == "2"


def test_parse_streaming_detail_handles_h265():
    """v0.6.18 supports H.265 too — newer V5.x firmwares default
    to H.265 for low bitrate."""
    xml = """<StreamingChannelList>
<StreamingChannel>
<id>1</id>
<Video>
<videoCodecType>H.265</videoCodecType>
<videoResolutionWidth>2560</videoResolutionWidth>
<videoResolutionHeight>1440</videoResolutionHeight>
<maxFrameRate>2500</maxFrameRate>
<constantBitRate>1024</constantBitRate>
</Video>
</StreamingChannel>
</StreamingChannelList>"""
    detail = _parse_streaming_detail(_parse_root(xml))
    assert detail["video_codec"] == "H.265"
    assert detail["video_resolution"] == "2560x1440"
    assert detail["video_frame_rate"] == 25.0


def test_parse_streaming_detail_handles_empty_response():
    """V4 NVR returns ``<ResponseStatus>`` instead of an actual
    streaming channel list; coordinator hands us ``None`` to
    this parser and we return the empty default shape so sensors
    cleanly show "unknown" rather than crashing."""
    detail = _parse_streaming_detail(None)
    assert detail["video_codec"] is None
    assert detail["video_resolution"] is None
    assert detail["video_frame_rate"] is None
    assert detail["video_bitrate_kbps"] is None
    assert detail["first_channel_id"] is None
    assert detail["channels"] == []


def test_parse_streaming_detail_missing_optional_fields():
    """Some IPC firmwares omit audio, frame rate, or video
    bitrate — we return None for those fields rather than
    raising."""
    xml = """<StreamingChannelList>
<StreamingChannel>
<id>1</id>
<Video>
<videoCodecType>H.264</videoCodecType>
<videoResolutionWidth>1920</videoResolutionWidth>
<videoResolutionHeight>1080</videoResolutionHeight>
</Video>
</StreamingChannel>
</StreamingChannelList>"""
    detail = _parse_streaming_detail(_parse_root(xml))
    assert detail["video_codec"] == "H.264"
    assert detail["video_resolution"] == "1920x1080"
    # Missing <maxFrameRate>, <constantBitRate>, audio → None.
    assert detail["video_frame_rate"] is None
    assert detail["video_bitrate_kbps"] is None
    assert detail["audio_codec"] is None


def test_parse_streaming_detail_multi_channel_first_only_top_level():
    """When multiple <StreamingChannel> are present (e.g. main +
    sub-stream), the *top-level* fields describe the FIRST channel
    while ``channels`` lists all entries. This matches what a
    single-channel IPC user sees — they'll only ever have one
    channel; the multi-channel case is rare."""
    xml = """<StreamingChannelList>
<StreamingChannel>
<id>1</id>
<Video>
<videoCodecType>H.264</videoCodecType>
<videoResolutionWidth>1920</videoResolutionWidth>
<videoResolutionHeight>1080</videoResolutionHeight>
<constantBitRate>2048</constantBitRate>
<maxFrameRate>1600</maxFrameRate>
</Video>
</StreamingChannel>
<StreamingChannel>
<id>2</id>
<Video>
<videoCodecType>H.264</videoCodecType>
<videoResolutionWidth>704</videoResolutionWidth>
<videoResolutionHeight>480</videoResolutionHeight>
<constantBitRate>1024</constantBitRate>
<maxFrameRate>3000</maxFrameRate>
</Video>
</StreamingChannel>
</StreamingChannelList>"""
    detail = _parse_streaming_detail(_parse_root(xml))
    assert detail["first_channel_id"] == "1"
    assert detail["video_resolution"] == "1920x1080"
    assert detail["video_bitrate_kbps"] == 2048
    assert len(detail["channels"]) == 2
    assert detail["channels"][0]["video_resolution"] == "1920x1080"
    assert detail["channels"][1]["video_resolution"] == "704x480"


# ---- 2. sensor entity keys include the v0.6.18 additions ----


def _sensor_keys() -> list[str]:
    src = Path(
        r"C:\Users\43457\Desktop\hikvision-isapi"
        r"\custom_components\hikvision_isapi_performance\sensor.py"
    ).read_text(encoding="utf-8-sig")
    import re
    return re.findall(r'^\s+key="([^"]+)"', src, flags=re.MULTILINE)


def test_v0618_storage_sensors_re_added():
    """The 4 storage sensors were re-added in v0.6.18 with ``_gb``
    suffix on the size keys (HA's preferred unit is GB for HDD)."""
    keys = set(_sensor_keys())
    assert "storage_total_gb" in keys
    assert "storage_used_gb" in keys
    assert "storage_free_gb" in keys
    assert "storage_usage_percent" in keys


def test_v0618_streaming_sensors_added():
    """New codec / resolution / framerate / bitrate / audio sensors."""
    keys = set(_sensor_keys())
    for new in (
        "channel_1_video_codec",
        "channel_1_video_resolution",
        "channel_1_video_frame_rate",
        "channel_1_video_bitrate",
        "channel_1_audio_codec",
    ):
        assert new in keys, (
            f"v0.6.18 added a new {new} sensor; this test pins "
            "it so future regressions don't silently drop it."
        )


def test_v0618_storage_uses_gigabytes_for_size_sensors():
    """The 3 size storage sensors must use UnitOfInformation.GIGABYTES
    (HA's preferred HDD unit), not MEGABYTES."""
    src = Path(
        r"C:\Users\43457\Desktop\hikvision-isapi"
        r"\custom_components\hikvision_isapi_performance\sensor.py"
    ).read_text(encoding="utf-8-sig")
    for key in ("storage_total_gb", "storage_used_gb", "storage_free_gb"):
        section = src.split(f'key="{key}"', 1)[1].split("),", 1)[0]
        assert "UnitOfInformation.GIGABYTES" in section, (
            f"{key} must use GIGABYTES unit."
        )


# ---- 3. manifest version ----


def test_v0618_manifest_version_bumped():
    manifest = json.loads(Path(
        r"C:\Users\43457\Desktop\hikvision-isapi"
        r"\custom_components\hikvision_isapi_performance\manifest.json"
    ).read_text(encoding="utf-8"))
    assert manifest["version"] == "0.6.18"
