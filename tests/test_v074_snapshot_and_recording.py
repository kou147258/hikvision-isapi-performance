"""v0.7.4: NVR/DVR snapshot routing + id format + recording-unknown.

All three defects were confirmed by probing the live DS-7708N-I4
(deviceType=DVR, 192.168.10.10), not inferred from reading code.

DEFECT 1 - DVR was routed to the IPC snapshot endpoint.

``camera.async_camera_image`` chose the proxy endpoint only for
``DEVICE_TYPE_NETWORK_VIDEO_RECORDER``. The module docstring says
"NVR / DVR" but the condition covered NVR alone, so a DVR fell through
to the direct ``/ISAPI/Streaming/channels/{id}/picture`` branch.
Probe result on the user's DS-7708N-I4:

    /ISAPI/Streaming/channels/1/picture                -> HTTP 400
    /ISAPI/ContentMgmt/StreamingProxy/channels/1/picture -> HTTP 503
    /ISAPI/ContentMgmt/StreamingProxy/channels/101/picture -> HTTP 200, 30142 B JPEG

That is why NVR cameras showed no picture while IPCs worked.

DEFECT 2 - the snapshot path used the channel id verbatim.

Hikvision's snapshot endpoints use ``{channel}{stream}`` numbering
(``101`` = channel 1 main stream), but ``coordinator.channels`` carries
the plain InputProxy ids (``"1"``). Requesting ``.../channels/1/picture``
returned 503 (Device Busy) on the NVR; ``101`` returned the real JPEG.
IPCs are unaffected because their channel ids already are ``"1"``, and
their own endpoint accepts both ``1`` and ``101``.

DEFECT 3 - "recording" rendered False when the device never reported it.

Neither ``InputProxyChannelList`` nor
``/ISAPI/ContentMgmt/InputProxy/channels/{id}/status`` carries a
``recordStatus`` element on the user's NVRs (probed: 0 occurrences), and
every ``/ISAPI/ContentMgmt/Recording/*`` endpoint returned 404. The
parsers coerce the missing value to ``False`` and the entities did
``bool(ch.get("recording"))``, so the switch showed "off" and the binary
sensor showed "未在运行" — asserting a fact the device never stated.

The motion sensor already did the right thing (returns None when
``motion_detected`` is absent). Recording must match that: no data means
unknown, not False.
"""

from __future__ import annotations

from typing import Any

import pytest

import tests.conftest  # noqa: F401  installs homeassistant stubs

from custom_components.hikvision_isapi_performance.const import (
    DEVICE_TYPE_DVR,
    DEVICE_TYPE_IPCAMERA,
    DEVICE_TYPE_NETWORK_VIDEO_RECORDER,
    DOMAIN,
)
from custom_components.hikvision_isapi_performance.coordinator import (
    HikvisionISAPIData,
    _parse_channel_status,
    _parse_channel_status_extended,
    _parse_channels,
)
from custom_components.hikvision_isapi_performance.isapi_client import _strip_xmlns
from custom_components.hikvision_isapi_performance import camera as camera_mod
from custom_components.hikvision_isapi_performance import (
    binary_sensor as binary_mod,
)
from custom_components.hikvision_isapi_performance import switch as switch_mod
from xml.etree import ElementTree as ET


def _entry():
    e = type("E", (), {})()
    e.entry_id = "cam_test"
    e.data = {"host": "192.168.10.10"}
    e.options = {}
    return e


class _FakeCoord:
    """Minimal coordinator stand-in carrying the fields camera.py reads."""

    def __init__(self, device_type: str, data: HikvisionISAPIData | None = None):
        self._host = "192.168.10.10"
        self._port = 80
        self._username = "admin"
        self._password = "pw"
        self._verify_ssl = False
        self._use_https = False
        self.channels: list[dict] = []
        self.network_interfaces: list[dict] = []
        self.device_type = device_type
        self.device_info: dict = {}
        self.capabilities: dict = {}
        self.streaming_channel_detail: dict = {}
        self.unique_id = f"{DOMAIN}_cam"
        self.data = data
        self._listeners: list = []

    def async_add_listener(self, listener):
        self._listeners.append(listener)
        return lambda: None


def _make_camera(device_type: str, channel: dict) -> camera_mod.HikvisionISAPICamera:
    data = HikvisionISAPIData(
        device_info={"deviceType": device_type}, system_status={},
        channels=[channel], capabilities={}, storage={}, network_interfaces=[],
    )
    data.device_type = device_type
    coord = _FakeCoord(device_type, data)
    return camera_mod.HikvisionISAPICamera(coord, _entry(), channel)


# ── DEFECT 1 + 2: snapshot path construction ─────────────────────────


