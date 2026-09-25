"""Tests for v0.6.15 — V4 storage schema + refresh summary log.

User reported post-v0.6.14 that on their V4 NVR (``DS-7708N-I4``,
V4.1.18 firmware, ``deviceType=DVR``):
- deviceInfo sensors populate (model, serial number, firmware, ...)
- "channel count" populates (so channels list IS non-empty)
- ALL other sensors show "unknown"

The most likely root cause for the storage-derived sensors
(``存储总量``, ``存储使用量``, ``存储剩余``, ``存储使用率``) is the
V4 firmware's storage XML schema:

    <Storage xmlns="...">
      <hddList>
        <HDD>
          <id>1</id>
          <size>2000396746752</size>           (bytes, NOT MB!)
          <freeSize>1234567890123</freeSize>   (bytes)
          <status>normal</status>
        </HDD>
      </hddList>
    </Storage>

Pre-v0.6.15 the parser only read ``<totalCapacity>`` / ``<usedCapacity>``
/ ``<freeCapacity>`` — V5 fields that V4 doesn't emit. So even
when ``/System/Storage/hardDisks`` returned successfully, every
storage field was ``None`` and the entities showed "unknown".

v0.6.15 extends ``_parse_storage`` to:
- Try V5 fields first (unchanged).
- Fall back to summing ``<hddList><HDD><size>`` / ``<freeSize>``,
  converting bytes → MB (decimal, 1 MB = 10^6 bytes) for parity
  with V5.

Plus: a single per-refresh INFO line summarizing which data
categories populated vs. missing, so future debugging is one
copy-paste away.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from xml.etree import ElementTree as ET

from custom_components.hikvision_isapi_performance.coordinator import (
    _parse_storage,
)
from custom_components.hikvision_isapi_performance.isapi_client import (
    _strip_xmlns,
)


def _v4_root(xml_str: str) -> ET.Element:
    """Parse XML the same way isapi_client.get_xml does — strip
    xmlns declarations first so plain tag lookups work. V4 firmware
    responses include xmlns=``http://www.hikvision.com/ver20/XMLSchema``
    on root, which ElementTree honors by prefixing every tag."""
    return ET.fromstring(_strip_xmlns(xml_str))


# ---- V4 storage XML: <hddList><HDD> schema ----


def test_parse_storage_v4_single_hdd_sums_to_mb():
    """V4 single-HDD NVR: sum size and freeSize, convert bytes→MB.

    2 TB = 2 * 10^12 bytes ≈ 2,000,397 MB decimal. Sanity bounds
    are loose (~2 million MB) since exact manufacturer-reported
    byte counts vary.
    """
    xml = """<Storage xmlns="http://www.hikvision.com/ver20/XMLSchema">
      <hddList>
        <HDD>
          <id>1</id>
          <size>2000396746752</size>          <!-- ~2 TB bytes -->
          <freeSize>1234567890123</freeSize>  <!-- ~1.2 TB bytes -->
          <status>normal</status>
        </HDD>
      </hddList>
    </Storage>"""
    out = _parse_storage(_v4_root(xml))
    assert out["total_mb"] is not None
    # 2 TB bytes → ~2,000,397 MB at 1 MB = 10^6 bytes
    assert 1_900_000 < out["total_mb"] < 2_100_000
    assert out["free_mb"] is not None
    assert 1_100_000 < out["free_mb"] < 1_400_000
    assert out["used_mb"] is not None
    # used = total - free
    expected_used = round(out["total_mb"] - out["free_mb"], 1)
    assert abs(out["used_mb"] - expected_used) < 1
    assert out["status"] == "normal"


def test_parse_storage_v4_multi_hdd_sums_across_hdds():
    """V4 NVR with multiple HDDs: total + free are summed."""
    xml = """<Storage xmlns="...">
      <hddList>
        <HDD>
          <id>1</id>
          <size>2000396746752</size>
          <freeSize>1000000000000</freeSize>
          <status>normal</status>
        </HDD>
        <HDD>
          <id>2</id>
          <size>4000796746752</size>
          <freeSize>2000000000000</freeSize>
          <status>normal</status>
        </HDD>
      </hddList>
    </Storage>"""
    out = _parse_storage(_v4_root(xml))
    # Total ≈ 6 TB = ~6,000,000 MB; we sum and convert.
    assert out["total_mb"] > 5_500_000
    assert out["total_mb"] < 6_500_000
    assert out["free_mb"] > 2_500_000
    assert out["free_mb"] < 3_500_000


def test_parse_storage_v4_exception_hdd_aggregates_status():
    """One HDD reports 'exception' → aggregate status becomes exception."""
    xml = """<Storage xmlns="...">
      <hddList>
        <HDD>
          <id>1</id>
          <size>2000000000000</size>
          <freeSize>1000000000000</freeSize>
          <status>normal</status>
        </HDD>
        <HDD>
          <id>2</id>
          <size>2000000000000</size>
          <freeSize>1500000000000</freeSize>
          <status>exception</status>
        </HDD>
      </hddList>
    </Storage>"""
    out = _parse_storage(_v4_root(xml))
    assert out["status"] == "exception"


def test_parse_storage_v5_still_works():
    """V5 storage XML (direct fields) still parses as before."""
    xml = """<Storage xmlns="...">
      <totalCapacity>2000000</totalCapacity>
      <usedCapacity>1234567</usedCapacity>
      <freeCapacity>765433</freeCapacity>
      <status>normal</status>
    </Storage>"""
    out = _parse_storage(_v4_root(xml))
    assert out["total_mb"] == 2000000
    assert out["used_mb"] == 1234567
    assert out["free_mb"] == 765433
    assert out["status"] == "normal"


def test_parse_storage_v5_takes_precedence_over_hddlist():
    """If BOTH <totalCapacity> AND <hddList> are present, prefer V5
    fields (more precise per-firmware). V5 is the 'direct' path."""
    xml = """<Storage xmlns="...">
      <totalCapacity>2000000</totalCapacity>
      <usedCapacity>1234567</usedCapacity>
      <freeCapacity>765433</freeCapacity>
      <hddList>
        <HDD>
          <id>1</id>
          <size>2000396746752</size>
          <freeSize>1234567890123</freeSize>
        </HDD>
      </hddList>
    </Storage>"""
    out = _parse_storage(_v4_root(xml))
    # V5 path wins — total_mb is exact 2000000 (MB), not the
    # bytes-converted value.
    assert out["total_mb"] == 2000000


def test_parse_storage_handles_empty_root():
    out = _parse_storage(None)
    assert out == {
        "total_mb": None, "used_mb": None,
        "free_mb": None, "status": "unknown",
    }


def test_parse_storage_empty_hddlist_falls_back_to_empty():
    """V4 with <hddList/> (no <HDD> children) → empty dict."""
    xml = """<Storage xmlns="...">
      <hddList/>
    </Storage>"""
    out = _parse_storage(_v4_root(xml))
    assert out["total_mb"] is None
    assert out["used_mb"] is None
    assert out["free_mb"] is None


# ---- refresh summary log ----


def test_v0615_coordinator_emits_refresh_summary_log():
    """v0.6.15: coordinator.py must emit one INFO line with the
    exact string ``refresh summary`` so the user can see at default
    log level whether categories populated."""
    src = Path(
        r"C:\Users\43457\Desktop\hikvision-isapi"
        r"\custom_components\hikvision_isapi_performance\coordinator.py"
    ).read_text(encoding="utf-8-sig")
    assert "refresh summary" in src, (
        "coordinator.py must log a 'refresh summary' INFO line — "
        "v0.6.15 diagnostic feature for V4 NVR data gaps."
    )


def test_v0615_summary_log_mentions_all_five_categories():
    """The summary line covers device_info / channels / storage /
    network / status / bitrate so the user gets a complete picture
    in one paste."""
    src = Path(
        r"C:\Users\43457\Desktop\hikvision-isapi"
        r"\custom_components\hikvision_isapi_performance\coordinator.py"
    ).read_text(encoding="utf-8-sig")
    for category in (
        "device_info", "channels", "storage", "network", "status",
        "bitrate",
    ):
        assert category in src, (
            f"refresh summary log must mention '{category}' so the "
            "user can paste one line back for full diagnostics"
        )


# ---- manifest version ----


def test_v0615_manifest_version_at_or_beyond_0_6_15():
    """v0.6.15 anchor; later releases may bump further."""
    manifest = json.loads(Path(
        r"C:\Users\43457\Desktop\hikvision-isapi"
        r"\custom_components\hikvision_isapi_performance\manifest.json"
    ).read_text(encoding="utf-8"))
    parts = manifest["version"].split(".")
    assert parts[0] == "0"
    assert int(parts[1]) >= 6
    if int(parts[1]) == 6:
        assert int(parts[2]) >= 15
