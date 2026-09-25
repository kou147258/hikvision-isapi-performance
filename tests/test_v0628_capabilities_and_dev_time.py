"""Regression tests for v0.6.28.

User feedback after v0.6.27 requested three things:

1. **Add ``/ISAPI/System/capabilities`` probe.** Independent of
   ``deviceType`` string, the capabilities endpoint reports the
   canonical ``<VideoInputChannelNums>`` and
   ``<SupportDeviceType>`` list. Useful for spotting obscure /
   new device types where the ``deviceType`` mapping might
   mis-fire (which would corrupt KB/MB memory-unit detection).

2. **Rename ``device_time_abnormal`` → ``dev_time_abnormal``**.
   The user requested the shorter name. The binary sensor's
   ``translation_key``, unique_id, class name, and helper
   function all rename. Old entities remain in the HA entity
   registry until the user deletes them; the new ones match.

3. **Strengthen the README scan_interval warning.** Move the
   warning to the top of both English and Chinese sections
   so first-time readers see it before the feature list.

Tests:
- ``_parse_capabilities`` extracts ``status_supported``,
  ``video_input_channels``, ``device_types``, ``ethernet_interfaces``
- ``_parse_capabilities`` returns empty dict on None root
- HikvisionISAPIData carries ``system_capabilities`` field
- ``capability_video_input_channels`` sensor reads from
  ``data.system_capabilities``
- The renamed helper ``_dev_time_abnormal`` exists and behaves
  identically to the v0.6.26 ``_device_time_abnormal``
- The renamed class ``HikvisionISAPIDevTimeAbnormalBinarySensor``
  exists and instantiates in async_setup_entry
- All 4 translation files declare ``dev_time_abnormal``
- The legacy ``device_time_abnormal`` key is GONE from all 4
  translations (regression guard — never silently re-add)
- README contains the scan_interval warning in BOTH English
  AND Chinese at the top (before the Features section)
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path


_REPO_ROOT = Path(r"C:\Users\43457\Desktop\hikvision-isapi")
_INTEGRATION_ROOT = (
    _REPO_ROOT / "custom_components" / "hikvision_isapi_performance"
)
_TRANSLATIONS_DIR = _INTEGRATION_ROOT / "translations"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig")


def _parse_xml(xml: str):
    """Strip xmlns and parse XML, returning root Element."""
    import xml.etree.ElementTree as ET
    from custom_components.hikvision_isapi_performance.isapi_client import (
        _strip_xmlns,
    )
    return ET.fromstring(_strip_xmlns(xml))


def _load_module(name: str):
    sys.path.insert(0, str(_INTEGRATION_ROOT.parent))
    from custom_components.hikvision_isapi_performance import (
        binary_sensor,
        coordinator,
    )
    return {
        "binary_sensor": binary_sensor,
        "coordinator": coordinator,
    }


# ---------------------------------------------------------------------------
# Capabilities probe
# ---------------------------------------------------------------------------


def test_v0628_capabilities_status_supported_true():
    """``_parse_capabilities`` reads ``status_supported=True``.

    V5 IPCs (DS-FB2127) return
    ``<SysStatus supported="true" />`` — the new flag must
    propagate through to ``HikvisionISAPIData.system_capabilities``
    so the consumer can mark sensors as unknown instead of 0%
    when status isn't supported.
    """
    coord = _load_module("coordinator")["coordinator"]
    xml = """<DeviceCap>
<SysCap>
<VideoInputChannelNums>1</VideoInputChannelNums>
</SysCap>
<SysStatus supported="true" />
<NetworkCap>
<EthernetNums>1</EthernetNums>
</NetworkCap>
<SupportDeviceType>
<DeviceType>IPCamera</DeviceType>
</SupportDeviceType>
</DeviceCap>"""
    result = coord._parse_capabilities(_parse_xml(xml))
    assert result.get("status_supported") is True, (
        f"v0.6.28: _parse_capabilities must read status_supported "
        f"from <SysStatus supported='true' /> (got "
        f"{result.get('status_supported')!r})."
    )


def test_v0628_capabilities_status_supported_false():
    """``status_supported=False`` is preserved.

    Some V4 NVRs emit ``<SysStatus supported="false" />``. The
    consumer (system_status sensors) can then skip the bogus
    "0%" placeholder.
    """
    coord = _load_module("coordinator")["coordinator"]
    xml = """<DeviceCap>