@pytest.mark.parametrize(
    "device_type,label",
    [
        (DEVICE_TYPE_NETWORK_VIDEO_RECORDER, "NVR"),
        (DEVICE_TYPE_DVR, "DVR"),
    ],
)
def test_recorder_snapshot_uses_proxy_endpoint(device_type, label):
    """Both NVR and DVR must use the StreamingProxy endpoint.

    Probed on DS-7708N-I4 (deviceType=DVR): the direct streaming
    endpoint returns 400, the proxy returns the JPEG. Pre-fix only
    NVR was routed to the proxy, so DVR cameras never rendered.
    """
    cam = _make_camera(device_type, {"id": "1", "name": "摄像机12"})
    path = cam._snapshot_path()
    assert "/ISAPI/ContentMgmt/StreamingProxy/" in path, (
        f"{label} must use the proxy endpoint, got {path}"
    )
    assert "/ISAPI/Streaming/channels/" not in path


def test_recorder_snapshot_id_uses_main_stream_convention():
    """NVR/DVR channel "1" must request stream id "101".

    Probe: ``.../StreamingProxy/channels/1/picture`` -> HTTP 503,
    ``.../StreamingProxy/channels/101/picture`` -> HTTP 200 + 30142 B JPEG.
    """
    cam = _make_camera(DEVICE_TYPE_NETWORK_VIDEO_RECORDER, {"id": "1", "name": "摄像机12"})
    path = cam._snapshot_path()
    assert "/channels/101/picture" in path, f"expected stream id 101, got {path}"
    assert "/channels/1/picture" not in path


def test_recorder_snapshot_channel_2_uses_201():
    """Channel 2 must map to 201, not 102 (which is channel 1's sub-stream)."""
    cam = _make_camera(DEVICE_TYPE_NETWORK_VIDEO_RECORDER, {"id": "2", "name": "摄像机13"})
    path = cam._snapshot_path()
    assert "/channels/201/picture" in path


def test_recorder_snapshot_channel_10_uses_1001():
    """Double-digit channels: channel 10 -> 1001 (not 100 1)."""
    cam = _make_camera(DEVICE_TYPE_NETWORK_VIDEO_RECORDER, {"id": "10", "name": "摄像机08"})
    path = cam._snapshot_path()
    assert "/channels/1001/picture" in path


def test_ipc_snapshot_keeps_direct_endpoint():
    """IPC must keep using the direct streaming endpoint (it works today)."""
    cam = _make_camera(DEVICE_TYPE_IPCAMERA, {"id": "1", "name": "摄像机06"})
    path = cam._snapshot_path()
    assert path.startswith("/ISAPI/Streaming/channels/")
    assert "StreamingProxy" not in path


def test_ipc_snapshot_id_left_verbatim():
    """IPC channel ids already match the streaming numbering — don't rewrite.

    The DS-FB2127 reports channels "1"/"2"/"3" and its own endpoint
    accepts them; appending "01" would produce "101" which on an IPC is
    a different (also valid) id, but leaving it alone preserves the
    behaviour that already works.
    """
    cam = _make_camera(DEVICE_TYPE_IPCAMERA, {"id": "1", "name": "摄像机06"})
    path = cam._snapshot_path()
    assert "/channels/1/picture" in path


def test_snapshot_path_appends_size_query_when_requested():
    """width/height are forwarded as query params, path shape preserved."""
    cam = _make_camera(DEVICE_TYPE_NETWORK_VIDEO_RECORDER, {"id": "1", "name": "摄像机12"})
    path = cam._snapshot_path(width=1920, height=1080)
    assert "/channels/101/picture?" in path
    assert "videoResolutionWidth=1920" in path
    assert "videoResolutionHeight=1080" in path


# ── DEFECT 3: recording must be unknown, not False, when unreported ──

# Real DS-7708N-I4 InputProxyChannelList entry: no <recordStatus> at all.
NVR_CHANNEL_NO_RECORD_STATUS = """<InputProxyChannelList version="1.0"
 xmlns="http://www.hikvision.com/ver20/XMLSchema" size="0">
<InputProxyChannel version="1.0" xmlns="http://www.hikvision.com/ver20/XMLSchema">
<id>1</id>
<name>摄像机12</name>
<sourceInputPortDescriptor>
<proxyProtocol>HIKVISION</proxyProtocol>
<ipAddress>10.18.176.10</ipAddress>
</sourceInputPortDescriptor>
<enableAnr>false</enableAnr>
<enableTiming>true</enableTiming>
</InputProxyChannel>
</InputProxyChannelList>"""

# Real DS-7708N-I4 per-channel status: <online> present, recordStatus absent.
NVR_STATUS_NO_RECORD = """<InputProxyChannelStatus version="1.0"
 xmlns="http://www.hikvision.com/ver20/XMLSchema">
<id>1</id>
<online>true</online>
<chanDetectResult>connect</chanDetectResult>
</InputProxyChannelStatus>"""

# A firmware that DOES report recordStatus must still yield True/False.
STATUS_RECORDING = """<InputProxyChannelStatus>
<id>1</id><online>true</online><recordStatus>recording</recordStatus>
</InputProxyChannelStatus>"""

