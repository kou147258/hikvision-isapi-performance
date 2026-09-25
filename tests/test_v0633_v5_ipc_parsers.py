"""Regression tests for v0.6.33.

User feedback after v0.6.32 (IPC 10.18.176.10, DS-2CD-something)
reported three remaining "unknown" / missing data:

1. ``capability_video_input_channels`` sensor still shows "unknown".
   v0.6.32 candidate list was V5-NVR centric
   (``VideoInputChannelNums`` / camelCase / ``InputChannelNum`` /
   ``ChannelNum``). V5 IPC firmware puts the count under
   ``<DeviceCap><SysCap><VideoCap><videoInputPortNums>`` — completely
   different schema. Walk candidates in order; first hit wins.

   Fix: prepend three new candidates to ``_parse_capabilities`` for
   the V5 IPC schema. Order them so they match first for IPCs but
   PascalCase V5 NVR still wins for NVRs (caller's NVR XML has
   both prefixes — VideoCap nested inside DeviceCap typically only
   on IPCs).

2. **Network IP shows blank / "\n"** for V5 IPC. IP is nested inside
   ``<IPAddress><ipAddress>X.X.X.X</ipAddress></IPAddress>`` on the
   user's IPC, but the legacy direct-text ``<IPAddress>X.X.X.X</IPAddress>``
   is what V5 NVR / older firmwares emit. Diagnostic script was
   *not* running the V4-style nested-IP fallback that production
   already had.

   Fix: diagnostic script now mirrors coordinator's ``_ip_field``
   helper that tries nested path first, falls back to direct text.

3. **Storage total/used/free all "unknown"** for V5 IPC. Storage XML
   uses the same shape as V4 NVR but with **lowercase** tag names
   (``<hdd>`` not ``<HDD>``, ``<capacity>`` not ``<size>``,
   ``<freeSpace>`` not ``<freeSize>``) and units already in **MB**
   (not bytes like V4 NVR).

   Fix: ``_parse_storage`` walks V4 (``<HDD><size>`` bytes) and
   V5 IPC (``<hdd><capacity>`` MB) shapes per HDD, auto-detects
   units. Bonus: ``used_mb`` previously returned ``None`` when
   ``free_units == 0`` (full disk) due to falsy ``and``; fixed in
   this version.

Tests covered:
- capabilities with V5 IPC VideoCap/videoInputPortNums (PascalCase
  root ``DeviceCap``)
- storage full-disk case (total > 0, free = 0) yields used = total
- storage V5 IPC lowercase <hdd>/<capacity>/<freeSpace> with units
  in MB (no conversion)
- storage V4 NVR uppercase <HDD>/<size>/<freeSize> in BYTES
  (regression — existing behaviour preserved)
- storage V5 NVR aggregate <totalCapacity>/<usedCapacity>/<freeCapacity>
  in MB (regression — existing behaviour preserved)
- _ip_field nested IP for V5 IPC (the user's 10.18.176.10 schema)
"""

from __future__ import annotations

import sys
from pathlib import Path


_REPO_ROOT = Path(r"C:\Users\43457\Desktop\hikvision-isapi")
_INTEGRATION_ROOT = (
    _REPO_ROOT / "custom_components" / "hikvision_isapi_performance"
)


def _parse_xml(xml: str):
    """Strip xmlns and parse XML, returning root Element."""
    import xml.etree.ElementTree as ET
    sys.path.insert(0, str(_REPO_ROOT))
    from custom_components.hikvision_isapi_performance.isapi_client import (
        _strip_xmlns,
    )
    return ET.fromstring(_strip_xmlns(xml))


def _load_module(name: str):
    sys.path.insert(0, str(_INTEGRATION_ROOT.parent))
    from custom_components.hikvision_isapi_performance import coordinator
    return coordinator


# ---------------------------------------------------------------------------
# capabilities — V5 IPC schema (DeviceCap/SysCap/VideoCap/videoInputPortNums)
# ---------------------------------------------------------------------------


def test_v0633_capabilities_v5ipc_video_cap_video_input_port_nums():
    """V5 IPC puts video input port count under
    ``<DeviceCap><SysCap><VideoCap><videoInputPortNums>``.

    The user's 10.18.176.10 (DS-2CD-something V5.x) returned 0 for
    this field — physically the IPC has 0 analog input ports (cameras
    are IP-attached). The sensor should at minimum *not* be ``None``
    for V5 IPC responses.
    """
    coord = _load_module("coordinator")
    xml = """<DeviceCap>
<SysCap>
<NetworkCap>
<isSupportWireless>false</isSupportWireless>
</NetworkCap>
<VideoCap>
<videoInputPortNums>0</videoInputPortNums>
<videoOutputPortNums>0</videoOutputPortNums>
</VideoCap>
</SysCap>
</DeviceCap>"""
    result = coord._parse_capabilities(_parse_xml(xml))
    # v0.6.33 added this candidate as the FIRST one to try.
    # 0 is a valid integer, so we expect 0 (not None).
    assert result["video_input_channels"] == 0, (
        f"v0.6.33: V5 IPC videoInputPortNums=0 must parse "
        f"(got {result['video_input_channels']!r})."
    )