<SysStatus supported="false" />
</DeviceCap>"""
    result = coord._parse_capabilities(_parse_xml(xml))
    assert result.get("status_supported") is False


def test_v0628_capabilities_video_input_channels_nvr():
    """V4 NVR with 8 channels → ``video_input_channels == 8``."""
    coord = _load_module("coordinator")["coordinator"]
    xml = """<DeviceCap>
<SysCap>
<VideoInputChannelNums>8</VideoInputChannelNums>
</SysCap>
<SysStatus supported="true" />
</DeviceCap>"""
    result = coord._parse_capabilities(_parse_xml(xml))
    assert result.get("video_input_channels") == 8, (
        f"v0.6.28: V4 NVR (8 channels) must populate "
        f"video_input_channels=8 (got "
        f"{result.get('video_input_channels')!r})."
    )


def test_v0628_capabilities_device_types_list():
    """``device_types`` is a list of all ``<DeviceType>`` entries."""
    coord = _load_module("coordinator")["coordinator"]
    xml = """<DeviceCap>
<SupportDeviceType>
<DeviceType>NetworkVideoRecorder</DeviceType>
<DeviceType>DVR</DeviceType>
</SupportDeviceType>
</DeviceCap>"""
    result = coord._parse_capabilities(_parse_xml(xml))
    types = result.get("device_types")
    assert types == ["NetworkVideoRecorder", "DVR"], (
        f"v0.6.28: device_types must list all <DeviceType> "
        f"entries (got {types!r})."
    )


def test_v0628_capabilities_ethernet_interfaces_nvr():
    """Dual-NIC NVR reports ``ethernet_interfaces == 2``."""
    coord = _load_module("coordinator")["coordinator"]
    xml = """<DeviceCap>
