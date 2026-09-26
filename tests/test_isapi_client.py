"""Tests for the ISAPI client (v0.6.12 httpx-based implementation).

The HTTP transport is exercised with ``httpx.MockTransport`` so we
don't need a real Hikvision device on the network. The auth flow,
network-error handling, and content-type handling all get covered
end-to-end here.

The pure-XML extractor functions in ``coordinator.py`` (``_parse_*``)
have their own test cases below — they don't depend on the HTTP
transport and stay green regardless of which HTTP library we use.
"""

from __future__ import annotations

from xml.etree import ElementTree as ET

import httpx
import pytest

from custom_components.hikvision_isapi_performance.coordinator import (
    _parse_channel_status,
    _parse_channel_status_extended,
    _parse_channels,
    _parse_device_info,
    _parse_network_interfaces,
    _parse_storage,
    _parse_streaming_channels,
    _parse_system_status,
    normalize_device_type,
)
from custom_components.hikvision_isapi_performance.isapi_client import (
    ISAPIAuthError,
    ISAPIConnectionError,
    ISAPIError,
    ISAPIClient,
)


# ---- helpers ----


def _xml_response(body: str, status: int = 200) -> httpx.Response:
    return httpx.Response(
        status, content=body.encode("utf-8"),
        headers={"Content-Type": "application/xml"},
    )


def _text_response(body: str, status: int = 200) -> httpx.Response:
    return httpx.Response(
        status, content=body.encode("utf-8"),
        headers={"Content-Type": "text/plain"},
    )


def _build_client(handler) -> ISAPIClient:
    """Build an ISAPIClient whose internal httpx.AsyncClient uses a mock.

    We swap in ``httpx.MockTransport`` on the private ``_client`` slot
    so the auth/URL/state logic still flows through the real client
    class. Setup is synchronous because ``httpx.AsyncClient(...)`` is
    a non-blocking constructor — only ``.request()`` is async.
    """
    client = ISAPIClient(
        host="10.18.176.10",
        username="admin",
        password="pass",
        port=80,
        use_https=False,
    )
    # The transport must be passed to the constructor — httpx binds it
    # inside __init__ and ignores later assignment to ``_transport``.
    # See test_v0612_httpx.py for the full explanation.
    real = httpx.AsyncClient(
        timeout=10.0, verify=False, follow_redirects=True,
        transport=httpx.MockTransport(handler),
    )
    client._client = real
    return client


# ---- auth flow: digest on first hit ----


@pytest.mark.asyncio
async def test_get_text_succeeds_with_digest_on_first_hit():
    """Device offers Digest on first 401 → client retries with digest."""
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(dict(request.headers))
        if len(calls) == 1:
            # First attempt: server challenges with Digest.
            return httpx.Response(
                401,
                headers={
                    "WWW-Authenticate": (
                        'Digest realm="IPCamera", nonce="abc123", '
                        'qop="auth", algorithm=MD5'
                    ),
                },
            )
        # Second attempt: include the Authorization header the client
        # computed from the Digest challenge.
        return _text_response("<DeviceInfo/>")

    client = _build_client(handler)
    try:
        body = await client.get_text("/ISAPI/System/deviceInfo")
        assert body == "<DeviceInfo/>"
        # Two requests: initial (no auth) + retry (with digest).
        assert len(calls) == 2
        # First request: no Authorization header.
        assert "authorization" not in calls[0]
        # Second request: the client added a Digest header.
        assert "authorization" in calls[1]
        assert calls[1]["authorization"].startswith("Digest ")
    finally:
        await client._client.aclose()


@pytest.mark.asyncio
async def test_get_text_falls_back_to_basic_when_no_digest_in_challenge():
    """Device returns 401 with a Basic-only challenge → client retries
    with Basic and locks it in for the rest of the session."""
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(dict(request.headers))
        if len(calls) == 1:
            return httpx.Response(
                401,
                headers={"WWW-Authenticate": 'Basic realm="IPCamera"'},
            )
        return _text_response("<ok/>")

    client = _build_client(handler)
    try:
        body = await client.get_text("/path")
        assert body == "<ok/>"
        # Three requests: initial digest attempt (401) + basic retry (ok).
        assert len(calls) == 2
        # Second request: Authorization header is Basic.
        assert calls[1]["authorization"].startswith("Basic ")
    finally:
        await client._client.aclose()


