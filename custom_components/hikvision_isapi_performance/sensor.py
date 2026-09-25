"""Sensor platform for Hikvision ISAPI.

Exposes device_info and system_status as sensors. The mapping is
fixed in ``SENSORS`` — the user can rename / re-unit / hide any of
them in the HA UI.

v0.6.19 cleanup (per user feedback after v0.6.18):

- ``_or_none`` helper normalises empty strings (``""``) to ``None`` so
  the entity shows "unknown" instead of a blank cell. V5 IPC firmware
  returns ``<encoderVersion></encoderVersion>`` for fields it
  doesn't support — pre-v0.6.19 these came through as empty strings.
- ``device_status`` no longer reads ``systemStatus.deviceStatus``
  (which is "Unknown" on V4 NVR because that XML field doesn't
  exist there). It now derives from coordinator data: "在线" when
  the coordinator has parsed device_info, "离线" when it doesn't.
  Coordinator failure already takes the entity unavailable in HA.
- ``mtu`` sensor exposes the interface MTU (parsed for V4 direct
  schema and V5 ``<Link>``-nested schema).
- ``time_mode`` sensor exposes NTP vs manual from ``/ISAPI/System/time``.
- ``encoder_release_date`` removed — V4-specific, only populated on
  some firmwares, of low operational value vs ``encoder_version``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, UnitOfInformation, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import HikvisionISAPIData, HikvisionISAPICoordinator
from .entity import HikvisionISAPIEntity

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, kw_only=True)
class HikvisionISAPISensorDescription(SensorEntityDescription):
    """A sensor description for ISAPI-derived values."""

    value_fn: Any  # Callable[[HikvisionISAPIData], Any]


SENSORS: tuple[HikvisionISAPISensorDescription, ...] = (
    # ---- device identity (from /ISAPI/System/deviceInfo) ----
    HikvisionISAPISensorDescription(
        key="model",
        translation_key="model",
        name="型号",
        icon="mdi:information-outline",
        value_fn=lambda d: _or_none(d.device_info.get("model")),
    ),
    HikvisionISAPISensorDescription(
        key="serial_number",
        translation_key="serial_number",
        name="序列号",
        icon="mdi:barcode",
        value_fn=lambda d: _or_none(d.device_info.get("serialNumber")),
    ),
    HikvisionISAPISensorDescription(
        key="firmware_version",
        translation_key="firmware_version",
        name="固件版本",
        icon="mdi:chip",
        value_fn=lambda d: _or_none(d.device_info.get("firmwareVersion")),
    ),
    HikvisionISAPISensorDescription(
        key="firmware_release_date",
        translation_key="firmware_release_date",
        name="固件发布日期",
        icon="mdi:calendar",
        # V5 firmware: "build 240522"; V4: same format.
        value_fn=lambda d: _or_none(d.device_info.get("firmwareReleasedDate")),
    ),
    HikvisionISAPISensorDescription(
        key="device_type",
        translation_key="device_type",
        name="设备类型",
        icon="mdi:devices",
        # Direct from deviceInfo; values like "IPCamera",
        # "NetworkVideoRecorder", "DVR". (V5.x IPC may return
        # "IPZoom" or other product-line-specific strings.)
        value_fn=lambda d: _or_none(d.device_info.get("deviceType")),
    ),
    HikvisionISAPISensorDescription(
        key="device_id",
        translation_key="device_id",
        name="设备 ID",
        icon="mdi:identifier",
        # UUID-style device ID from <deviceID>. Useful for
        # distinguishing physically-identical units in dashboards.
        value_fn=lambda d: _or_none(d.device_info.get("deviceID")),
    ),
    HikvisionISAPISensorDescription(
        key="device_mac",
        translation_key="device_mac",
        name="设备 MAC",
        icon="mdi:network",
        # Direct from deviceInfo, distinct from the per-NIC MAC
        # reported by /System/Network/interfaces. On the user's
        # dual-NIC NVR these may differ; the deviceInfo MAC is
        # the canonical hardware address.
        value_fn=lambda d: _or_none(d.device_info.get("macAddress")),
    ),
    HikvisionISAPISensorDescription(
        key="encoder_version",
        translation_key="encoder_version",
        name="编码器版本",
        icon="mdi:codec",
        # V4 firmware has this; V5 may omit (returns "").
        value_fn=lambda d: _or_none(d.device_info.get("encoderVersion")),
    ),
    # ---- system status (from /ISAPI/System/status) ----
    HikvisionISAPISensorDescription(
        key="device_status",
        translation_key="device_status",
        name="设备状态",
        icon="mdi:check-circle",
        # v0.6.19: derived from coordinator data, not from
        # ``<deviceStatus>``. V4 NVR firmware doesn't emit
        # ``<deviceStatus>`` in its ``/System/status`` XML, so the
        # pre-v0.6.19 read always returned "Unknown" on V4 NVRs.
        # Coordinator success means the device responded; coordinator
        # failure already takes the entity unavailable in HA.
        value_fn=lambda d: _device_status_text(d),
    ),
    HikvisionISAPISensorDescription(
        key="cpu_usage",
        translation_key="cpu_usage",
        name="CPU 使用率",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:chip",
        # v0.6.17 fix: reads ``cpuUtilization`` (matching the key
        # the coordinator stores). Pre-v0.6.17 this was the wrong
        # key ``cpuUsage`` and the sensor always showed "unknown".
        value_fn=lambda d: _safe_int(d.system_status.get("cpuUtilization")),
    ),
    HikvisionISAPISensorDescription(
        key="memory_usage_percent",
        translation_key="memory_usage",
        name="内存使用率",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:memory",
        # v0.6.17 fix: was reading raw MB (728.234375 on V4 NVR)
        # and HA rendered it as PERCENTAGE → "728%". Now compute
        # percentage from ``memoryUsage / (memoryUsage +
        # memoryAvailable)``. Pre-v0.6.17 this was the
        # misnamed-and-miscomputed ``memory_usage`` sensor.
        value_fn=lambda d: _memory_usage_percent(d),
    ),
    HikvisionISAPISensorDescription(
        key="memory_available_mb",
        translation_key="memory_available",
        name="内存剩余 (MB)",
        native_unit_of_measurement=UnitOfInformation.MEGABYTES,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:memory",
        # Raw available memory in MB, useful for monitoring
        # trend (capacity-constrained IPCs). v0.6.22: dropped the
        # ``DATA_SIZE`` device class and the
        # ``UnitOfTime.SECONDS`` unit — both were a v0.6.17-era
        # copy-paste bug. The value is in MB, not bytes; using
        # DATA_SIZE made HA treat the raw integer as bytes (and
        # render it as "402 s" because the native_unit was
        # ``UnitOfTime.SECONDS``). Plain MEGABYTES native_unit
        # with no device_class gives "402 MB" cleanly.
        value_fn=lambda d: _safe_int(d.system_status.get("memoryAvailable")),
    ),
    HikvisionISAPISensorDescription(
        key="uptime_hours",
        translation_key="uptime",
        name="运行时长",
        device_class=SensorDeviceClass.DURATION,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfTime.HOURS,
        icon="mdi:clock-outline",
        # v0.6.17: only sensor exposing runtime. Hours, rounded
        # to 1 decimal. Pre-v0.6.17 there were two seconds-based
        # sensors (``uptime`` / ``device_uptime``) which were
        # deleted because human-readable values are more useful
        # for runtime tracking.
        value_fn=lambda d: _uptime_hours(d.system_status.get("uptime")),
    ),
    # ---- channels (only populates if device exposes channel list) ----
    HikvisionISAPISensorDescription(
        key="channel_count",
        translation_key="channel_count",
        name="通道数",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:counter",
        value_fn=lambda d: len(d.channels),
    ),
    # v0.6.28: cross-check channel count from the device's own
    # capability probe (``/ISAPI/System/capabilities``,
    # ``<VideoInputChannelNums>``). IPCs report 1; NVRs report the
    # number of camera channels they support. Useful for spotting
    # new / obscure device types whose ``deviceType`` string the
    # integration doesn't recognise — if the live channel count
    # doesn't match the device's own claim, the dashboard surfaces
    # the discrepancy. ``None`` when the endpoint isn't reachable.
    HikvisionISAPISensorDescription(
        key="capability_video_input_channels",
        translation_key="capability_video_input_channels",
        name="能力 — 视频输入通道数",
        icon="mdi:counter",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d: d.system_capabilities.get("video_input_channels"),
    ),
    # ---- network (first interface) ----
    HikvisionISAPISensorDescription(
        key="network_ip",
        translation_key="network_ip",
        name="IP 地址",
        icon="mdi:ip",
        value_fn=lambda d: _first_iface(d, "ip_address"),
    ),
    HikvisionISAPISensorDescription(
        key="network_subnet",
        translation_key="network_subnet",
        name="子网掩码",
        icon="mdi:subnet",
        value_fn=lambda d: _first_iface(d, "subnet_mask"),
    ),
    HikvisionISAPISensorDescription(
        key="network_gateway",
        translation_key="network_gateway",
        name="默认网关",
        icon="mdi:router-network",
        value_fn=lambda d: _first_iface(d, "default_gateway"),
    ),
    HikvisionISAPISensorDescription(
        key="network_mac",
        translation_key="network_mac",
        name="网卡 MAC",
        icon="mdi:network",
        # v0.6.17 fix: V4 NVR nests MAC inside <Link>, V5 puts
        # it directly. coord's _parse_network_interfaces now
        # tries both paths so this sensor populates on both
        # firmware generations.
        value_fn=lambda d: _first_iface(d, "mac_address"),
    ),
    HikvisionISAPISensorDescription(
        key="network_mtu",
        translation_key="network_mtu",
        name="网卡 MTU",
        icon="mdi:network",
        state_class=SensorStateClass.MEASUREMENT,
        # v0.6.27: drop the trailing "B" suffix. MTU is a unitless
        # byte count — HA displays raw integers without unit if
        # ``native_unit_of_measurement`` isn't set, which is what
        # the user asked for ("just a number, no B").
        # v0.6.19: read first interface MTU. V5 wraps MTU inside
        # ``<Link>`` (alongside MAC), V4 puts it directly on the
        # interface. _parse_network_interfaces handles both.
        value_fn=lambda d: _first_iface(d, "mtu"),
    ),
    # ---- NIC 2 (v0.6.23) — only registered when device has >= 2
    # network interfaces. Hikvision V4 NVRs (DS-7708N-I4 / DS-8632-I8)
    # ship with 2 NICs by default; users on these devices previously
    # saw only the first NIC's data. See async_setup_entry for the
    # conditional registration logic.
    #
    # ``translation_key`` is distinct from NIC 1's because the
    # entity's i18n string ("Network 2 IP Address") differs from
    # NIC 1's ("Network IP Address"). Reusing NIC 1's key would
    # trip HA's i18n uniqueness check and surface both NICs as
    # "Network IP Address" in the user's language.
    # ----
    HikvisionISAPISensorDescription(
        key="network_2_ip",
        translation_key="network_2_ip",
        name="网卡 2 IP 地址",
        icon="mdi:ip",
        value_fn=lambda d: _second_iface(d, "ip_address"),
    ),
    HikvisionISAPISensorDescription(
        key="network_2_subnet",
        translation_key="network_2_subnet",
        name="网卡 2 子网掩码",
        icon="mdi:subnet",
        value_fn=lambda d: _second_iface(d, "subnet_mask"),
    ),
    HikvisionISAPISensorDescription(
        key="network_2_gateway",
        translation_key="network_2_gateway",
        name="网卡 2 默认网关",
        icon="mdi:router-network",
        value_fn=lambda d: _second_iface(d, "default_gateway"),
    ),
    HikvisionISAPISensorDescription(
        key="network_2_mac",
        translation_key="network_2_mac",
        name="网卡 2 MAC",
        icon="mdi:network",
        value_fn=lambda d: _second_iface(d, "mac_address"),
    ),
    HikvisionISAPISensorDescription(
        key="network_2_mtu",
        translation_key="network_2_mtu",
        name="网卡 2 MTU",
        icon="mdi:network",
        state_class=SensorStateClass.MEASUREMENT,
        # v0.6.27: same as network_mtu — drop the trailing "B"
        # suffix so HA displays the raw integer.
        value_fn=lambda d: _second_iface(d, "mtu"),
    ),
    # ---- device clock (from /ISAPI/System/time, v0.6.19) ----
    HikvisionISAPISensorDescription(
        key="time_mode",
        translation_key="time_mode",
        name="时间同步模式",
        icon="mdi:clock-check",
        # Returns ``"NTP"`` / ``"manual"`` from
        # ``/ISAPI/System/time``. Useful to detect devices whose
        # time drifted because NTP is misconfigured. The
        # coordinator's _fetch_time is best-effort: on V4 firmware
        # that doesn't expose the endpoint this stays "unknown".
        value_fn=lambda d: _or_none(d.time_info.get("time_mode")),
    ),
    # ---- storage (re-added in v0.6.18) ----
    # Storage endpoints are 4xx on the user's V4 NVR
    # (DS-7708N-I4 V4.1.18) but work on V5 NVRs and modern
    # Hikvision DVRs. Keep these sensors so users with working
    # firmware see real values; V4 users continue to see
    # "unknown" gracefully.
    HikvisionISAPISensorDescription(
        key="storage_total_gb",
        translation_key="storage_total",
        name="存储总量 (GB)",
        device_class=SensorDeviceClass.DATA_SIZE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfInformation.GIGABYTES,
        icon="mdi:harddisk",
        # Display in GB (HA's preferred unit for HDD sizes);
        # MB internally for cross-firmware compatibility.
        value_fn=lambda d: _mb_to_gb(d.storage.get("total_mb")),
    ),
    HikvisionISAPISensorDescription(
        key="storage_used_gb",
        translation_key="storage_used",
        name="存储已用 (GB)",
        device_class=SensorDeviceClass.DATA_SIZE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfInformation.GIGABYTES,
        icon="mdi:harddisk",
        value_fn=lambda d: _mb_to_gb(d.storage.get("used_mb")),
    ),
    HikvisionISAPISensorDescription(
        key="storage_free_gb",
        translation_key="storage_free",
        name="存储剩余 (GB)",
        device_class=SensorDeviceClass.DATA_SIZE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfInformation.GIGABYTES,
        icon="mdi:harddisk",
        value_fn=lambda d: _mb_to_gb(d.storage.get("free_mb")),
    ),
    HikvisionISAPISensorDescription(
        key="storage_usage_percent",
        translation_key="storage_usage",
        name="存储使用率",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=PERCENTAGE,
        icon="mdi:harddisk",
        value_fn=lambda d: _storage_usage_pct(d.storage),
    ),
    # ---- per-channel streaming detail (v0.6.18 / v0.6.27) ----
    # Per-channel codec, resolution, frame rate, bitrate, audio codec,
    # AND channel name. Populated from
    # ``/ISAPI/Streaming/channels`` (V5 IPCs and V5+ NVRs). V4 NVR
    # firmware usually returns 4 on this endpoint so these sensors
    # stay "unknown" on V4 NVRs — that's fine, V4 NVRs expose the
    # info via different endpoints we don't poll yet.
    #
    # v0.6.27: previously only ``channel_1_*`` sensors were emitted
    # (5 entities for the first channel only). Users with NVRs that
    # have 8/16/32 channels only saw channel 1's codec/resolution.
    # Now all detected channels get the same 6 sensors. The dynamic
    # generation happens in ``async_setup_entry`` below — the static
    # ``channel_1_*`` entries are gone, but the new generator emits
    # the SAME key for channel id="1" so users' existing entity
    # registry entries (created under v0.6.18–v0.6.26) match
    # seamlessly without orphaned-entity cleanup.
    # ----
    # ---- reboot count (V4 NVR doesn't return this; V5 IPC does) ----
    HikvisionISAPISensorDescription(
        key="reboot_count",
        translation_key="reboot_count",
        name="重启次数",
        state_class=SensorStateClass.TOTAL_INCREASING,
        icon="mdi:restart",
        value_fn=lambda d: _safe_int(d.system_status.get("rebootCount")),
    ),
)


# ---- helpers ----


def _safe_int(value: Any) -> int | None:
    """Best-effort int parsing; returns ``None`` for missing / non-int."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _or_none(value: Any) -> str | None:
    """Normalise empty/whitespace strings to ``None``.

    v0.6.19: V5 IPC firmware returns ``<encoderVersion></encoderVersion>``
    for fields it doesn't populate. Pre-v0.6.19 these came through as
    ``""`` and the HA entity rendered a blank cell instead of
    "unknown". Returning ``None`` lets HA display the standard
    "unknown" state.
    """
    if value is None:
        return None
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    return value