<NetworkCap>
<EthernetNums>2</EthernetNums>
</NetworkCap>
</DeviceCap>"""
    result = coord._parse_capabilities(_parse_xml(xml))
    assert result.get("ethernet_interfaces") == 2, (
        f"v0.6.28: dual-NIC NVR must report "
        f"ethernet_interfaces=2 (got "
        f"{result.get('ethernet_interfaces')!r})."
    )


def test_v0628_capabilities_none_root_returns_empty():
    """``_parse_capabilities(None)`` returns empty dict (no crash)."""
    coord = _load_module("coordinator")["coordinator"]
    result = coord._parse_capabilities(None)
    assert result == {}, (
        f"v0.6.28: _parse_capabilities(None) must return empty "
        f"dict (got {result!r}). The coordinator refresh loop "
        f"uses empty dict as the safe default when the endpoint "
        f"isn't reachable."
    )


def test_v0628_capabilities_data_class_has_field():
    """``HikvisionISAPIData`` accepts and stores ``system_capabilities``."""
    coord = _load_module("coordinator")["coordinator"]
    data = coord.HikvisionISAPIData(
        device_info={"deviceType": "IPCamera"},
        system_status={},
        channels=[],
        capabilities={},
        system_capabilities={"status_supported": True, "video_input_channels": 1},
    )
    assert data.system_capabilities == {
        "status_supported": True, "video_input_channels": 1,
    }, (
        "v0.6.28: HikvisionISAPIData must store system_capabilities "
        "verbatim (it powers the capability_video_input_channels "
        "sensor + future status_unknown flag)."
    )


def test_v0628_capabilities_data_class_default_empty():
    """Default for ``system_capabilities`` is empty dict, not None."""
    coord = _load_module("coordinator")["coordinator"]
    data = coord.HikvisionISAPIData(
        device_info={"deviceType": "IPCamera"},
        system_status={},
        channels=[],
        capabilities={},
    )
    assert data.system_capabilities == {}, (
        "v0.6.28: HikvisionISAPIData(..., system_capabilities=None) "
        "must default to {} (None would crash the sensor .get)."
    )


def test_v0628_capability_sensor_registered():
    """``capability_video_input_channels`` is in the SENSORS list.

    Source-load check — we can't import sensor.py directly because
    the conftest stub for SensorEntityDescription doesn't match
    the production @dataclass signature (same problem as v0.6.18
    regression test).
    """
    src = _read(_INTEGRATION_ROOT / "sensor.py")
    assert 'key="capability_video_input_channels"' in src, (
        "v0.6.28: capability_video_input_channels sensor must be "
        "registered in SENSORS."
    )


# ---------------------------------------------------------------------------
# dev_time_abnormal rename
# ---------------------------------------------------------------------------


def test_v0628_dev_time_abnormal_helper_exists():
    """``_dev_time_abnormal`` helper exists (rename from v0.6.26)."""
    bs_path = _INTEGRATION_ROOT / "binary_sensor.py"
    src = _read(bs_path)
    assert "def _dev_time_abnormal(" in src, (
        "v0.6.28: helper function must be renamed to "
        "_dev_time_abnormal (v0.6.26 name was _device_time_abnormal)."
    )
    assert "def _device_time_abnormal(" not in src, (
        "v0.6.28: legacy _device_time_abnormal helper must be "
        "removed — its presence would indicate an incomplete rename."
    )


def test_v0628_dev_time_abnormal_helper_behaviour():
    """The renamed helper behaves identically to the v0.6.26 one."""
    from datetime import datetime, timedelta, timezone

    from custom_components.hikvision_isapi_performance.binary_sensor import (
        _dev_time_abnormal,
    )
    now = datetime(2026, 9, 25, 10, 0, 0, tzinfo=timezone.utc)
    # 2004-05 dead CMOS battery → True
    assert _dev_time_abnormal("2004-05-03T22:54:38+08:00", now) is True
    # 1 hour ago → False
    assert _dev_time_abnormal((now - timedelta(hours=1)).isoformat(), now) is False
    # None / garbage → None
    assert _dev_time_abnormal(None) is None
    assert _dev_time_abnormal("not-a-date") is None


def test_v0628_dev_time_abnormal_binary_sensor_class():
    """``HikvisionISAPIDevTimeAbnormalBinarySensor`` exists and is used."""
    bs_path = _INTEGRATION_ROOT / "binary_sensor.py"
    src = _read(bs_path)
    assert "class HikvisionISAPIDevTimeAbnormalBinarySensor(" in src, (
        "v0.6.28: binary sensor class must be renamed to "
        "HikvisionISAPIDevTimeAbnormalBinarySensor."
    )
    assert "class HikvisionISAPIDeviceTimeAbnormalBinarySensor(" not in src, (
        "v0.6.28: legacy HikvisionISAPIDeviceTimeAbnormalBinarySensor "
        "class must be removed."
    )
    # async_setup_entry must instantiate the new class.
    assert "HikvisionISAPIDevTimeAbnormalBinarySensor(coordinator, entry)" in src, (
        "v0.6.28: async_setup_entry must instantiate the renamed "
        "binary sensor (otherwise the entity never registers)."
    )
    # unique_id must use the new key suffix.
    assert '_dev_time_abnormal' in src, (
        "v0.6.28: binary sensor unique_id must use _dev_time_abnormal "
        "as the suffix so HA matches new entities cleanly."
    )
    assert '_device_time_abnormal' not in src, (
        "v0.6.28: legacy _device_time_abnormal unique_id suffix "
        "must be removed."
    )


# ---------------------------------------------------------------------------
# Translations
# ---------------------------------------------------------------------------


def test_v0628_translations_use_dev_time_abnormal():
    """All 4 translation files declare ``dev_time_abnormal``."""
    expected_names = {
        "en.json": "Device Time Abnormal",
        "zh-CN.json": "设备时间异常",
        "zh.json": "设备时间异常",
        "zh-Hans.json": "设备时间异常",
    }
    for fname, expected_name in expected_names.items():
        path = _TRANSLATIONS_DIR / fname
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        bs = data.get("entity", {}).get("binary_sensor", {})
        key = bs.get("dev_time_abnormal")
        assert key is not None, (
            f"v0.6.28: {fname} must declare binary_sensor.dev_time_abnormal"
        )
        assert key.get("name") == expected_name, (
            f"v0.6.28: {fname} dev_time_abnormal.name should be "
            f"{expected_name!r}, got {key.get('name')!r}"
        )


def test_v0628_legacy_device_time_abnormal_removed():
    """The legacy ``device_time_abnormal`` key is GONE everywhere.

    Regression guard — never silently re-add the old key alongside
    the new one (which would surface two parallel entities in HA
    with confusingly similar names).
    """
    for fname in ("en.json", "zh-CN.json", "zh.json", "zh-Hans.json"):
        path = _TRANSLATIONS_DIR / fname
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        bs = data.get("entity", {}).get("binary_sensor", {})
        assert "device_time_abnormal" not in bs, (
            f"v0.6.28: {fname} must NOT declare "
            f"binary_sensor.device_time_abnormal (legacy key from "
            f"v0.6.26 — fully replaced by dev_time_abnormal)."
        )


def test_v0628_translations_capability_video_input_channels():
    """All 4 translation files declare ``capability_video_input_channels``."""
    expected = {
        "en.json": "Capability: Video Input Channels",
        "zh-CN.json": "能力 — 视频输入通道数",
        "zh.json": "能力 — 视频输入通道数",
        "zh-Hans.json": "能力 — 视频输入通道数",
    }
    for fname, expected_name in expected.items():
        path = _TRANSLATIONS_DIR / fname
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        sensor = data.get("entity", {}).get("sensor", {})
        key = sensor.get("capability_video_input_channels")
        assert key is not None, (
            f"v0.6.28: {fname} must declare "
            f"sensor.capability_video_input_channels"
        )
        assert key.get("name") == expected_name, (
            f"v0.6.28: {fname} capability_video_input_channels.name "
            f"should be {expected_name!r}, got {key.get('name')!r}"
        )


# ---------------------------------------------------------------------------
# README scan_interval warning at the top
# ---------------------------------------------------------------------------


def test_v0628_readme_scan_interval_warning_at_top_english():
    """English README has the scan_interval warning at the TOP
    (above the Features list). v0.6.28 moved it from the
    Configure section to the file header so first-time readers
    see it before reading the feature list.
    """
    readme = _read(_REPO_ROOT / "README.md")
    # The warning text + Features header both present.
    warning_idx = readme.find("scan_interval")
    features_idx = readme.find("## Features")
    assert warning_idx != -1, "v0.6.28: README must mention scan_interval"
    assert features_idx != -1, "v0.6.28: README must still have Features section"
    # The first scan_interval mention must come BEFORE the Features
    # section, otherwise the warning is buried below the feature
    # list and users miss it.
    assert warning_idx < features_idx, (
        f"v0.6.28: scan_interval warning must appear at the top "
        f"of the README (before Features). Found warning at "
        f"offset {warning_idx}, Features at offset {features_idx}."
    )


def test_v0628_readme_scan_interval_warning_at_top_chinese():
    """Chinese README also has the scan_interval warning at the top."""
    readme = _read(_REPO_ROOT / "README.md")
    # Chinese warning uses "轮询间隔" (translate_key phrase) — look
    # for the literal "120" which appears in both languages.
    chinese_start = readme.find("# Hikvision ISAPI Performance（简体中文）")
    chinese_features = readme.find("## 功能", chinese_start)
    chinese_120 = readme.find("120", chinese_start)
    assert chinese_start != -1, "Chinese section must exist"
    assert chinese_features != -1, "Chinese Features section must exist"
    assert chinese_120 != -1 and chinese_120 < chinese_features, (
        f"v0.6.28: Chinese scan_interval warning must appear "
        f"before the 功能 (Features) section. Found 120 at "
        f"offset {chinese_120}, Features at offset {chinese_features}."
    )


# ---------------------------------------------------------------------------
# ISAPI_SYSTEM_CAPABILITIES constant exported
# ---------------------------------------------------------------------------


def test_v0628_capabilities_constant_exported():
    """``ISAPI_SYSTEM_CAPABILITIES`` is exported from const.py."""
    src = _read(_INTEGRATION_ROOT / "const.py")
    assert "ISAPI_SYSTEM_CAPABILITIES" in src, (
        "v0.6.28: const.py must export ISAPI_SYSTEM_CAPABILITIES "
        "constant so coordinator.py can reference it."
    )
    assert '"/ISAPI/System/capabilities"' in src, (
        "v0.6.28: ISAPI_SYSTEM_CAPABILITIES must point to "
        "/ISAPI/System/capabilities."
    )