@pytest.mark.asyncio
async def test_get_text_does_not_retry_basic_when_digest_offered():
    """If the device offers Digest and still returns 401, the
    credentials are wrong. The client MUST NOT retry with Basic —
    that would just burn another failed login toward Hikvision's
    account-lockout policy.
    """
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(dict(request.headers))
        # Always 401 with Digest challenge — credentials rejected.
        return httpx.Response(
            401,
            headers={
                "WWW-Authenticate": (
                    'Digest realm="IPCamera", nonce="abc", qop="auth"'
                ),
            },
        )

    client = _build_client(handler)
    try:
        with pytest.raises(ISAPIAuthError) as exc:
            await client.get_text("/path")
        assert exc.value.status_code == 401
        # Only two HTTP calls: initial attempt + one digest retry.
        # No basic retry even though basic would also fail.
        assert len(calls) == 2
    finally:
        await client._client.aclose()


@pytest.mark.asyncio
async def test_get_text_auth_switch_persists_across_requests():
    """Once the client switches to Basic, subsequent calls skip the
    initial unauthenticated attempt (they're already Basic)."""
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(dict(request.headers))
        return _text_response("<ok/>")

    # Build client and manually switch its auth to Basic (simulating
    # the post-fallback state).
    client = _build_client(handler)
    try:
        client._auth = client._basic_auth
        await client.get_text("/first")
        await client.get_text("/second")
        # Two requests, both sent with Basic auth (no 401 retry needed).
        assert len(calls) == 2
        assert calls[0]["authorization"].startswith("Basic ")
        assert calls[1]["authorization"].startswith("Basic ")
    finally:
        await client._client.aclose()


# ---- error conversion ----


@pytest.mark.asyncio
async def test_500_raises_isapi_error():
    """Server-side errors surface as ISAPIError (not generic Exception)."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            500, content=b"Internal Server Error",
            headers={"Content-Type": "text/plain"},
        )

    client = _build_client(handler)
    try:
        with pytest.raises(ISAPIError) as exc:
            await client.get_text("/path")
        assert exc.value.status_code == 500
    finally:
        await client._client.aclose()


@pytest.mark.asyncio
async def test_404_raises_isapi_error():
    """404 = NOT found, not auth = ISAPIError."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, content=b"not found")

    client = _build_client(handler)
    try:
        with pytest.raises(ISAPIError) as exc:
            await client.get_text("/path")
        assert exc.value.status_code == 404
    finally:
        await client._client.aclose()