def test_v0633_capabilities_v5ipc_video_cap_nonzero():
    """A multi-port encoder (DS-67xx-series or similar) would emit
    a positive number here. Make sure we don't crash on that."""
    coord = _load_module("coordinator")
    xml = """<DeviceCap>
<SysCap>
<VideoCap>
<videoInputPortNums>4</videoInputPortNums>
</VideoCap>
</SysCap>
</DeviceCap>"""
    result = coord._parse_capabilities(_parse_xml(xml))
    assert result["video_input_channels"] == 4


def test_v0633_capabilities_v5ipc_video_input_port_nums_pascalcase_root():
    """Some V5 IPC firmwares use PascalCase root container
    ``<DeviceCap><SysCap><VideoCap><VideoInputPortNums>``.
    """
    coord = _load_module("coordinator")
    xml = """<DeviceCap>
<SysCap>
<VideoCap>
<VideoInputPortNums>1</VideoInputPortNums>
</VideoCap>
</SysCap>
</DeviceCap>"""
    result = coord._parse_capabilities(_parse_xml(xml))
    assert result["video_input_channels"] == 1


def test_v0633_capabilities_v5_nvr_still_works():
    """v0.6.32's PascalCase candidate must still beat the new IPC one
    if both happen to be present in the same response — V5 NVR uses
    the standalone ``<VideoInputChannelNums>`` form, not the nested
    ``<VideoCap>`` form.

    Because the new candidates come FIRST in v0.6.33's list, a V5
    IPC response that ALSO has a stray ``<VideoInputChannelNums>``
    element (unlikely but defensive) would yield the IPC value
    instead. For pure V5 NVR responses the new candidates don't
    match anything, so V5 NVR continues to use the original 4
    candidates — this test pins that path.
    """
    coord = _load_module("coordinator")
    xml = """<DeviceCap>
<SysCap>
<VideoInputChannelNums>16</VideoInputChannelNums>
</SysCap>
</DeviceCap>"""
    result = coord._parse_capabilities(_parse_xml(xml))
    assert result["video_input_channels"] == 16, (
        "v0.6.33: V5 NVR PascalCase VideoInputChannelNums must "
        "still parse (no IPC VideoCap element to match first)."
    )


# ---------------------------------------------------------------------------
# storage — full-disk edge case + V5 IPC lowercase schema
# ---------------------------------------------------------------------------


def test_v0633_storage_v5ipc_full_disk_free_zero():
    """V5 IPC full disk: ``<capacity>119290</capacity>``,
    ``<freeSpace>0</freeSpace>``.

    Pre-fix: ``used_mb == None`` because ``total_units and free_units``
    short-circuited on the falsy ``free_units == 0``.
    Post-fix: ``used_mb = total_mb = 119290``.
    """
    coord = _load_module("coordinator")
    xml = """<storage>
<hddList>
<hdd>
<id>1</id>
<capacity>119290</capacity>
<freeSpace>0</freeSpace>
<status>ok</status>
</hdd>
</hddList>
</storage>"""
    result = coord._parse_storage(_parse_xml(xml))
    assert result["total_mb"] == 119290, result
    assert result["free_mb"] == 0, result
    assert result["used_mb"] == 119290, (
        f"v0.6.33: full-disk case (free=0) must yield "
        f"used_mb=total_mb=119290 (got {result['used_mb']!r})."
    )


def test_v0633_storage_v5ipc_lowercase_with_nonfull_disk():
    """V5 IPC with actual free space — straightforward non-full case."""
    coord = _load_module("coordinator")
    xml = """<storage>
<hddList>
<hdd>
<id>1</id>
<capacity>1000</capacity>
<freeSpace>400</freeSpace>
<status>ok</status>
</hdd>
</hddList>
</storage>"""
    result = coord._parse_storage(_parse_xml(xml))
    assert result["total_mb"] == 1000, result
    assert result["free_mb"] == 400, result
    assert result["used_mb"] == 600, result


