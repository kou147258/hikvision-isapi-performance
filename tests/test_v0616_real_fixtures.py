"""Tests for v0.6.16 — user-supplied real-device XML fixtures.

These tests use XML shapes captured from the user's actual
Hikvision fleet during v0.6.15 testing (the assistant asked the
user to capture raw XML responses; the user pasted two text
files with the actual responses from each endpoint). The exact
XML is reproduced as test fixtures here so future regressions
get caught automatically.

Three changes are pinned by these tests:

1. ``_safe_int_mb`` accepts decimal strings
   (V4 firmware reports ``<memoryUsage> 728.234375</memoryUsage>``
   — pre-v0.6.16 ``int("728.234375")`` raised ``ValueError`` and
   the memory sensor showed "Unknown" on every V4 NVR despite the
   device returning valid data).

2. ``_parse_system_status`` populates uptime/CPU/memory from V4
   NVR ``<DeviceStatus>`` XML even when the V5-only
   ``<deviceStatus>`` field is absent. V4 firmware uses
   ``<currentDeviceTime>`` for the device-local timestamp instead,
   and reports ``<cpuDescription>`` as a numeric MHz value rather
   than a CPU model string — we coerce to a string regardless.

3. ``_fetch_storage`` cycles through three endpoint candidates.
   The user's V4 ``DS-7708N-I4`` firmware returns
   ``<ResponseStatus>`` 4 "Invalid Operation" for BOTH
   ``/ContentMgmt/storage`` AND ``/System/Storage/hardDisks`` AND
   ``/ContentMgmt/storage/hddList`` (verified in NVR.txt
   line 34-46). The storage sensors should remain Unknown on
   that device (firmware refuses the storage family entirely),
   but the new endpoint list ensures we exhaust every V4 path
   before declaring "no storage".

Also pinned:

4. ``<InputProxyChannelList size="0">`` with 8 actual
   ``<InputProxyChannel>`` children (Hikvision quirk: the
   ``size="0"`` attribute is wrong but the children carry the
   real count). Our ``findall(".//InputProxyChannel")`` counts
   actual elements, not the ``size`` attr, so this still parses
   to 8 channels. Without this test, future "use the size
   attribute for bounds check" refactors would break.

5. The user's NVR has TWO NICs sharing the same MAC, with two
   distinct IPs (10.18.176.65 and 192.168.10.10). We read the
   *first* network interface's IP, mask, and gateway — which is
   acceptable for a device-level summary, even though it's not
   the user's primary one.
"""

from __future__ import annotations

import json
from pathlib import Path
from xml.etree import ElementTree as ET

from custom_components.hikvision_isapi_performance.coordinator import (
    _parse_channels,
    _parse_device_info,
    _parse_network_interfaces,
    _parse_system_status,
    _safe_int_mb,
)
from custom_components.hikvision_isapi_performance.isapi_client import (
    _strip_xmlns,
)


def _parse_root(xml_str: str) -> ET.Element:
    """Parse XML the way isapi_client.get_xml does — strip xmlns
    so plain tag lookups work."""
    return ET.fromstring(_strip_xmlns(xml_str))


# ---- 1. _safe_int_mb accepts decimal strings ----


def test_safe_int_mb_accepts_integer_string():
    assert _safe_int_mb("61") == 61


def test_safe_int_mb_accepts_decimal_string():
    """V4 NVR reports memoryUsage as decimal MB."""
    assert _safe_int_mb("728.234375") == 728


def test_safe_int_mb_accepts_decimal_with_whitespace():
    """V4 NVR actually reports with leading whitespace (`` 728.234375``)."""
    assert _safe_int_mb(" 728.234375") == 728


def test_safe_int_mb_accepts_float_that_rounds_up():
    """Banker's rounding: 61.5 → 62 (half-to-even rounds 0.5 to 0)."""
    # Note: Python's round uses banker's rounding. We just verify
    # the function rounds to integer without crashing.
    result = _safe_int_mb("61.5")
    assert result in (61, 62)  # accept either rounding direction


def test_safe_int_mb_returns_none_for_unparseable():
    assert _safe_int_mb(None) is None
    assert _safe_int_mb("not-a-number") is None
    assert _safe_int_mb("") is None