def _device_status_text(data: HikvisionISAPIData) -> str | None:
    """v0.6.19: derive device status from data availability.

    Pre-v0.6.19 this read ``systemStatus.deviceStatus`` which is
    "Unknown" on V4 NVR (the field doesn't exist in V4 XML). We
    can't fall back to coordinator failure for unavailable (HA
    already handles that), so the only meaningful states are
    "在线" (data parsed) and "离线" (no data — unreachable).

    Empty ``device_info`` dict means the coordinator's deviceInfo
    fetch returned None / 4xx. Treat that as unreachable.
    """
    if not data.device_info:
        return "离线"
    return "在线"


def _mb_to_gb(mb: Any) -> float | None:
    """Convert MB → GB (decimal, 1 GB = 10^3 MB) for HDD display.

    Pre-v0.6.17 this existed. v0.6.18 re-adds it for the storage
    sensors. Returns ``None`` for missing / non-numeric MB so the
    entity shows "unknown" rather than "0 GB" with no data.
    """
    if mb is None:
        return None
    try:
        return round(float(mb) / 1024.0, 1)
    except (TypeError, ValueError):
        return None


def _storage_usage_pct(storage: dict[str, Any]) -> float | None:
    """Compute used/total percentage for storage.

    Returns ``None`` when total/used aren't both populated (e.g.
    the device doesn't expose storage endpoints) so the entity
    shows "unknown" rather than "0%" placeholder values.
    """
    total = storage.get("total_mb")
    used = storage.get("used_mb")
    if total is None or used is None or total <= 0:
        return None
    return round(used * 100 / total, 1)


