"""v0.7.5: per-stream sensors named after their camera, main + sub stream.

User request: each stream's codec / resolution / frame rate / bitrate must
be attributable to a named camera and tier, e.g. "摄像机12 主码流 视频编码"
and "摄像机12 子码流 视频编码", instead of the generic "通道 1 视频编码".

Facts established by probing the live fleet (not assumed):

* Neither device exposes a stream-tier field. There is no ``streamType``
  or similar element in ``/ISAPI/Streaming/channels`` — the tier can only
  be derived from the stream id.
* The two id conventions differ:
    - NVR  DS-8632N-I8: ids ``101``/``102``/``104`` with
      ``Video/dynVideoInputChannelID`` = 1 → tier = id - channel*100
    - IPC  DS-FB2127:   ids ``1``/``2``/``3`` with
      ``Video/videoInputChannelID`` = 1 → tier = id
* ``_channel_summary`` only read ``videoInputChannelID``, so on NVRs the
  owning channel number was ``None`` and no tier could be derived.

Design decisions pinned by these tests:

* Main stream keeps the existing entity key ``channel_{id}_<field>`` so
  upgrading does not orphan entities already in the user's registry; the
  sub stream gets ``channel_{id}_sub_<field>``.
* ``translation_key`` must be ``None`` on these dynamic entities. In real
  HA a translation_key overrides ``name``, and since every channel shares
  one key all channels would render the identical label — defeating the
  whole point. The literal ``name`` must be authoritative.
"""

from __future__ import annotations

from typing import Any
from xml.etree import ElementTree as ET

import pytest

import tests.conftest  # noqa: F401  installs homeassistant stubs

from custom_components.hikvision_isapi_performance.const import DOMAIN
from custom_components.hikvision_isapi_performance.coordinator import (
    HikvisionISAPIData,
    _parse_streaming_detail,
)
from custom_components.hikvision_isapi_performance.isapi_client import _strip_xmlns
from custom_components.hikvision_isapi_performance import sensor as sensor_mod


# ── real captured XML ─────────────────────────────────────────────────

# DS-8632N-I8 (192.168.10.9), trimmed to channels 1-2, verbatim field names.
NVR_STREAMING_XML = """<StreamingChannelList version="2.0"
 xmlns="http://www.isapi.org/ver20/XMLSchema">
<StreamingChannel>
<id>101</id><channelName>101</channelName><enabled>true</enabled>
<Video>
<dynVideoInputChannelID>1</dynVideoInputChannelID>
<videoCodecType>H.264</videoCodecType>
<videoResolutionWidth>3840</videoResolutionWidth>
<videoResolutionHeight>2160</videoResolutionHeight>
<videoQualityControlType>VBR</videoQualityControlType>
<vbrUpperCap>16384</vbrUpperCap>
<maxFrameRate>2500</maxFrameRate>
</Video>
<Audio><audioCompressionType>G.711alaw</audioCompressionType></Audio>
</StreamingChannel>
<StreamingChannel>
<id>102</id><channelName>102</channelName><enabled>true</enabled>
<Video>
<dynVideoInputChannelID>1</dynVideoInputChannelID>
<videoCodecType>H.264</videoCodecType>
<videoResolutionWidth>704</videoResolutionWidth>
<videoResolutionHeight>576</videoResolutionHeight>
<vbrUpperCap>512</vbrUpperCap>
<maxFrameRate>2500</maxFrameRate>
</Video>
<Audio><audioCompressionType>G.711alaw</audioCompressionType></Audio>
</StreamingChannel>
<StreamingChannel>
<id>201</id><channelName>201</channelName><enabled>true</enabled>
<Video>
<dynVideoInputChannelID>2</dynVideoInputChannelID>
<videoCodecType>H.265</videoCodecType>
<videoResolutionWidth>1920</videoResolutionWidth>
<videoResolutionHeight>1080</videoResolutionHeight>
<vbrUpperCap>4096</vbrUpperCap>
<maxFrameRate>2500</maxFrameRate>
</Video>
<Audio><audioCompressionType>G.711alaw</audioCompressionType></Audio>
</StreamingChannel>
<StreamingChannel>
<id>202</id><channelName>202</channelName><enabled>true</enabled>
<Video>
<dynVideoInputChannelID>2</dynVideoInputChannelID>
<videoCodecType>H.264</videoCodecType>
<videoResolutionWidth>640</videoResolutionWidth>
<videoResolutionHeight>360</videoResolutionHeight>
<vbrUpperCap>256</vbrUpperCap>
<maxFrameRate>2500</maxFrameRate>
</Video>
</StreamingChannel>
</StreamingChannelList>"""