STATUS_IDLE = """<InputProxyChannelStatus>
<id>1</id><online>true</online><recordStatus>idle</recordStatus>
</InputProxyChannelStatus>"""


def test_parse_channels_omits_recording_when_field_absent():
    """A channel with no <recordStatus> must not claim recording=False.

    Design: ``None`` marks "device never reported this", distinct from
    ``False`` which means the device explicitly said it is not recording.
    """
    root = ET.fromstring(_strip_xmlns(NVR_CHANNEL_NO_RECORD_STATUS))
    channels = _parse_channels(root)
    assert len(channels) == 1
    assert "recording" not in channels[0] or channels[0]["recording"] is None, (
        "recording must not be False when the device never reported it"
    )


def test_channel_status_omits_recording_when_field_absent():
    """Same for the per-channel status endpoint."""
    root = ET.fromstring(_strip_xmlns(NVR_STATUS_NO_RECORD))
    st = _parse_channel_status(root)
    assert st["online"] is True, "online IS reported here, must be True"
    assert "recording" not in st or st["recording"] is None


def test_channel_status_extended_omits_recording_when_field_absent():
    root = ET.fromstring(_strip_xmlns(NVR_STATUS_NO_RECORD))
    st = _parse_channel_status_extended(root)
    assert st["online"] is True
    assert "recording" not in st or st["recording"] is None


def test_channel_status_reports_recording_when_field_present():
    """Firmware that does report it must still surface True/False."""
    st_rec = _parse_channel_status(ET.fromstring(_strip_xmlns(STATUS_RECORDING)))
    assert st_rec["recording"] is True

    st_idle = _parse_channel_status(ET.fromstring(_strip_xmlns(STATUS_IDLE)))
    assert st_idle["recording"] is False


class _BinCoord:
    """Coordinator fake for binary_sensor / switch entity tests."""

    def __init__(self, data):
        self.data = data
        self.channels = data.channels
        self.network_interfaces = []
        self.device_type = "networkvideorecorder"
        self.device_info = {}
        self.capabilities = {}
        self.streaming_channel_detail = {}
        self.unique_id = f"{DOMAIN}_bin"
        self._host = "192.168.10.10"
        self._port = 80
        self._username = "admin"
        self._password = "pw"
        self._verify_ssl = False
        self._use_https = False
        self._listeners: list = []

    def async_add_listener(self, listener):
        self._listeners.append(listener)
        return lambda: None


def _data_with_channels(channels: list[dict]) -> HikvisionISAPIData:
    d = HikvisionISAPIData(
        device_info={"deviceType": "DVR"}, system_status={}, channels=channels,
        capabilities={}, storage={}, network_interfaces=[],
    )
    d.device_type = DEVICE_TYPE_DVR
    return d


def test_recording_binary_sensor_is_none_when_unreported():
    """'录像中' must read unknown, not 未在运行, when the device stays silent."""
    data = _data_with_channels([{"id": "1", "name": "摄像机12", "online": True}])
    coord = _BinCoord(data)
    ent = binary_mod.HikvisionISAPIChannelRecordingBinarySensor(
        coord, _entry(), "1", "摄像机12",
    )
    assert ent.is_on is None


def test_recording_binary_sensor_reflects_true_when_reported():
    data = _data_with_channels(
        [{"id": "1", "name": "摄像机12", "online": True, "recording": True}]
    )
    ent = binary_mod.HikvisionISAPIChannelRecordingBinarySensor(
        _BinCoord(data), _entry(), "1", "摄像机12",
    )
    assert ent.is_on is True


def test_recording_binary_sensor_reflects_false_when_explicitly_idle():
    data = _data_with_channels(
        [{"id": "1", "name": "摄像机12", "online": True, "recording": False}]
    )
    ent = binary_mod.HikvisionISAPIChannelRecordingBinarySensor(
        _BinCoord(data), _entry(), "1", "摄像机12",
    )
    assert ent.is_on is False


def test_recording_switch_is_none_when_unreported():
    """The switch must not display 'off' for a state it cannot know."""
    data = _data_with_channels([{"id": "1", "name": "摄像机12", "online": True}])
    ent = switch_mod.HikvisionISAPIRecordingSwitch(
        _BinCoord(data), _entry(), {"id": "1", "name": "摄像机12"},
    )
    assert ent.is_on is None


def test_online_binary_sensor_still_true_when_recording_absent():
    """Guard: fixing recording must not disturb the online sensor.

    The probe showed <online>true</online> IS present in the per-channel
    status response, so online must keep reporting True.
    """
    data = _data_with_channels([{"id": "1", "name": "摄像机12", "online": True}])
    ent = binary_mod.HikvisionISAPIChannelOnlineBinarySensor(
        _BinCoord(data), _entry(), "1", "摄像机12",
    )
    assert ent.is_on is True