def _memory_usage_percent(data: HikvisionISAPIData) -> float | None:
    """Compute memory usage percentage from ``memoryUsage`` and
    ``memoryAvailable``.

    Both fields are in MB. Memory percentage isn't a Hikvision
    device-reported metric — it's ``used / (used + available) * 100``
    rounded to 1 decimal. Returns ``None`` if either field is
    missing (sensor shows "unknown" instead of "0%" with no data).
    """
    used = _safe_int(data.system_status.get("memoryUsage"))
    available = _safe_int(data.system_status.get("memoryAvailable"))
    if used is None or available is None:
        return None
    total = used + available
    if total <= 0:
        return None
    return round(used * 100 / total, 1)


def _uptime_hours(value: Any) -> float | None:
    """Convert uptime seconds to hours, rounded to 1 decimal.

    Pre-v0.6.17 this was hidden behind a seconds-based sensor
    pair the user didn't want. Now it's the only uptime sensor,
    presented in human-friendly hours.
    """
    secs = _safe_int(value)
    if secs is None:
        return None
    return round(secs / 3600, 1)


def _first_iface(data: HikvisionISAPIData, field: str) -> Any:
    """Return ``field`` from the first network interface, or None.

    v0.6.19: returns ``None`` for empty strings too, so network
    sensors don't render blank cells.
    """
    ifaces = data.network_interfaces
    if not ifaces:
        return None
    return _or_none(ifaces[0].get(field))