# DS-FB2127 (10.18.176.18): IPC convention, ids 1/2/3, videoInputChannelID=1.
IPC_STREAMING_XML = """<StreamingChannelList version="2.0"
 xmlns="http://www.hikvision.com/ver20/XMLSchema">
<StreamingChannel>
<id>1</id><channelName>摄像机06</channelName><enabled>true</enabled>
<Video>
<videoInputChannelID>1</videoInputChannelID>
<videoCodecType>H.264</videoCodecType>
<videoResolutionWidth>1920</videoResolutionWidth>
<videoResolutionHeight>1080</videoResolutionHeight>
<constantBitRate>2048</constantBitRate>
<maxFrameRate>3000</maxFrameRate>
</Video>
<Audio><audioCompressionType>G.711alaw</audioCompressionType></Audio>
</StreamingChannel>
<StreamingChannel>
<id>2</id><channelName>摄像机06</channelName><enabled>true</enabled>
<Video>
<videoInputChannelID>1</videoInputChannelID>
<videoCodecType>H.264</videoCodecType>
<videoResolutionWidth>704</videoResolutionWidth>
<videoResolutionHeight>480</videoResolutionHeight>
<constantBitRate>1024</constantBitRate>
<maxFrameRate>3000</maxFrameRate>
</Video>
<Audio><audioCompressionType>G.711alaw</audioCompressionType></Audio>
</StreamingChannel>
</StreamingChannelList>"""


def _parse(xml: str) -> dict[str, Any]:
    return _parse_streaming_detail(ET.fromstring(_strip_xmlns(xml)))


def _data(detail: dict[str, Any], channels: list[dict[str, Any]]) -> HikvisionISAPIData:
    d = HikvisionISAPIData(
        device_info={"deviceType": "NVR"}, system_status={}, channels=channels,
        capabilities={}, storage={}, network_interfaces=[],
        streaming_channel_detail=detail,
    )
    return d


NVR_CHANNELS = [
    {"id": "1", "name": "摄像机12", "online": True, "recording": None},
    {"id": "2", "name": "摄像机13", "online": True, "recording": None},
]
IPC_CHANNELS = [
    {"id": "1", "name": "摄像机06", "online": True, "recording": None},
]


class _Coord:
    def __init__(self, data):
        self.data = data
        self.channels = data.channels
        self.network_interfaces = []
        self.device_type = "networkvideorecorder"
        self.device_info = {}
        self.capabilities = {}
        self.streaming_channel_detail = {}
        self.unique_id = f"{DOMAIN}_t"
        self._host = "1.2.3.4"
        self._port = 80
        self._username = "admin"
        self._password = "pw"
        self._verify_ssl = False
        self._use_https = False
        self._listeners = []

    def async_add_listener(self, listener):
        self._listeners.append(listener)
        return lambda: None


def _entry():
    e = type("E", (), {})()
    e.entry_id = "tier_test"
    e.data = {"host": "192.168.10.9"}
    e.options = {}
    return e


# ── parser: owning channel must be captured on NVRs ───────────────────


def test_parser_captures_dyn_video_input_channel_id_for_nvr():
    """NVRs carry the owning channel in dynVideoInputChannelID.

    ``_channel_summary`` only read ``videoInputChannelID``, which is
    absent on the NVR, so the owning channel was None and no tier could
    be derived.
    """
    detail = _parse(NVR_STREAMING_XML)
    by_id = {c["id"]: c for c in detail["channels"]}
    assert by_id["101"].get("dyn_video_input_channel_id") == 1
    assert by_id["201"].get("dyn_video_input_channel_id") == 2


def test_parser_keeps_video_input_channel_id_for_ipc():
    detail = _parse(IPC_STREAMING_XML)
    by_id = {c["id"]: c for c in detail["channels"]}
    assert by_id["1"].get("video_input_channel_id") == 1


# ── tier derivation ───────────────────────────────────────────────────


