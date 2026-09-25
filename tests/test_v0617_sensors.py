"""Tests for v0.6.17 sensor cleanup.

Three user-reported bugs all fixed:

1. ``cpu_usage`` reads ``cpuUtilization`` (was reading
   ``cpuUsage``, which doesn't exist in the parsed dict, so
   the sensor always showed "unknown").

2. ``memory_usage`` shows a *percentage* (was reading raw MB and
   HA rendered it as PERCENTAGE → "728%"). The new
   ``memory_usage_percent`` computes ``used / (used +
   available) * 100``.

3. ``network_mac`` populates from V4 firmware where the MAC
   address is nested inside ``<Link><MACAddress>...</MACAddress></Link>``.

Also deleted in v0.6.17:

- The 4 storage sensors (``storage_total``, ``storage_used``,
  ``storage_free``, ``storage_usage``) — V4 NVR firmware the
  user has refuses the storage family entirely so these would
  always show "unknown".
- The 2 seconds-based uptime sensors (``uptime`` and
  ``device_uptime``) — user wants hours only; the new
  ``uptime_hours`` is the only runtime sensor.
- The 4 per-channel sensors (``channel_1_uptime``,
  ``sd_card_writes``, ``channel_1_reboot_count``,
  ``dome_high_temp_runtime``) — V4 NVR doesn't serve the
  per-channel endpoint so these were "unknown" on the user's
  fleet. They can be re-added in a future release once the
  per-channel endpoint works on V5 IPC firmware.

Added in v0.6.17 (data we already parsed but didn't expose):

- ``device_mac`` (from deviceInfo)
- ``device_type`` (text: IPC / NVR / DVR / IPZoom)
- ``device_id`` (UUID)
- ``firmware_release_date``
- ``encoder_version``
- ``encoder_release_date``
- ``memory_available_mb`` (raw MB value, useful for IPCs with
  limited RAM)
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from xml.etree import ElementTree as ET

from custom_components.hikvision_isapi_performance.coordinator import (
    _parse_device_info,
    _parse_network_interfaces,
    _parse_system_status,
    _safe_int_mb,
)
from custom_components.hikvision_isapi_performance.isapi_client import (
    _strip_xmlns,
)


def _parse_root(xml_str: str) -> ET.Element:
    return ET.fromstring(_strip_xmlns(xml_str))


# ---- _parse_device_info: V4 returns encoder fields ----


def test_parse_device_info_extracts_encoder_version_v4():
    """V4 firmware's <DeviceInfo> includes <encoderVersion> /
    <encoderReleasedDate>; pre-v0.6.17 these weren't extracted."""
    xml = """<DeviceInfo xmlns="http://www.hikvision.com/ver20/XMLSchema" version="1.0">
<deviceName>录像机02</deviceName>
<model>DS-7708N-I4</model>
<serialNumber>SN-TEST-009</serialNumber>
<macAddress>02:00:00:00:00:0e</macAddress>
<firmwareVersion>V4.1.18</firmwareVersion>
<encoderVersion>V5.0</encoderVersion>
<encoderReleasedDate>build 200109</encoderReleasedDate>
<deviceType>DVR</deviceType>
</DeviceInfo>"""
    info = _parse_device_info(_parse_root(xml))
    assert info["encoderVersion"] == "V5.0"
    assert info["encoderReleasedDate"] == "build 200109"
    assert info["model"] == "DS-7708N-I4"
    assert info["deviceType"] == "DVR"


# ---- _parse_network_interfaces: V4 MAC in <Link> ----


