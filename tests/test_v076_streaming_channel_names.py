"""v0.7.6: streaming channel list read the wrong XML tag names.

Found while verifying v0.7.5 against the captured fleet data: the IPC
DS-FB2127 showed its channel name as "Channel 1" instead of the real
"摄像机06", and its channel-online binary sensor read False even
though the camera was streaming.

Root cause: ``_parse_streaming_channels_list`` looked up ``name`` and
``online``, but ``/ISAPI/Streaming/channels`` actually emits
``channelName`` and ``enabled``. Verified verbatim in the probe captures
for both the IPC and the NVR:

    <StreamingChannel>
      <id>1</id>
      <channelName>摄像机06</channelName>
      <enabled>true</enabled>
      ...
    </StreamingChannel>

Both lookups therefore returned None, so the name fell back to
``f"Channel {id}"`` and online evaluated to False.

The sibling parser ``_parse_channels`` (InputProxyChannelList) is
unaffected — that shape really does use ``<name>`` and ``<online>``, as
captured from the DS-7708N-I4.
"""

from __future__ import annotations

from xml.etree import ElementTree as ET

import pytest

import tests.conftest  # noqa: F401  installs homeassistant stubs

from custom_components.hikvision_isapi_performance.coordinator import (
    _parse_channels,
    _parse_streaming_channels_list,
)
from custom_components.hikvision_isapi_performance.isapi_client import _strip_xmlns


# Verbatim from the DS-FB2127 probe (10.18.176.18), namespaces preserved
# so the strip path is exercised too.
IPC_STREAMING_XML = """<StreamingChannelList version="2.0"
 xmlns="http://www.hikvision.com/ver20/XMLSchema">
<StreamingChannel version="2.0" xmlns="http://www.hikvision.com/ver20/XMLSchema">
<id>1</id>
<channelName>摄像机06</channelName>
<enabled>true</enabled>
<Video>
<videoInputChannelID>1</videoInputChannelID>
<videoCodecType>H.264</videoCodecType>
</Video>
</StreamingChannel>
<StreamingChannel version="2.0" xmlns="http://www.hikvision.com/ver20/XMLSchema">
<id>2</id>
<channelName>摄像机06</channelName>
<enabled>false</enabled>
<Video>
<videoInputChannelID>1</videoInputChannelID>
<videoCodecType>H.264</videoCodecType>
</Video>
</StreamingChannel>
</StreamingChannelList>"""

# Verbatim from the DS-8632N-I8 probe (192.168.10.9). On NVRs the
# channelName is just the stream id, but <enabled> is still meaningful.
NVR_STREAMING_XML = """<StreamingChannelList version="2.0"
 xmlns="http://www.isapi.org/ver20/XMLSchema">
<StreamingChannel>
<id>101</id>
<channelName>101</channelName>
<enabled>true</enabled>
</StreamingChannel>
</StreamingChannelList>"""

# Verbatim from the DS-7708N-I4 probe (192.168.10.10): InputProxyChannelList
# really does use <name> and has no <online> element at all.
NVR_INPUTPROXY_XML = """<InputProxyChannelList version="1.0"
 xmlns="http://www.hikvision.com/ver20/XMLSchema" size="0">
<InputProxyChannel version="1.0" xmlns="http://www.hikvision.com/ver20/XMLSchema">
<id>1</id>
<name>摄像机12</name>
<sourceInputPortDescriptor>
<proxyProtocol>HIKVISION</proxyProtocol>
<ipAddress>10.18.176.10</ipAddress>
</sourceInputPortDescriptor>
<enableTiming>true</enableTiming>
</InputProxyChannel>
</InputProxyChannelList>"""


def _parse(xml: str, fn):
    return fn(ET.fromstring(_strip_xmlns(xml)))


# ── the defect ────────────────────────────────────────────────────────


def test_ipc_channel_name_comes_from_channelName_element():
    """IPC name must be the real camera name, not the "Channel 1" fallback.

    v0.7.7: the fixture's two streams both carry ``videoInputChannelID=1``,
    i.e. they are the main and sub stream of ONE camera, so they now group
    into a single channel. The point of this test is unchanged — the name
    must come from ``<channelName>``, not the ``Channel {id}``` fallback.
    """
    channels = _parse(IPC_STREAMING_XML, _parse_streaming_channels_list)
    assert len(channels) == 1, (
        "two streams of the same camera must group into one channel"
    )
    assert channels[0]["id"] == "1"
    assert channels[0]["name"] == "摄像机06"


def test_ipc_online_comes_from_enabled_element():
    """``<enabled>`` is this shape's online indicator.

    The grouped 摄像机06 channel merges an enabled main stream with a disabled
    sub stream, so it is online (a camera streaming on any stream counts
    as online). To keep the original v0.7.6 guarantee that ``enabled=false``
    is NOT coerced to True, assert it directly on a single disabled stream.
    """
    channels = _parse(IPC_STREAMING_XML, _parse_streaming_channels_list)
    assert channels[0]["online"] is True

    # A lone camera whose only stream is disabled must read offline.
    disabled_xml = """<StreamingChannelList>
<StreamingChannel><id>1</id><channelName>Cam</channelName>
<enabled>false</enabled>
<Video><videoInputChannelID>1</videoInputChannelID></Video>
</StreamingChannel>
</StreamingChannelList>"""
    disabled = _parse(disabled_xml, _parse_streaming_channels_list)
    assert len(disabled) == 1
    assert disabled[0]["online"] is False, (
        "enabled=false must not be coerced to True"
    )


def test_nvr_streaming_channel_parses_enabled():
    """The isapi.org-namespace NVR uses the same tag names."""
    channels = _parse(NVR_STREAMING_XML, _parse_streaming_channels_list)
    assert len(channels) == 1
    assert channels[0]["id"] == "101"
    assert channels[0]["name"] == "101"
    assert channels[0]["online"] is True


def test_inputproxy_parser_still_uses_name_element():
    """Guard: the sibling parser must keep its own (different) tag names.

    InputProxyChannelList genuinely uses <name>, and carries no <online>
    element — that value comes from the per-channel /status endpoint and
    is merged in by the coordinator.
    """
    channels = _parse(NVR_INPUTPROXY_XML, _parse_channels)
    assert len(channels) == 1
    assert channels[0]["name"] == "摄像机12"
    # No <online> in this shape → must not claim False as a fact.
    assert channels[0]["online"] is not True


def test_streaming_parser_recording_stays_tri_state():
    """v0.7.4 behaviour must survive the v0.7.6 tag-name fix."""
    channels = _parse(IPC_STREAMING_XML, _parse_streaming_channels_list)
    for ch in channels:
        assert ch["recording"] is None, (
            "this shape carries no <recordStatus>; recording must be unknown"
        )


def test_streaming_parser_missing_channelName_falls_back():
    """A stream with no channelName still yields a usable label."""
    xml = """<StreamingChannelList>
<StreamingChannel><id>7</id><enabled>true</enabled></StreamingChannel>
</StreamingChannelList>"""
    channels = _parse(xml, _parse_streaming_channels_list)
    assert len(channels) == 1
    assert channels[0]["name"] == "Channel 7"
    assert channels[0]["online"] is True


def test_streaming_parser_empty_list():
    assert _parse_streaming_channels_list(None) == []
    xml = "<StreamingChannelList></StreamingChannelList>"
    assert _parse(xml, _parse_streaming_channels_list) == []