@pytest.mark.parametrize(
    "stream_id,owner_ch,expected",
    [
        ("101", 1, 1),   # NVR main
        ("102", 1, 2),   # NVR sub
        ("104", 1, 4),   # NVR third
        ("201", 2, 1),
        ("1001", 10, 1),  # double-digit channel
        ("1002", 10, 2),
        ("1", 1, 1),      # IPC main
        ("2", 1, 2),      # IPC sub
        ("3", 1, 3),      # IPC third
    ],
)
def test_stream_tier_derivation(stream_id, owner_ch, expected):
    """Tier = id - channel*100 when id encodes the channel, else id itself."""
    assert sensor_mod._stream_tier(stream_id, owner_ch) == expected


def test_stream_tier_none_when_owner_unknown():
    """Without an owning channel the tier is not guessable."""
    assert sensor_mod._stream_tier("101", None) is None


# ── per-tier value lookup ─────────────────────────────────────────────


def test_nvr_main_stream_values():
    data = _data(_parse(NVR_STREAMING_XML), NVR_CHANNELS)
    assert sensor_mod._channel_tier_field("1", 1, "video_codec")(data) == "H.264"
    assert sensor_mod._channel_tier_field(
        "1", 1, "video_resolution")(data) == "3840x2160"
    assert sensor_mod._channel_tier_field("1", 1, "video_bitrate_kbps")(data) == 16384


def test_nvr_sub_stream_values_differ_from_main():
    """The sub stream must report its OWN numbers, not the main stream's."""
    data = _data(_parse(NVR_STREAMING_XML), NVR_CHANNELS)
    assert sensor_mod._channel_tier_field(
        "1", 2, "video_resolution")(data) == "704x576"
    assert sensor_mod._channel_tier_field("1", 2, "video_bitrate_kbps")(data) == 512
    # and channel 2 is distinct from channel 1
    assert sensor_mod._channel_tier_field("2", 1, "video_codec")(data) == "H.265"
    assert sensor_mod._channel_tier_field(
        "2", 2, "video_resolution")(data) == "640x360"


def test_ipc_tier_lookup_uses_ipc_convention():
    data = _data(_parse(IPC_STREAMING_XML), IPC_CHANNELS)
    assert sensor_mod._channel_tier_field(
        "1", 1, "video_resolution")(data) == "1920x1080"
    assert sensor_mod._channel_tier_field(
        "1", 2, "video_resolution")(data) == "704x480"
    assert sensor_mod._channel_tier_field("1", 2, "video_bitrate_kbps")(data) == 1024


def test_tier_lookup_falls_back_to_id_convention_without_owner_field():
    """Older firmware may omit both channel-id fields.

    The v0.7.3 ``{channel}01`` convention must still work so this change
    does not regress devices whose XML lacks the owner field.
    """
    detail = {
        "first_channel_id": "201",
        "channels": [
            {"id": "201", "video_codec": "H.265", "video_resolution": "1920x1080"},
            {"id": "202", "video_codec": "H.264", "video_resolution": "640x360"},
        ],
    }
    data = _data(detail, [{"id": "2", "name": "摄像机13"}])
    assert sensor_mod._channel_tier_field("2", 1, "video_codec")(data) == "H.265"
    assert sensor_mod._channel_tier_field("2", 2, "video_codec")(data) == "H.264"


def test_missing_tier_returns_none():
    data = _data(_parse(NVR_STREAMING_XML), NVR_CHANNELS)
    # channel 1 has no tier-3 stream in the fixture
    assert sensor_mod._channel_tier_field("1", 3, "video_codec")(data) is None
    # channel 9 does not exist at all
    assert sensor_mod._channel_tier_field("9", 1, "video_codec")(data) is None


# ── entity construction: names, keys, translations ────────────────────


def test_builds_main_and_sub_stream_entities_only():
    """Exactly two tiers x five video/audio fields + one name = 11 entities."""
    data = _data(_parse(NVR_STREAMING_XML), NVR_CHANNELS)
    coord = _Coord(data)
    ents = sensor_mod._build_per_channel_entities(coord, _entry(), NVR_CHANNELS[0])
    keys = [e.entity_description.key for e in ents]

    # five fields for each of main + sub
    for field in ("video_codec", "video_resolution", "video_frame_rate",
                  "video_bitrate", "audio_codec"):
        assert f"channel_1_{field}" in keys, f"main stream {field} missing"
        assert f"channel_1_sub_{field}" in keys, f"sub stream {field} missing"
    # no third stream (user chose main + sub only)
    assert not any("_third_" in k or "_3_" in k for k in keys)