@pytest.mark.asyncio
async def test_network_error_raises_isapi_connection_error():
    """Transport-layer failures → ISAPIConnectionError."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("simulated connection refused")

    client = _build_client(handler)
    try:
        with pytest.raises(ISAPIConnectionError) as exc:
            await client.get_text("/path")
        assert "connection refused" in str(exc.value).lower() or \
            "simulated" in str(exc.value).lower()
    finally:
        await client._client.aclose()


@pytest.mark.asyncio
async def test_timeout_raises_isapi_connection_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("simulated read timeout")

    client = _build_client(handler)
    try:
        with pytest.raises(ISAPIConnectionError):
            await client.get_text("/path")
    finally:
        await client._client.aclose()


# ---- get_xml parsing ----


@pytest.mark.asyncio
async def test_get_xml_parses_xml_response():

    def handler(request: httpx.Request) -> httpx.Response:
        return _xml_response("<DeviceInfo><model>DS-2CD2</model></DeviceInfo>")

    client = _build_client(handler)
    try:
        root = await client.get_xml("/path")
        text = root.findtext("model")
        assert text == "DS-2CD2"
    finally:
        await client._client.aclose()


@pytest.mark.asyncio
async def test_get_xml_raises_isapi_error_on_invalid_xml():

    def handler(request: httpx.Request) -> httpx.Response:
        return _text_response("not valid xml <missing close")

    client = _build_client(handler)
    try:
        with pytest.raises(ISAPIError) as exc:
            await client.get_xml("/path")
        assert "invalid XML" in str(exc.value)
    finally:
        await client._client.aclose()


# ---- get_bytes (camera image path) ----


@pytest.mark.asyncio
async def test_get_bytes_returns_raw_bytes():
    image = b"\xff\xd8\xff\xe0\x00\x10JFIF...jpeg..."

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=image,
            headers={"Content-Type": "image/jpeg"},
        )

    client = _build_client(handler)
    try:
        body = await client.get_bytes("/picture")
        assert body == image
    finally:
        await client._client.aclose()


# ---- URL construction ----


def test_base_url_uses_http_when_use_https_false():
    c = ISAPIClient("10.18.176.10", "u", "p", port=80, use_https=False)
    assert c.base_url == "http://10.18.176.10:80"


def test_base_url_uses_https_when_use_https_true():
    c = ISAPIClient("10.18.176.10", "u", "p", port=443, use_https=True)
    assert c.base_url == "https://10.18.176.10:443"


def test_base_url_includes_port_even_on_default_ports():
    """v0.6.11: URL always includes :port (no implicit port inference).

    Hikvision firmware on a non-standard port would silently break
    if we relied on httpx's default-port behavior, so we always
    pass the port explicitly.
    """
    c80 = ISAPIClient("host", "u", "p", port=80)
    assert c80.base_url == "http://host:80"
    c443 = ISAPIClient("host", "u", "p", port=443)
    assert c443.base_url == "http://host:443"


# ---- coordinator XML parsers (unchanged from previous releases) ----


def test_parse_device_info_extracts_fields():
    xml = """<DeviceInfo>
        <deviceName>Front Door</deviceName>
        <deviceID>abc123</deviceID>
        <model>DS-2CD2143G2-I</model>
        <serialNumber>SN-9876</serialNumber>
        <firmwareVersion>V5.7.10 build 240120</firmwareVersion>
        <firmwareReleasedDate>build 240120</firmwareReleasedDate>
        <deviceType>IPCamera</deviceType>
        <macAddress>00:11:22:33:44:55</macAddress>
    </DeviceInfo>"""
    info = _parse_device_info(ET.fromstring(xml))
    assert info["model"] == "DS-2CD2143G2-I"
    assert info["serialNumber"] == "SN-9876"
    assert info["firmwareVersion"] == "V5.7.10 build 240120"
    assert info["macAddress"] == "00:11:22:33:44:55"
    assert info["manufacturer"] == "Hikvision"


def test_parse_device_info_handles_missing_fields():
    info = _parse_device_info(ET.fromstring("<DeviceInfo/>"))
    assert info["model"] == ""
    assert info["serialNumber"] == ""


def test_parse_system_status_extracts_fields():
    xml = """<SystemStatus>
        <deviceStatus>OK</deviceStatus>
        <CPUUsage>27</CPUUsage>
        <memoryUsage>35</memoryUsage>
        <uptime>12345</uptime>
    </SystemStatus>"""
    status = _parse_system_status(ET.fromstring(xml))
    assert status["deviceStatus"] == "OK"
    assert status["cpuUtilization"] == "27"
    assert status["memoryUsage"] == "35"
    assert status["uptime"] == "12345"


def test_parse_channels_extracts_id_name_online_recording():
    xml = """<InputProxyChannelList>
        <InputProxyChannel>
            <id>1</id>
            <name>Front Door</name>
            <online>true</online>
            <recordStatus>recording</recordStatus>
        </InputProxyChannel>
        <InputProxyChannel>
            <id>2</id>
            <name>Side Gate</name>
            <online>false</online>
            <recordStatus>idle</recordStatus>
        </InputProxyChannel>
    </InputProxyChannelList>"""
    channels = _parse_channels(ET.fromstring(xml))
    assert len(channels) == 2
    assert channels[0]["online"] is True
    assert channels[0]["recording"] is True
    assert channels[1]["online"] is False
    assert channels[1]["recording"] is False


def test_parse_channels_handles_empty_root():
    assert _parse_channels(ET.fromstring("<InputProxyChannelList/>")) == []
    assert _parse_channels(None) == []


def test_parse_storage_extracts_capacity_and_status():
    xml = """<Storage>
        <totalCapacity>2000000</totalCapacity>
        <usedCapacity>1234567</usedCapacity>
        <freeCapacity>765433</freeCapacity>
        <status>normal</status>
    </Storage>"""
    storage = _parse_storage(ET.fromstring(xml))
    assert storage["total_mb"] == 2000000
    assert storage["used_mb"] == 1234567
    assert storage["free_mb"] == 765433
    assert storage["status"] == "normal"


def test_parse_storage_handles_empty_root():
    storage = _parse_storage(None)
    assert storage == {
        "total_mb": None, "used_mb": None,
        "free_mb": None, "status": "unknown",
    }


def test_parse_network_interfaces_extracts_ip_mask_gateway():
    xml = """<NetworkInterfaceList>
        <NetworkInterface>
            <id>1</id>
            <interfaceName>LAN1</interfaceName>
            <IPAddress>192.168.1.10</IPAddress>
            <subnetMask>255.255.255.0</subnetMask>
            <DefaultGateway>192.168.1.1</DefaultGateway>
            <MTU>1500</MTU>
            <MACAddress>00:11:22:33:44:55</MACAddress>
        </NetworkInterface>
    </NetworkInterfaceList>"""
    ifs = _parse_network_interfaces(ET.fromstring(xml))
    assert len(ifs) == 1
    assert ifs[0]["ip_address"] == "192.168.1.10"


def test_parse_streaming_channels_extracts_bitrate():
    xml = """<StreamingChannelList>
        <StreamingChannel>
            <id>1</id>
            <videoAverageBitrate>2048</videoAverageBitrate>
        </StreamingChannel>
        <StreamingChannel>
            <id>2</id>
            <maxBitrate>4096</maxBitrate>
        </StreamingChannel>
    </StreamingChannelList>"""
    bitrates = _parse_streaming_channels(ET.fromstring(xml))
    assert bitrates == {"1": 2048, "2": 4096}


def test_parse_streaming_channels_handles_empty_root():
    assert _parse_streaming_channels(ET.fromstring("<StreamingChannelList/>")) == {}
    assert _parse_streaming_channels(None) == {}


def test_parse_channel_status_online_recording_motion():
    xml = """<InputProxyChannelStatus>
        <online>true</online>
        <recordStatus>recording</recordStatus>
        <motionDetection>false</motionDetection>
    </InputProxyChannelStatus>"""
    status = _parse_channel_status(ET.fromstring(xml))
    assert status["online"] is True
    assert status["recording"] is True
    assert status["motion_detected"] is False


def test_normalize_device_type_ipcamera_variants():
    for raw in ("IPCamera", "ipcamera", "IPC", "IpC", " ipc "):
        assert normalize_device_type(raw) == "ipcamera"


def test_normalize_device_type_nvr_variants():
    for raw in ("NetworkVideoRecorder", "nvr", "NVR"):
        assert normalize_device_type(raw) == "networkvideorecorder"


def test_normalize_device_type_dvr_variants():
    assert normalize_device_type("DVR") == "dvr"
    assert normalize_device_type("DigitalVideoRecorder") == "dvr"


def test_normalize_device_type_defaults_to_ipcamera():
    assert normalize_device_type("") == "ipcamera"
    assert normalize_device_type("UnknownType") == "ipcamera"
    assert normalize_device_type(None) == "ipcamera"


def test_parse_system_status_nested_schema():
    xml = """<DeviceStatus version="2.0">
        <currentDeviceTime>2026-09-24T16:42:03+08:00</currentDeviceTime>
        <deviceUpTime>92914</deviceUpTime>
        <CPUList>
            <CPU>
                <cpuDescription>ARM926EJ-Sid(wb)</cpuDescription>
                <cpuUtilization>16</cpuUtilization>
            </CPU>
        </CPUList>
        <MemoryList>
            <Memory>
                <memoryDescription>DDR Memory</memoryDescription>
                <memoryUsage>99</memoryUsage>
                <memoryAvailable>9696</memoryAvailable>
            </Memory>
        </MemoryList>
        <totalRebootCount>40</totalRebootCount>
    </DeviceStatus>"""
    status = _parse_system_status(ET.fromstring(xml))
    assert status["cpuUtilization"] == "16"
    assert status["memoryUsage"] == "99"
    assert status["uptime"] == "92914"
    assert status["rebootCount"] == "40"


def test_parse_channel_status_extended_includes_health_fields():
    xml = """<InputProxyChannelStatus>
        <online>true</online>
        <recordStatus>recording</recordStatus>
        <motionDetection>false</motionDetection>
        <deviceUpTime>594666</deviceUpTime>
        <totalRebootCount>40</totalRebootCount>
        <SDCardStatusInfo><videoRewritingTimes>3581</videoRewritingTimes></SDCardStatusInfo>
        <Camera><cameraRunTotalTime>594666</cameraRunTotalTime></Camera>
        <DomeInfo>
            <domeRunTotalTime>594666</domeRunTotalTime>
            <heatState>0</heatState>
            <fanState>1</fanState>
            <runtimeOverPositiveforty>346495</runtimeOverPositiveforty>
        </DomeInfo>
    </InputProxyChannelStatus>"""
    status = _parse_channel_status_extended(ET.fromstring(xml))
    assert status["online"] is True
    assert status["recording"] is True
    assert status["uptime"] == "594666"
    assert status["reboot_count"] == "40"
    assert status["sd_card_writes"] == "3581"
    assert status["camera_run_total_time"] == "594666"
    assert status["dome_runtime_over_40"] == "346495"