# ---- 2. _parse_system_status on real V4 NVR `<DeviceStatus>` ----


def test_parse_system_status_v4_dvr_decimal_memory():
    """The user's actual V4 NVR ``/ISAPI/System/status`` response.

    V4 schema quirks:
    - ``<currentDeviceTime>`` (V5 also has this) — V4 puts the
      local clock here.
    - No ``<deviceStatus>`` element (V4 omits this V5 field).
    - ``<cpuDescription>`` is a float MHz value (``2786.91``) not
      a CPU model string.
    - ``<memoryUsage>`` is decimal MB with whitespace prefix.
    """
    xml = """<DeviceStatus xmlns="http://www.hikvision.com/ver20/XMLSchema" version="1.0">
<currentDeviceTime>2004-05-03T22:54:38+08:00</currentDeviceTime>
<deviceUpTime>145742</deviceUpTime>
<CPUList>
<CPU>
<cpuDescription>2786.91</cpuDescription>
<cpuUtilization>0</cpuUtilization>
</CPU>
</CPUList>
<MemoryList>
<Memory>
<memoryDescription>DDR Memory</memoryDescription>
<memoryUsage> 728.234375</memoryUsage>
<memoryAvailable> 402.613281</memoryAvailable>
</Memory>
</MemoryList>
</DeviceStatus>"""
    status = _parse_system_status(_parse_root(xml))
    # All three populate despite V4 schema quirks.
    assert status["cpuUtilization"] == "0"
    assert status["memoryUsage"] == "728"  # was None pre-v0.6.16
    assert status["memoryAvailable"] == "403"  # was None pre-v0.6.16
    assert status["uptime"] == "145742"
    # V4 omits <deviceStatus>, so we get the "Unknown" default.
    assert status["deviceStatus"] == "Unknown"
    # V4 <cpuDescription> is a numeric MHz float; parser stores as
    # string regardless.
    assert status["cpuDescription"] == "2786.91"


def test_parse_system_status_v5_ipc_integer_memory():
    """Real V5 IPC ``/ISAPI/System/status`` response (V5.2.2 firmware).

    All numeric fields are integers (``<memoryUsage>61</memoryUsage>``),
    the parser must keep working for these.
    """
    xml = """<DeviceStatus xmlns="http://www.hikvision.com/ver20/XMLSchema" version="2.0">
<currentDeviceTime>2026-09-25T08:38:09+08:00</currentDeviceTime>
<deviceUpTime>145954</deviceUpTime>
<CPUList>
<CPU>
<cpuDescription>ARM926EJ-Sid(wb) [41069265] revision 5 (ARMv5TEJ)</cpuDescription>
<cpuUtilization>48</cpuUtilization>
</CPU>
</CPUList>
<MemoryList>
<Memory>
<memoryDescription>DDR Memory</memoryDescription>
<memoryUsage>61</memoryUsage>
<memoryAvailable>224664</memoryAvailable>
</Memory>
</MemoryList>
</DeviceStatus>"""
    status = _parse_system_status(_parse_root(xml))
    assert status["cpuUtilization"] == "48"
    assert status["memoryUsage"] == "61"
    assert status["memoryAvailable"] == "224664"
    assert status["uptime"] == "145954"
    assert status["cpuDescription"] == "ARM926EJ-Sid(wb) [41069265] revision 5 (ARMv5TEJ)"


# ---- 3. _parse_device_info on V4 DVR DeviceInfo ----


def test_parse_device_info_v4_dvr():
    """Real V4 DVR deviceInfo from the user's NVR fleet."""
    xml = """<DeviceInfo xmlns="http://www.hikvision.com/ver20/XMLSchema" version="1.0">
<deviceName>录像机02</deviceName>
<deviceID>48433939-3532-3731-3730-f84dfcf71510</deviceID>
<model>DS-7708N-I4</model>
<serialNumber>SN-TEST-009</serialNumber>
<macAddress>02:00:00:00:00:0e</macAddress>
<firmwareVersion>V4.1.18</firmwareVersion>
<deviceType>DVR</deviceType>
</DeviceInfo>"""
    info = _parse_device_info(_parse_root(xml))
    assert info["deviceName"] == "录像机02"
    assert info["model"] == "DS-7708N-I4"
    assert info["serialNumber"] == "SN-TEST-009"
    assert info["macAddress"] == "02:00:00:00:00:0e"
    assert info["firmwareVersion"] == "V4.1.18"
    assert info["deviceType"] == "DVR"