def test_v0633_storage_v5ipc_lowercase_two_hdds_summed():
    """V5 IPC with two HDDs in the list — sum across both."""
    coord = _load_module("coordinator")
    xml = """<storage>
<hddList>
<hdd>
<id>1</id>
<capacity>1000</capacity>
<freeSpace>200</freeSpace>
</hdd>
<hdd>
<id>2</id>
<capacity>2000</capacity>
<freeSpace>500</freeSpace>
</hdd>
</hddList>
</storage>"""
    result = coord._parse_storage(_parse_xml(xml))
    assert result["total_mb"] == 3000, result
    assert result["free_mb"] == 700, result
    assert result["used_mb"] == 2300, result


def test_v0633_storage_v5ipc_status_error_yields_exception():
    """V5 IPC ``<status>error</status>`` on the HDD should aggregate
    to ``status="exception"``."""
    coord = _load_module("coordinator")
    xml = """<storage>
<hddList>
<hdd>
<id>1</id>
<capacity>1000</capacity>
<freeSpace>500</freeSpace>
<status>error</status>
</hdd>
</hddList>
</storage>"""
    result = coord._parse_storage(_parse_xml(xml))
    assert result["status"] == "exception", result


def test_v0633_storage_v5ipc_status_ok_yields_normal():
    """V5 IPC ``<status>ok</status>`` should aggregate to ``status="normal"``."""
    coord = _load_module("coordinator")
    xml = """<storage>
<hddList>
<hdd>
<id>1</id>
<capacity>1000</capacity>
<freeSpace>500</freeSpace>
<status>ok</status>
</hdd>
</hddList>
</storage>"""
    result = coord._parse_storage(_parse_xml(xml))
    assert result["status"] == "normal", result


# ---------------------------------------------------------------------------
# storage — V4 NVR uppercase regression (must not change)
# ---------------------------------------------------------------------------


def test_v0633_storage_v4nvr_uppercase_bytes():
    """V4 NVR ``<HDD><size>`` in BYTES — must convert to MB.

    2000396746752 bytes / 1e6 = 2000396.746752 → round() → 2000396.7.
    1000198373376 bytes / 1e6 = 1000198.373376 → round() → 1000198.4.
    2000396.746752 - 1000198.373376 = 1000198.373376 → round() →
    1000198.4 (used_mb rounds to the same as free_mb — that's a
    property of the input numbers, not a bug).
    """
    coord = _load_module("coordinator")
    xml = """<Storage>
<hddList>
<HDD>
<id>1</id>
<size>2000396746752</size>
<freeSize>1000198373376</freeSize>
<status>normal</status>
</HDD>
</hddList>
</Storage>"""
    result = coord._parse_storage(_parse_xml(xml))
    assert result["total_mb"] == 2000396.7, result
    assert result["free_mb"] == 1000198.4, result
    assert result["used_mb"] == 1000198.4, result
    assert result["status"] == "normal", result


def test_v0633_storage_v4nvr_no_size_no_capacity_returns_empty():
    """If neither V4 ``<size>`` nor V5 IPC ``<capacity>`` is present,
    we should return the empty dict, not crash."""
    coord = _load_module("coordinator")
    xml = """<Storage>
<hddList>
<HDD>
<id>1</id>
<status>normal</status>
</HDD>
</hddList>
</Storage>"""
    result = coord._parse_storage(_parse_xml(xml))
    assert result["total_mb"] is None, result
    assert result["used_mb"] is None, result
    assert result["free_mb"] is None, result


def test_v0633_storage_v5nvr_aggregate_still_works():
    """V5 NVR aggregate ``<totalCapacity>``/``<usedCapacity>``/``<freeCapacity>``
    in MB at the root — must still work (regression pin)."""
    coord = _load_module("coordinator")
    xml = """<Storage>
<totalCapacity>2000000</totalCapacity>
<usedCapacity>1234567</usedCapacity>
<freeCapacity>765433</freeCapacity>
<status>normal</status>
</Storage>"""
    result = coord._parse_storage(_parse_xml(xml))
    assert result == {
        "total_mb": 2000000,
        "used_mb": 1234567,
        "free_mb": 765433,
        "status": "normal",
    }, result


def test_v0633_storage_v5nvr_aggregate_only_total_yields_partial():
    """V5 NVR with only totalCapacity (used/free absent) — partial fill
    is OK and must not regress to crashing."""
    coord = _load_module("coordinator")
    xml = """<Storage>
<totalCapacity>5000</totalCapacity>
<status>normal</status>
</Storage>"""
    result = coord._parse_storage(_parse_xml(xml))
    assert result["total_mb"] == 5000, result
    assert result["used_mb"] is None, result
    assert result["free_mb"] is None, result
    assert result["status"] == "normal", result


# ---------------------------------------------------------------------------
# network — verify a defensive V5 IPC nested-IP test exists. The
# production parser already handles this via _ip_field; the
# diagnostic script test lives in tests/test_diagnostic_real_device.py
# (added separately).
# ---------------------------------------------------------------------------