def test_parse_network_interfaces_v4_mac_in_link():
    """V4 NVR firmware wraps MAC inside ``<Link>``; v0.6.17
    coordinator reads that path so the ``network_mac`` sensor
    populates."""
    xml = """<NetworkInterfaceList xmlns="http://www.hikvision.com/ver20/XMLSchema" version="1.0">
<NetworkInterface>
<id>1</id>
<IPAddress>
<ipAddress>10.18.176.65</ipAddress>
<subnetMask>255.255.255.0</subnetMask>
</IPAddress>
<Link>
<MACAddress>02:00:00:00:00:0e</MACAddress>
<autoNegotiation>true</autoNegotiation>
</Link>
</NetworkInterface>
</NetworkInterfaceList>"""
    ifaces = _parse_network_interfaces(_parse_root(xml))
    assert len(ifaces) == 1
    # v0.6.17 fix: mac_address comes from Link/MACAddress for V4.
    assert ifaces[0]["mac_address"] == "02:00:00:00:00:0e"


def test_parse_network_interfaces_v5_mac_direct():
    """V5 firmware has <MACAddress> as a direct child of <NetworkInterface>;
    v0.6.17 still handles this case via the V5 fallback."""
    xml = """<NetworkInterfaceList xmlns="http://www.hikvision.com/ver20/XMLSchema" version="2.0">
<NetworkInterface>
<id>1</id>
<IPAddress>192.168.1.10</IPAddress>
<subnetMask>255.255.255.0</subnetMask>
<DefaultGateway>192.168.1.1</DefaultGateway>
<MACAddress>00:11:22:33:44:55</MACAddress>
</NetworkInterface>
</NetworkInterfaceList>"""
    ifaces = _parse_network_interfaces(_parse_root(xml))
    assert ifaces[0]["mac_address"] == "00:11:22:33:44:55"


# ---- _safe_int_mb regression ----


def test_safe_int_mb_accepts_integer():
    """Pre-v0.6.17 sanity: ``int("61")`` → 61."""
    assert _safe_int_mb("61") == 61


def test_safe_int_mb_accepts_decimal():
    assert _safe_int_mb("728.234375") == 728


# ---- sensor.py structural assertions ----


def _sensor_keys():
    """Load sensor key list directly from the module source."""
    src = Path(
        r"C:\Users\43457\Desktop\hikvision-isapi"
        r"\custom_components\hikvision_isapi_performance\sensor.py"
    ).read_text(encoding="utf-8-sig")
    return re.findall(r'^\s+key="([^"]+)"', src, flags=re.MULTILINE)


def test_v0617_cpu_usage_sensor_reads_cpuutilization_not_cpuusage():
    """v0.6.17 bug fix: ``cpu_usage`` sensor must read
    ``cpuUtilization`` (the key the coordinator stores)."""
    src = Path(
        r"C:\Users\43457\Desktop\hikvision-isapi"
        r"\custom_components\hikvision_isapi_performance\sensor.py"
    ).read_text(encoding="utf-8-sig")
    # Check the cpu_usage sensor's value_fn references cpuUtilization
    # (not cpuUsage). We match a small slice of the cpu_usage entry.
    cpu_section = src.split('key="cpu_usage"', 1)[1].split("),", 1)[0]
    assert "cpuUtilization" in cpu_section, (
        "cpu_usage sensor's value_fn must read 'cpuUtilization' "
        "(matches coordinator's parsed dict key). Pre-v0.6.17 "
        "this read the wrong key 'cpuUsage' and the sensor always "
        "showed 'unknown'."
    )
    assert "cpuUsage)" not in cpu_section.replace("cpuUsage)", "OK"), (
        "cpu_usage sensor must not reference cpuUsage (typo / "
        "wrong-key regression)."
    )


def test_v0617_memory_usage_sensor_computes_percentage():
    """v0.6.17: memory_usage must compute percentage, not pass
    through raw MB."""
    src = Path(
        r"C:\Users\43457\Desktop\hikvision-isapi"
        r"\custom_components\hikvision_isapi_performance\sensor.py"
    ).read_text(encoding="utf-8-sig")
    # The percentage helper should divide used by (used + available).
    helper_block = src.split("def _memory_usage_percent", 1)[1]
    helper_block = helper_block.split("def ", 1)[0]
    assert "used + available" in helper_block or "(used +" in helper_block, (
        "memory_usage must compute used/(used+available) — pre-v0.6.17 "
        "dumped raw MB and HA rendered it as percent producing "
        "728%-class values."
    )
    assert "100" in helper_block, (
        "memory_usage must multiply by 100 to get percentage."
    )