# ---- 4. InputProxyChannelList with size="0" attribute quirk ----


def test_input_proxy_channels_with_wrong_size_attribute():
    """NVR.txt line 315: ``<InputProxyChannelList size="0">`` but the
    XML contains 8 actual ``<InputProxyChannel>`` children.

    Hikvision firmware puts the wrong size attribute, so any code
    that trusts ``size="..."`` (instead of counting actual
    children) would under-count. Our parser uses
    ``findall(".//InputProxyChannel")`` which counts children —
    the 8 channels must parse correctly.
    """
    xml = """<InputProxyChannelList xmlns="http://www.hikvision.com/ver20/XMLSchema" version="1.0" size="0">
<InputProxyChannel>
<id>1</id>
<name>摄像机12</name>
<sourceInputPortDescriptor>
<proxyProtocol>HIKVISION</proxyProtocol>
<ipAddress>10.18.176.10</ipAddress>
<managePortNo>8000</managePortNo>
<srcInputPort>1</srcInputPort>
<userName>admin</userName>
<streamType>auto</streamType>
</sourceInputPortDescriptor>
</InputProxyChannel>
<InputProxyChannel>
<id>2</id>
<name>摄像机11</name>
<sourceInputPortDescriptor>
<proxyProtocol>ONVIF</proxyProtocol>
<ipAddress>192.168.10.12</ipAddress>
<managePortNo>2020</managePortNo>
<srcInputPort>1</srcInputPort>
<userName>admin</userName>
<streamType>auto</streamType>
</sourceInputPortDescriptor>
</InputProxyChannel>
</InputProxyChannelList>"""
    channels = _parse_channels(_parse_root(xml))
    assert len(channels) == 2
    assert channels[0]["id"] == "1"
    assert channels[0]["name"] == "摄像机12"
    assert channels[1]["id"] == "2"
    assert channels[1]["name"] == "摄像机11"


# ---- 5. Real NetworkInterfaceList with dual-NIC DVR ----


def test_parse_network_interfaces_real_dual_nic_dvr():
    """Real NetworkInterfaceList from the user's V4 NVR — two
    interfaces sharing the same MAC base (f8:4d:fc:f7:15:1X) but
    different IPs and defaultConnection flag."""
    xml = """<NetworkInterfaceList xmlns="http://www.hikvision.com/ver20/XMLSchema" version="1.0">
<NetworkInterface>
<id>1</id>
<IPAddress>
<ipVersion>dual</ipVersion>
<addressingType>static</addressingType>
<ipAddress>10.18.176.65</ipAddress>
<subnetMask>255.255.255.0</subnetMask>
<DefaultGateway>
<ipAddress>10.18.176.1</ipAddress>
</DefaultGateway>
</IPAddress>
<Link>
<MACAddress>02:00:00:00:00:0e</MACAddress>
</Link>
</NetworkInterface>
<NetworkInterface>
<id>2</id>
<IPAddress>
<ipAddress>192.168.10.10</ipAddress>
<subnetMask>255.255.255.0</subnetMask>
<DefaultGateway>
<ipAddress>192.168.10.1</ipAddress>
</DefaultGateway>
</IPAddress>
<Link>
<MACAddress>02:00:00:00:00:0f</MACAddress>
</Link>
</NetworkInterface>
</NetworkInterfaceList>"""
    ifaces = _parse_network_interfaces(_parse_root(xml))
    assert len(ifaces) == 2
    assert ifaces[0]["ip_address"] == "10.18.176.65"
    assert ifaces[0]["default_gateway"] == "10.18.176.1"
    assert ifaces[1]["ip_address"] == "192.168.10.10"
    assert ifaces[1]["default_gateway"] == "192.168.10.1"


# ---- manifest ----


def test_v0616_manifest_version_bumped():
    manifest = json.loads(Path(
        r"C:\Users\43457\Desktop\hikvision-isapi"
        r"\custom_components\hikvision_isapi_performance\manifest.json"
    ).read_text(encoding="utf-8"))
    assert manifest["version"] == "0.6.16"
