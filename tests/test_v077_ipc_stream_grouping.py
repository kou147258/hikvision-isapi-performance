"""v0.7.7: an IPC's streams were each treated as a separate camera channel.

Probed on the live fleet. Every IPC returns one ``<StreamingChannel>`` per
*stream*, not per camera, and all of them carry the same physical channel
number in ``Video/dynVideoInputChannelID`` (NVR schema) or
``Video/videoInputChannelID`` (IPC schema):

    IPC摄像机10 (DS-2DF8C832MX-ZDK) — 5 streams, 1 camera
        stream 101..105, all channelName='摄像机10', all owner='1'
    IPC摄像机06 (DS-FB2127) — 3 streams, 1 camera
        stream 1,2,3, all channelName='摄像机06', all owner='1'
    NVR录像机01 (DS-8632N-I8) — 35 streams, 13 cameras
        stream 101/102/104 -> owner 1, 201/202/204 -> owner 2, ...

``_parse_streaming_channels_list`` emitted one channel dict per stream, so a
single IPC produced 5 sets of camera / recording-switch / binary-sensor /
per-channel-sensor entities, all pointing at the same physical camera. After
v0.7.5 raised per-channel sensors from 6 to 11, the 摄像机10 IPC alone
generated 5 x 11 = 55 sensors.

Fix: group streams by their physical channel number. One camera -> one
channel entry. The entry's ``id`` becomes the physical channel number ("1"),
which makes the existing ``_streaming_id_candidates`` lookup resolve
correctly for both id schemes:

    NVR-style IPC  id='1' -> main ['101','1'] hits stream 101
                          -> sub  ['102']    hits stream 102
    legacy IPC     id='1' -> main ['101','1'] hits stream 1
                          -> sub  ['102','2'] hits stream 2

NVRs are unaffected: their channel list comes from
``/ISAPI/ContentMgmt/InputProxy/channels`` (one entry per physical channel
already), and this parser is only their fallback.

Tests use XML captured from the real devices.
"""

from __future__ import annotations

import sys
from pathlib import Path
from xml.etree import ElementTree as ET

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import tests.conftest  # noqa: F401  installs the HA stubs

from custom_components.hikvision_isapi_performance.coordinator import (  # noqa: E402
    _parse_streaming_channels_list,
)
from custom_components.hikvision_isapi_performance.isapi_client import (  # noqa: E402
    _strip_xmlns,
)
from custom_components.hikvision_isapi_performance.sensor import (  # noqa: E402
    _channel_tier_field,
    _streaming_id_candidates,
)

# v0.8: fixtures live INSIDE the repo (tests/fixtures_v08) and are
# sanitized with the same mapping table used for every other v0.8 file.
# They previously pointed at a hard-coded desktop capture directory
# outside the repo — a data source that (a) isn't in version control, so
# a fresh clone failed, and (b) was never sanitized, which is what broke
# these two assertions after the sanitizing pass renamed the expected
# camera names but not the XML they were read from.
CAPTURES = _REPO_ROOT / "tests" / "fixtures_v08"


def _root(prefix: str) -> ET.Element:
    files = sorted(CAPTURES.glob(prefix + "__S_stream__ISAPI_Streaming_channels.xml"))
    assert files, f"missing capture for {prefix} in {CAPTURES}"
    return ET.fromstring(
        _strip_xmlns(files[-1].read_text(encoding="utf-8", errors="replace"))
    )


def _ids(channels) -> list[str]:
    return [c["id"] for c in channels]


# ---------------------------------------------------------------------------
# Real-device regression: one IPC must yield exactly one channel
# ---------------------------------------------------------------------------


def test_ipc_with_five_streams_yields_one_channel() -> None:
    """摄像机10 IPC: 5 streams (101-105) must collapse to 1 channel."""
    channels = _parse_streaming_channels_list(_root("10_18_176_13"))

    assert len(channels) == 1, (
        f"a single IPC must produce one channel, got {len(channels)}: "
        f"{_ids(channels)}. Each extra entry duplicates a whole set of "
        f"camera / switch / binary-sensor / sensor entities."
    )


def test_ipc_with_legacy_ids_yields_one_channel() -> None:
    """摄像机06 IPC (DS-FB2127): legacy stream ids 1,2,3 -> 1 channel."""
    channels = _parse_streaming_channels_list(_root("10_18_176_18"))

    assert len(channels) == 1
    assert _ids(channels) == ["1"]