def _second_iface(data: HikvisionISAPIData, field: str) -> Any:
    """v0.6.23: return ``field`` from the second network interface.

    Hikvision V4 NVRs (e.g. ``DS-7708N-I4``) ship with two NICs.
    The first refresh's ``network_interfaces`` list usually has
    both. Used by the ``network_2_*`` sensor set below; returns
    ``None`` for single-NIC devices or when the second interface
    is missing the field.
    """
    ifaces = data.network_interfaces
    if len(ifaces) < 2:
        return None
    return _or_none(ifaces[1].get(field))


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up static ISAPI sensors (model, firmware, cpu, memory, etc.).

    v0.6.22: storage sensors (4 entities) are registered only when
    ``coordinator.device_type`` is NVR or DVR. IPCs have no HDD,
    so the storage endpoints return 4 "Invalid Operation" on the
    user's V5 IPC (``DS-FB2127``) — registering the sensors on IPC
    showed 3-4 permanently-unknown entities cluttering the device
    card. NVR/DVR storage sensors remain registered even on
    firmware that 4xx's the endpoints (V4 NVR users still see
    "unknown" — gracefully degraded, not a hard error).

    v0.6.23: NIC 2 sensors (5 entities: ip / subnet / gateway /
    mac / mtu) are registered only when the coordinator reports
    ``len(network_interfaces) >= 2``. Most V4 NVRs ship with two
    NICs (e.g. ``DS-7708N-I4`` exposes 10.18.176.65 and
    192.168.10.10). Single-NIC devices and IPCs don't get the
    NIC 2 sensors at all. Late-arriving NIC 2 data is handled by
    a one-shot coordinator listener (mirrors the per-channel
    pattern in ``binary_sensor.py``).
    """
    from .const import (
        DEVICE_TYPE_DVR,
        DEVICE_TYPE_NETWORK_VIDEO_RECORDER,
    )

    coordinator: HikvisionISAPICoordinator = hass.data[DOMAIN][entry.entry_id]
    device_type = coordinator.device_type
    # Storage sensors are useful only on NVR/DVR. Skip on IPC.
    is_recorder = device_type in (
        DEVICE_TYPE_NETWORK_VIDEO_RECORDER, DEVICE_TYPE_DVR,
    )

    # v0.6.27: only suppress the ``cpu_usage`` sensor on V4
    # firmware NVR/DVR. Pre-v0.6.26 this was suppressed on every
    # NVR/DVR, but V5/V6/V7 NVR firmware reports the NVR's own
    # CPU correctly — only the V4 firmware family has the
    # documented ``cpuUtilization=0`` bug. Detect via the
    # ``firmwareVersion`` field (e.g. "V4.1.18" → V4, "V5.2.2"
    # → keep, "V4.62.000" → V4). The check is ``startswith("V4")``
    # case-insensitive; we tolerate leading whitespace from the
    # device's XML.
    firmware_version = (
        coordinator.device_info.get("firmwareVersion", "") or ""
    ).strip().upper()
    is_v4_recorder = is_recorder and firmware_version.startswith("V4")

    # NIC 2 sensors are useful only when the device has >=2
    # network interfaces. Check synchronously first (data may
    # already be available from a previous coordinator refresh);
    # otherwise listen for late arrival.
    nic2_descs = tuple(d for d in SENSORS if d.key.startswith("network_2_"))

    # Per-channel streaming detail + name sensors (v0.6.27).
    # v0.6.18 added 5 static ``channel_1_*`` sensors; users with
    # NVRs (8/16/32 channels) only saw channel 1's codec/
    # resolution/etc. Now we generate the same 6 entities per
    # channel dynamically. The static entries for channel 1 are
    # gone (the dynamic generator emits identical keys for
    # ``id == "1"`` so existing entity registry entries match
    # without orphaned-entity cleanup).
    per_channel_entities: list[HikvisionISAPISensor] = []
    for ch in coordinator.channels:
        per_channel_entities.extend(
            _build_per_channel_entities(coordinator, entry, ch)
        )
    # Track which channels we already registered so the late-
    # arrival listener doesn't duplicate them.
    coordinator._hikvision_isapi_performance_channel_added_ids = [
        ch.get("id", "") for ch in coordinator.channels
    ]  # type: ignore[attr-defined]

    # NIC 1 = everything except network_2_* and channel_*_* (the
    # latter are emitted per-channel above).
    nic1_descs = tuple(
        d for d in SENSORS
        if not d.key.startswith("network_2_")
        and not d.key.startswith("channel_")
    )

    entities: list[HikvisionISAPISensor] = [
        HikvisionISAPISensor(coordinator, entry, desc)
        for desc in nic1_descs
        if (
            # storage filter: storage sensors only on NVR/DVR
            (is_recorder or not desc.key.startswith("storage_"))
            # cpu_usage filter: only suppress on V4 firmware NVR/DVR
            and (not is_v4_recorder or desc.key != "cpu_usage")
        )
    ]
    # Append per-channel entities (built above).
    entities.extend(per_channel_entities)

    # Add NIC 2 entities synchronously if the coordinator already
    # has the second interface data.
    if coordinator.network_interfaces and len(coordinator.network_interfaces) >= 2:
        entities.extend(
            HikvisionISAPISensor(coordinator, entry, desc) for desc in nic2_descs
        )
        coordinator._hikvision_isapi_performance_nic2_added = True  # type: ignore[attr-defined]
    else:
        coordinator._hikvision_isapi_performance_nic2_added = False  # type: ignore[attr-defined]

    async_add_entities(entities)

    # Late-arrival listener: if the first refresh hasn't populated
    # the second NIC yet, register it on the next update that has
    # >= 2 interfaces.
    if not coordinator._hikvision_isapi_performance_nic2_added:  # type: ignore[attr-defined]
        coordinator.async_add_listener(
            _make_nic2_listener(hass, entry, coordinator, async_add_entities, nic2_descs),
        )

    # Late-arrival listener for per-channel sensors. If the first
    # coordinator refresh didn't yet return channel data, register
    # the entities on the next refresh that does.
    coordinator.async_add_listener(
        _make_per_channel_listener(hass, entry, coordinator, async_add_entities),
    )


def _make_nic2_listener(
    hass: HomeAssistant,
    entry: ConfigEntry,
    coordinator: HikvisionISAPICoordinator,
    async_add_entities: AddEntitiesCallback,
    nic2_descs,
):
    """One-shot listener: register NIC 2 sensors when network_interfaces
    reaches 2+ entries.
    """

    async def _on_update() -> None:
        if getattr(coordinator, "_hikvision_isapi_performance_nic2_added", False):
            return
        ifaces = coordinator.network_interfaces
        if not ifaces or len(ifaces) < 2:
            return
        coordinator._hikvision_isapi_performance_nic2_added = True  # type: ignore[attr-defined]
        async_add_entities(
            [HikvisionISAPISensor(coordinator, entry, d) for d in nic2_descs],
        )

    return _on_update


# v0.6.27: per-channel streaming-detail + name sensors.
#
# Builds the 6 sensors for one channel (codec, resolution, frame
# rate, bitrate, audio codec, name). The keys all carry the channel
# id (``channel_<id>_video_codec`` etc.) so users with NVRs that
# have 8/16/32 channels get the same set of entities for each
# channel — not just channel 1 like v0.6.18–v0.6.26 did.
#
# ``value_fn`` closures capture ``channel_id`` so each sensor only
# reads its own channel's data from ``streaming_channel_detail``.
# On devices that don't expose the streaming endpoint (V4 NVRs
# usually 4xx on ``/ISAPI/Streaming/channels``), the fields stay
# ``None`` and the entities render "unknown".


def _channel_streaming_field(channel_id: str, field: str):
    """Build a ``value_fn`` that reads ``field`` from a specific channel.

    v0.6.27: applies ``_or_none`` to string-typed fields so empty
    / whitespace XML cells render as ``None`` (→ "unknown") in HA
    rather than as a blank cell — the v0.6.19 audit test pins this
    behaviour for the original ``channel_1_*`` static sensors and
    we want the dynamic per-channel generator to do the same.
    Numeric fields (frame_rate / bitrate_kbps) pass through
    unchanged because ``_or_none`` is type-checked for strings.
    """
    string_fields = {"video_codec", "video_resolution", "audio_codec"}
    use_or_none = field in string_fields

    def _fn(data) -> Any:
        if data is None:
            return None
        for ch in data.streaming_channel_detail.get("channels", []) or []:
            if ch.get("id") == channel_id:
                value = ch.get(field)
                return _or_none(value) if use_or_none else value
        # Fallback: if the per-channel list doesn't include this
        # id (e.g. the parser collapsed it), the legacy
        # ``first_channel`` shape is still useful for channel 1
        # only.
        if channel_id == "1" or str(channel_id) == str(
            data.streaming_channel_detail.get("first_channel_id")
        ):
            value = data.streaming_channel_detail.get(field)
            return _or_none(value) if use_or_none else value
        return None
    return _fn


def _channel_name_value(channel_id: str):
    """Read the human-readable name of a channel from coordinator data."""
    def _fn(data) -> Any:
        if data is None:
            return None
        for ch in data.channels:
            if ch.get("id") == channel_id:
                name = ch.get("name")
                return _or_none(name) or f"Channel {channel_id}"
        return f"Channel {channel_id}"
    return _fn


def _build_per_channel_entities(
    coordinator: HikvisionISAPICoordinator,
    entry: ConfigEntry,
    channel: dict[str, Any],
) -> list[HikvisionISAPISensor]:
    """Build the 6 per-channel sensors for one channel dict.

    Returns HikvisionISAPISensor instances (not descriptions) so
    callers can append directly to ``async_add_entities``.

    Channel name comes from the coordinator's ``channels`` list
    (which already has the friendly name from the deviceInfo
    endpoint); codec/resolution/etc. come from
    ``streaming_channel_detail["channels"]``.
    """
    channel_id = str(channel.get("id", "")).strip() or "1"
    # Default-name fallback when the device doesn't return one.
    default_name = channel.get("name") or f"Channel {channel_id}"

    # Translation keys are SHARED across all channels (e.g.
    # ``channel_video_codec``). The entity's display name
    # carries the channel number. HA looks up translations by
    # translation_key so we reuse the same template for every
    # channel — only the entity name (rendered by HA's
    # ``_attr_name``) varies.
    descs = [
        HikvisionISAPISensorDescription(
            key=f"channel_{channel_id}_video_codec",
            translation_key="channel_video_codec",
            name=f"通道 {channel_id} 视频编码",
            icon="mdi:codec",
            value_fn=_channel_streaming_field(channel_id, "video_codec"),
        ),
        HikvisionISAPISensorDescription(
            key=f"channel_{channel_id}_video_resolution",
            translation_key="channel_video_resolution",
            name=f"通道 {channel_id} 分辨率",
            icon="mdi:aspect-ratio",
            value_fn=_channel_streaming_field(channel_id, "video_resolution"),
        ),
        HikvisionISAPISensorDescription(
            key=f"channel_{channel_id}_video_frame_rate",
            translation_key="channel_video_frame_rate",
            name=f"通道 {channel_id} 帧率",
            device_class=SensorDeviceClass.FREQUENCY,
            state_class=SensorStateClass.MEASUREMENT,
            native_unit_of_measurement="fps",
            icon="mdi:speedometer",
            value_fn=_channel_streaming_field(channel_id, "video_frame_rate"),
        ),
        HikvisionISAPISensorDescription(
            key=f"channel_{channel_id}_video_bitrate",
            translation_key="channel_video_bitrate",
            name=f"通道 {channel_id} 码率",
            device_class=SensorDeviceClass.DATA_RATE,
            state_class=SensorStateClass.MEASUREMENT,
            native_unit_of_measurement="kbps",
            icon="mdi:video",
            value_fn=_channel_streaming_field(channel_id, "video_bitrate_kbps"),
        ),
        HikvisionISAPISensorDescription(
            key=f"channel_{channel_id}_audio_codec",
            translation_key="channel_audio_codec",
            name=f"通道 {channel_id} 音频编码",
            icon="mdi:music-clef",
            value_fn=_channel_streaming_field(channel_id, "audio_codec"),
        ),
        HikvisionISAPISensorDescription(
            key=f"channel_{channel_id}_name",
            translation_key="channel_name",
            name=f"通道 {channel_id} 名称",
            icon="mdi:tag",
            value_fn=_channel_name_value(channel_id),
        ),
    ]
    return [
        HikvisionISAPISensor(coordinator, entry, desc) for desc in descs
    ]


def _make_per_channel_listener(
    hass: HomeAssistant,
    entry: ConfigEntry,
    coordinator: HikvisionISAPICoordinator,
    async_add_entities: AddEntitiesCallback,
):
    """Listener: register per-channel sensors when channels arrive late.

    Mirrors the NIC 2 listener pattern. Tracks the channel ids we
    already registered so we don't double-register after a reload
    or duplicate the sync block.
    """
    already_ids: set[str] = set(
        getattr(
            coordinator,
            "_hikvision_isapi_performance_channel_added_ids",
            [],
        ) or []
    )

    async def _on_update() -> None:
        current_ids = {
            str(ch.get("id", "")) for ch in coordinator.channels
        }
        new_ids = current_ids - already_ids
        if not new_ids:
            return
        new_entities: list[HikvisionISAPISensor] = []
        for ch in coordinator.channels:
            if str(ch.get("id", "")) in new_ids:
                new_entities.extend(
                    _build_per_channel_entities(coordinator, entry, ch)
                )
        if new_entities:
            already_ids.update(new_ids)
            coordinator._hikvision_isapi_performance_channel_added_ids = (
                list(already_ids)
            )  # type: ignore[attr-defined]
            async_add_entities(new_entities)

    return _on_update


class HikvisionISAPISensor(HikvisionISAPIEntity, SensorEntity):
    """Static ISAPI sensor entity."""

    entity_description: HikvisionISAPISensorDescription

    def __init__(
        self,
        coordinator: HikvisionISAPICoordinator,
        entry: ConfigEntry,
        description: HikvisionISAPISensorDescription,
    ) -> None:
        super().__init__(coordinator, entry)
        self.entity_description = description
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"

    @property
    def native_value(self) -> Any:
        if self.coordinator.data is None:
            return None
        return self.entity_description.value_fn(self.coordinator.data)