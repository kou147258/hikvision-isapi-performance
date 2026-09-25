"""Regression tests for diagnostic_real_device.py.

The diagnostic script must mirror coordinator.py's parsers — when
the production parser grows new candidate tags, the diagnostic
parser must grow them too, otherwise the script silently reports
"no candidate matched" on devices the integration actually supports.

v0.6.33 specifically: diagnostic script's ``parse_storage`` had the
same full-disk-bug as production AND its ``parse_network_interfaces``
did not have the V4 nested IP fallback that production has had
since v0.6.17. Both fixed together.
"""

from __future__ import annotations

import sys
from pathlib import Path


_REPO_ROOT = Path(r"C:\Users\43457\Desktop\hikvision-isapi")


def _load_diag():
    sys.path.insert(0, str(_REPO_ROOT))
    import diagnostic_real_device as d  # type: ignore
    return d


def _strip(xml: str):
    d = _load_diag()
    # _strip_xmlns already parses the XML internally and returns the
    # root Element. Don't double-parse.
    return d._strip_xmlns(xml)


# ---------------------------------------------------------------------------
# parse_capabilities: must mirror coordinator._parse_capabilities
# ---------------------------------------------------------------------------


def test_diag_capabilities_v5ipc_video_cap_video_input_port_nums():
    """Mirror of coordinator test — V5 IPC must yield a number."""
    d = _load_diag()
    xml = """<DeviceCap>
<SysCap>
<VideoCap>
<videoInputPortNums>0</videoInputPortNums>
</VideoCap>
</SysCap>
</DeviceCap>"""
    result = d.parse_capabilities(_strip(xml))
    assert result["video_input_channels"] == 0, (
        f"v0.6.33: diagnostic must mirror coordinator's V5 IPC "
        f"candidate (got {result['video_input_channels']!r})"
    )


def test_diag_capabilities_v5_nvr_pascalcase():
    """V5 NVR ``<VideoInputChannelNums>`` must still work."""
    d = _load_diag()
    xml = """<DeviceCap>
<SysCap>
<VideoInputChannelNums>8</VideoInputChannelNums>
</SysCap>
</DeviceCap>"""
    result = d.parse_capabilities(_strip(xml))
    assert result["video_input_channels"] == 8


# ---------------------------------------------------------------------------
# parse_storage: must mirror coordinator._parse_storage
# ---------------------------------------------------------------------------


def test_diag_storage_v5ipc_full_disk_free_zero():
    """V5 IPC full-disk case — must yield used_mb = total_mb = 119290."""
    d = _load_diag()
    xml = """<storage>
<hddList>
<hdd>
<capacity>119290</capacity>
<freeSpace>0</freeSpace>
<status>ok</status>
</hdd>
</hddList>
</storage>"""
    result = d.parse_storage(_strip(xml))
    assert result["used_mb"] == 119290, result
    assert result["total_mb"] == 119290, result
    assert result["free_mb"] == 0, result


def test_diag_storage_v4nvr_uppercase_bytes():
    """V4 NVR uppercase ``<HDD><size>`` in BYTES must convert to MB."""
    d = _load_diag()
    xml = """<Storage>
<hddList>
<HDD>
<size>2000396746752</size>
<freeSize>1000198373376</freeSize>
<status>normal</status>
</HDD>
</hddList>
</Storage>"""
    result = d.parse_storage(_strip(xml))
    assert result["total_mb"] == 2000396.7, result
    assert result["free_mb"] == 1000198.4, result
    # Same as coordinator: rounding makes used_mb and free_mb match
    # (1000198.3... rounds to 1000198.4). This is a property of the
    # input numbers, not a parser bug.
    assert result["used_mb"] == 1000198.4, result


def test_diag_storage_v5nvr_aggregate():
    """V5 NVR aggregate must still work."""
    d = _load_diag()
    xml = """<Storage>
<totalCapacity>2000000</totalCapacity>
<usedCapacity>1234567</usedCapacity>
<freeCapacity>765433</freeCapacity>
<status>normal</status>
</Storage>"""
    result = d.parse_storage(_strip(xml))
    assert result == {
        "total_mb": 2000000,
        "used_mb": 1234567,
        "free_mb": 765433,
        "status": "normal",
    }