def test_second_ipc_also_yields_one_channel() -> None:
    """摄像机12 IPC: streams 101,102,103 -> 1 channel."""
    channels = _parse_streaming_channels_list(_root("10_18_176_10"))

    assert len(channels) == 1
    assert channels[0]["name"] == "摄像机12"


def test_nvr_keeps_one_channel_per_physical_camera() -> None:
    """录像机01 NVR: 35 streams across 13 cameras must stay 13 channels.

    Grouping must not over-collapse — an NVR genuinely has many cameras.
    """
    channels = _parse_streaming_channels_list(_root("192_168_10_9"))

    assert len(channels) == 13, f"expected 13 physical cameras, got {len(channels)}"
    assert _ids(channels) == [str(n) for n in range(1, 14)]


# ---------------------------------------------------------------------------
# The grouped channel id must still resolve to the right stream
# ---------------------------------------------------------------------------


def test_grouped_id_resolves_main_and_sub_stream_on_nvr_style_ipc() -> None:
    """摄像机10 IPC after grouping: id='1' must find streams 101 and 102.

    Expected values read from the captured XML (probe_tier_resolution.py):
    stream 101 = 2560x1440 @25fps 8192kbps, stream 102 = 704x576 @25fps
    512kbps.
    """
    detail_root = _root("10_18_176_13")
    from custom_components.hikvision_isapi_performance.coordinator import (
        _parse_streaming_detail,
    )

    detail = _parse_streaming_detail(detail_root)
    channels = _parse_streaming_channels_list(detail_root)
    ch_id = channels[0]["id"]
    assert ch_id == "1"

    class _Data:
        streaming_channel_detail = detail

    main_res = _channel_tier_field(ch_id, 1, "video_resolution")(_Data())
    sub_res = _channel_tier_field(ch_id, 2, "video_resolution")(_Data())
    main_bitrate = _channel_tier_field(ch_id, 1, "video_bitrate_kbps")(_Data())

    assert main_res == "2560x1440", f"main stream resolution wrong: {main_res!r}"
    assert sub_res == "704x576", f"sub stream resolution wrong: {sub_res!r}"
    assert main_bitrate == 8192, f"main stream bitrate wrong: {main_bitrate!r}"
    assert main_res != sub_res, "main and sub stream must differ"


def test_grouped_id_resolves_main_and_sub_stream_on_legacy_ipc() -> None:
    """摄像机06 IPC (ids 1,2,3): id='1' must find stream 1 (main) and 2 (sub).

    ``_streaming_id_candidates`` takes a single argument (the channel id)
    and returns the bare id first, then the ``{channel}01`` recorder
    convention -- verified against the implementation, not assumed.

    Expected values from the captured XML: stream 1 = H.264 1920x1080
    @30fps 2048kbps, stream 2 = H.264 704x480 @30fps 1024kbps.
    """
    detail_root = _root("10_18_176_18")
    from custom_components.hikvision_isapi_performance.coordinator import (
        _parse_streaming_detail,
    )

    detail = _parse_streaming_detail(detail_root)
    channels = _parse_streaming_channels_list(detail_root)
    ch_id = channels[0]["id"]

    assert _streaming_id_candidates(ch_id) == ["1", "101"]

    class _Data:
        streaming_channel_detail = detail

    main_res = _channel_tier_field(ch_id, 1, "video_resolution")(_Data())
    sub_res = _channel_tier_field(ch_id, 2, "video_resolution")(_Data())
    main_bitrate = _channel_tier_field(ch_id, 1, "video_bitrate_kbps")(_Data())
    sub_bitrate = _channel_tier_field(ch_id, 2, "video_bitrate_kbps")(_Data())

    assert main_res == "1920x1080", f"main stream resolution wrong: {main_res!r}"
    assert sub_res == "704x480", f"sub stream resolution wrong: {sub_res!r}"
    assert main_bitrate == 2048, f"main stream bitrate wrong: {main_bitrate!r}"
    assert sub_bitrate == 1024, f"sub stream bitrate wrong: {sub_bitrate!r}"