def test_entity_names_carry_camera_name_and_tier():
    """The user's chosen format: 摄像机12 主码流 视频编码."""
    data = _data(_parse(NVR_STREAMING_XML), NVR_CHANNELS)
    ents = sensor_mod._build_per_channel_entities(_Coord(data), _entry(), NVR_CHANNELS[0])
    names = {e.entity_description.key: e.entity_description.name for e in ents}

    assert names["channel_1_video_codec"] == "摄像机12 主码流 视频编码"
    assert names["channel_1_sub_video_codec"] == "摄像机12 子码流 视频编码"
    assert names["channel_1_video_resolution"] == "摄像机12 主码流 分辨率"
    assert names["channel_1_sub_video_bitrate"] == "摄像机12 子码流 码率"


def test_entity_names_use_second_camera_name():
    data = _data(_parse(NVR_STREAMING_XML), NVR_CHANNELS)
    ents = sensor_mod._build_per_channel_entities(_Coord(data), _entry(), NVR_CHANNELS[1])
    names = {e.entity_description.key: e.entity_description.name for e in ents}
    assert names["channel_2_video_codec"] == "摄像机13 主码流 视频编码"
    assert names["channel_2_sub_audio_codec"] == "摄像机13 子码流 音频编码"


def test_dynamic_entities_have_no_translation_key():
    """translation_key would override ``name`` in real HA.

    All channels share one key, so every channel would render the same
    label and the camera names would never appear. The literal name must
    win.
    """
    data = _data(_parse(NVR_STREAMING_XML), NVR_CHANNELS)
    ents = sensor_mod._build_per_channel_entities(_Coord(data), _entry(), NVR_CHANNELS[0])
    assert ents, "should build entities"
    for e in ents:
        assert e.entity_description.translation_key is None, (
            f"{e.entity_description.key} sets translation_key, which would "
            f"override its per-camera name"
        )


def test_channel_name_sensor_still_present_once():
    """The name sensor stays a single entity per channel (not per tier)."""
    data = _data(_parse(NVR_STREAMING_XML), NVR_CHANNELS)
    ents = sensor_mod._build_per_channel_entities(_Coord(data), _entry(), NVR_CHANNELS[0])
    name_keys = [e.entity_description.key for e in ents if e.entity_description.key.endswith("_name")]
    assert name_keys == ["channel_1_name"], f"unexpected name sensors: {name_keys}"


def test_main_stream_key_unchanged_for_upgrade_safety():
    """Main-stream keys must match v0.7.4 so no entity is orphaned."""
    data = _data(_parse(NVR_STREAMING_XML), NVR_CHANNELS)
    ents = sensor_mod._build_per_channel_entities(_Coord(data), _entry(), NVR_CHANNELS[0])
    ids = {e._attr_unique_id for e in ents}
    # pre-v0.7.5 unique_ids for channel 1
    for field in ("video_codec", "video_resolution", "video_frame_rate",
                  "video_bitrate", "audio_codec", "name"):
        assert f"tier_test_channel_1_{field}" in ids, (
            f"unique_id for channel_1_{field} changed — would orphan the "
            f"existing entity in the user's registry"
        )


def test_values_flow_through_to_entities():
    """End to end: the built entity returns the right per-tier value."""
    data = _data(_parse(NVR_STREAMING_XML), NVR_CHANNELS)
    ents = sensor_mod._build_per_channel_entities(_Coord(data), _entry(), NVR_CHANNELS[0])
    by_key = {e.entity_description.key: e for e in ents}

    assert by_key["channel_1_video_resolution"].native_value == "3840x2160"
    assert by_key["channel_1_sub_video_resolution"].native_value == "704x576"
    assert by_key["channel_1_video_bitrate"].native_value == 16384
    assert by_key["channel_1_sub_video_bitrate"].native_value == 512
    assert by_key["channel_1_name"].native_value == "摄像机12"


def test_name_falls_back_when_channel_has_no_name():
    data = _data(_parse(NVR_STREAMING_XML), [{"id": "1", "name": None}])
    ents = sensor_mod._build_per_channel_entities(_Coord(data), _entry(), {"id": "1", "name": None})
    names = {e.entity_description.key: e.entity_description.name for e in ents}
    assert names["channel_1_video_codec"] == "Channel 1 主码流 视频编码"