# ---------------------------------------------------------------------------
# parse_network_interfaces: V5 IPC nested IP must work (the user's
# 10.18.176.10 schema)
# ---------------------------------------------------------------------------


def test_diag_network_v5ipc_nested_ip():
    """V5 IPC emits ``<IPAddress><ipAddress>X.X.X.X</ipAddress></IPAddress>``.

    Prior to v0.6.33 the diagnostic parser read ``<IPAddress>``'s
    text directly, yielding ``"\\n"`` (whitespace between nested
    elements). The fix mirrors coordinator._ip_field: try nested
    path first, fall back to direct text.
    """
    d = _load_diag()
    xml = """<NetworkInterfaceList>
<NetworkInterface>
<id>1</id>
<IPAddress>
<ipVersion>dual</ipVersion>
<ipAddress>10.18.176.10</ipAddress>
<subnetMask>255.255.255.0</subnetMask>
<DefaultGateway>
<ipAddress>10.18.176.1</ipAddress>
</DefaultGateway>
</IPAddress>
<Link>
<MACAddress>08-cc-81-fe-f7-d8</MACAddress>
<MTU>1500</MTU>
</Link>
</NetworkInterface>
</NetworkInterfaceList>"""
    ifaces = d.parse_network_interfaces(_strip(xml))
    assert len(ifaces) == 1, ifaces
    iface = ifaces[0]
    assert iface["ip_address"] == "10.18.176.10", iface
    assert iface["subnet_mask"] == "255.255.255.0", iface
    assert iface["default_gateway"] == "10.18.176.1", iface
    assert iface["mac_address"] == "08-cc-81-fe-f7-d8", iface
    assert iface["mtu"] == 1500, iface


def test_diag_network_v5nvr_direct_ip():
    """V5 NVR / older firmware emits ``<IPAddress>X.X.X.X</IPAddress>``
    as direct text — fall-through path."""
    d = _load_diag()
    xml = """<NetworkInterfaceList>
<NetworkInterface>
<id>1</id>
<IPAddress>192.168.1.10</IPAddress>
<subnetMask>255.255.255.0</subnetMask>
<DefaultGateway>192.168.1.1</DefaultGateway>
<MACAddress>02:00:00:00:00:0d</MACAddress>
</NetworkInterface>
</NetworkInterfaceList>"""
    ifaces = d.parse_network_interfaces(_strip(xml))
    assert len(ifaces) == 1, ifaces
    assert ifaces[0]["ip_address"] == "192.168.1.10", ifaces


# ---------------------------------------------------------------------------
# _strip_xmlns: defensive BOM + leading-whitespace strip
# ---------------------------------------------------------------------------


def test_diag_strip_xmlns_handles_bom():
    """V5 IPC firmwares sometimes prefix the response with a UTF-8
    BOM. ElementTree rejects the wrapped document because the BOM
    pushes the ``<?xml ?>`` declaration off byte 0. The
    v0.6.33 strip tolerates the BOM."""
    d = _load_diag()
    bom = "\ufeff"
    xml = bom + '<?xml version="1.0"?>\n<DeviceCap><VideoInputChannelNums>4</VideoInputChannelNums></DeviceCap>'
    root = d._strip_xmlns(xml)
    # VideoInputChannelNums should still be findable
    n = root.find(".//VideoInputChannelNums")
    assert n is not None and n.text == "4", (
        "v0.6.33: BOM-prefixed XML must parse; "
        f"got n={n!r}"
    )


def test_diag_strip_xmlns_leading_whitespace():
    """Some firmwares prefix the response with whitespace / newlines
    before ``<?xml ?>``. The strip tolerates that."""
    d = _load_diag()
    xml = "\n\n<?xml version=\"1.0\"?>\n<DeviceCap><VideoInputChannelNums>4</VideoInputChannelNums></DeviceCap>"
    root = d._strip_xmlns(xml)
    n = root.find(".//VideoInputChannelNums")
    assert n is not None and n.text == "4", f"got n={n!r}"