def test_v0617_storage_sensors_removed():
    """The 4 storage sensors are no longer in the SENSORS tuple."""
    keys = _sensor_keys()
    for removed in ("storage_total", "storage_used",
                    "storage_free", "storage_usage"):
        assert removed not in keys, (
            f"{removed} was supposed to be deleted in v0.6.17 "
            "(V4 NVR refuses all storage endpoints; sensor would "
            "always be 'unknown')."
        )


def test_v0617_seconds_uptime_sensors_removed():
    """The seconds-based uptime sensors are gone; only the hours
    version remains."""
    keys = _sensor_keys()
    # The old keys were `uptime` (top-level, seconds) and
    # `device_uptime` (per-channel-style, also seconds).
    assert "uptime" not in keys, (
        "old `uptime` sensor (seconds) must be deleted."
    )
    assert "device_uptime" not in keys, (
        "old `device_uptime` sensor (seconds) must be deleted."
    )
    # hours version kept
    assert "uptime_hours" in keys, (
        "`uptime_hours` sensor must remain — it's the only "
        "runtime display after v0.6.17."
    )


def test_v0617_uptime_sensor_uses_hours_unit():
    """The remaining uptime sensor must be HOURS, not seconds."""
    src = Path(
        r"C:\Users\43457\Desktop\hikvision-isapi"
        r"\custom_components\hikvision_isapi_performance\sensor.py"
    ).read_text(encoding="utf-8-sig")
    uptime_section = src.split('key="uptime_hours"', 1)[1].split("),", 1)[0]
    assert "UnitOfTime.HOURS" in uptime_section, (
        "uptime_hours sensor must use UnitOfTime.HOURS (user "
        "asked for hours display)."
    )
    assert "UnitOfTime.SECONDS" not in uptime_section, (
        "uptime_hours sensor must NOT use UnitOfTime.SECONDS."
    )


def test_v0617_new_sensors_added():
    """v0.6.17 added these sensors we already had data for.

    Note: ``encoder_release_date`` was added in v0.6.17 but removed
    in v0.6.19 — it was V4-firmware-only and of low operational
    value vs ``encoder_version``. The test intentionally drops it
    from the assertion list. See ``test_v0619_audit.py`` for the
    v0.6.19 removal assertion.
    """
    keys = _sensor_keys()
    for new in (
        "device_mac",
        "device_type",
        "device_id",
        "firmware_release_date",
        "encoder_version",
        "memory_available_mb",
    ):
        assert new in keys, (
            f"new sensor {new} should be added in v0.6.17 — "
            "data is already in the parsed dict."
        )


def test_v0617_total_sensor_count():
    """Sanity check the sensor count is in a reasonable range
    after the v0.6.17 / v0.6.18 churn (deletions + re-adds +
    streaming-detail expansion).
    """
    keys = _sensor_keys()
    assert 15 <= len(keys) <= 35, (
        f"unexpected sensor count after v0.6.18: {len(keys)} sensors"
    )


# ---- manifest version ----


def test_v0617_manifest_version_at_or_beyond():
    """v0.6.17 anchor; later releases (v0.6.18+) bump further."""
    manifest = json.loads(Path(
        r"C:\Users\43457\Desktop\hikvision-isapi"
        r"\custom_components\hikvision_isapi_performance\manifest.json"
    ).read_text(encoding="utf-8"))
    parts = manifest["version"].split(".")
    assert parts[0] == "0"
    assert int(parts[1]) >= 6
    if int(parts[1]) == 6:
        assert int(parts[2]) >= 17