def test_nvr_grouped_ids_resolve_their_own_streams() -> None:
    """录像机01 NVR: channel 2 must read stream 201/202, not channel 1's.

    Expected values from the captured XML: stream 101 = 3840x2160 H.264,
    stream 201 = 1920x1080 H.265, stream 202 = 640x360 H.264. The codec
    difference between channel 1 (H.264) and channel 2 (H.265) is what
    makes this a meaningful cross-channel check.
    """
    from custom_components.hikvision_isapi_performance.coordinator import (
        _parse_streaming_detail,
    )

    detail = _parse_streaming_detail(_root("192_168_10_9"))

    class _Data:
        streaming_channel_detail = detail

    ch1_main_res = _channel_tier_field("1", 1, "video_resolution")(_Data())
    ch1_main_codec = _channel_tier_field("1", 1, "video_codec")(_Data())
    ch2_main_res = _channel_tier_field("2", 1, "video_resolution")(_Data())
    ch2_main_codec = _channel_tier_field("2", 1, "video_codec")(_Data())
    ch2_sub_res = _channel_tier_field("2", 2, "video_resolution")(_Data())

    assert ch1_main_res == "3840x2160", f"channel 1 main wrong: {ch1_main_res!r}"
    assert ch1_main_codec == "H.264", f"channel 1 codec wrong: {ch1_main_codec!r}"
    assert ch2_main_res == "1920x1080", (
        f"channel 2 read the wrong stream: {ch2_main_res!r}"
    )
    assert ch2_main_codec == "H.265", f"channel 2 codec wrong: {ch2_main_codec!r}"
    assert ch2_sub_res == "640x360", f"channel 2 sub wrong: {ch2_sub_res!r}"


# ---------------------------------------------------------------------------
# Channel name and online state must survive grouping
# ---------------------------------------------------------------------------


def test_grouped_channel_keeps_real_name() -> None:
    """The grouped entry must carry the camera name, not a stream id."""
    for prefix, expected in [
        ("10_18_176_13", "摄像机10"),
        ("10_18_176_18", "摄像机06"),
        ("10_18_176_10", "摄像机12"),
    ]:
        channels = _parse_streaming_channels_list(_root(prefix))
        assert channels[0]["name"] == expected, (
            f"{prefix}: expected name {expected!r}, got {channels[0]['name']!r}"
        )


def test_grouped_channel_reports_online_true() -> None:
    """All probed IPC streams report enabled=true; the channel must too."""
    for prefix in ("10_18_176_13", "10_18_176_18", "10_18_176_10"):
        channels = _parse_streaming_channels_list(_root(prefix))
        assert channels[0]["online"] is True, f"{prefix}: online should be True"


def test_streams_without_channel_number_fall_back_to_own_id() -> None:
    """A stream with no owner field must still appear (no silent data loss)."""
    xml = (
        "<StreamingChannelList>"
        "<StreamingChannel><id>7</id><channelName>No Owner</channelName>"
        "<enabled>true</enabled></StreamingChannel>"
        "</StreamingChannelList>"
    )
    channels = _parse_streaming_channels_list(ET.fromstring(xml))

    assert len(channels) == 1
    assert channels[0]["id"] == "7", "must fall back to the stream id"
    assert channels[0]["name"] == "No Owner"


def test_mixed_owner_and_ownerless_streams_both_survive() -> None:
    """Grouping must not drop streams that lack the owner field."""
    xml = (
        "<StreamingChannelList>"
        "<StreamingChannel><id>101</id><channelName>Cam A</channelName>"
        "<enabled>true</enabled><Video><dynVideoInputChannelID>1"
        "</dynVideoInputChannelID></Video></StreamingChannel>"
        "<StreamingChannel><id>102</id><channelName>Cam A</channelName>"
        "<enabled>true</enabled><Video><dynVideoInputChannelID>1"
        "</dynVideoInputChannelID></Video></StreamingChannel>"
        "<StreamingChannel><id>999</id><channelName>Orphan</channelName>"
        "<enabled>true</enabled></StreamingChannel>"
        "</StreamingChannelList>"
    )
    channels = _parse_streaming_channels_list(ET.fromstring(xml))

    ids = _ids(channels)
    assert "1" in ids, f"owner-grouped channel missing: {ids}"
    assert "999" in ids, f"ownerless stream was dropped: {ids}"
    assert len(channels) == 2


def test_grouping_prefers_enabled_stream_for_online_state() -> None:
    """If any stream of a camera is enabled, the camera counts as online."""
    xml = (
        "<StreamingChannelList>"
        "<StreamingChannel><id>101</id><channelName>Cam</channelName>"
        "<enabled>false</enabled><Video><dynVideoInputChannelID>1"
        "</dynVideoInputChannelID></Video></StreamingChannel>"
        "<StreamingChannel><id>102</id><channelName>Cam</channelName>"
        "<enabled>true</enabled><Video><dynVideoInputChannelID>1"
        "</dynVideoInputChannelID></Video></StreamingChannel>"
        "</StreamingChannelList>"
    )
    channels = _parse_streaming_channels_list(ET.fromstring(xml))

    assert len(channels) == 1
    assert channels[0]["online"] is True
