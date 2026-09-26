"""Binary sensor platform for Hikvision ISAPI.

Three per-channel binary sensors are created for each detected channel:
- ``channel_{N}_online`` — whether the channel is reachable
- ``channel_{N}_recording`` — whether the channel is currently recording
- ``channel_{N}_motion`` — whether motion is currently being detected

These complement the v0.1.0 device-level ``online`` / ``recording``
binary sensors (which aggregate "any channel"). Per-channel
breakdown is more useful for HA automations like
"if camera_3_motion → turn on hallway light".

The v0.1.23 pattern (from hikvision-snmp) is followed: system / per-
channel entities are registered immediately if data is available;
otherwise a coordinator listener adds them as soon as the first
poll completes.

v0.6.19 adds a device-level ``device_online`` binary sensor (CONNECTIVITY
class) for HA automations like "if device offline → notify". Coordinator
failure already takes the entity unavailable, so ``is_on`` is True when
the latest poll returned data and False only when ``data`` is None.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import HikvisionISAPICoordinator
from .entity import HikvisionISAPIEntity

_LOGGER = logging.getLogger(__name__)


# v0.6.26 (renamed v0.6.28): device time vs. HA host time delta
# threshold for ``dev_time_abnormal`` to flag a problem. 24 hours
# covers the usual "V4 NVR dead CMOS battery" symptom (clock
# rolls back to 2004-05) while tolerating a few hours of NTP
# drift / daylight-saving shift without false positives.
DEVICE_TIME_ABNORMAL_THRESHOLD_SECONDS = 24 * 60 * 60


def _memory_calibration_anomalous(
    memory_usage_mb: str | None,
    memory_available_mb: str | None,
) -> bool | None:
    """Detect likely KB/MB mis-calibration that survived ``_parse_system_status``.

    v0.6.29: surfaces the v0.6.25 mixed-unit heuristic as a HA
    binary sensor so users can spot devices where the memory
    numbers look wrong without grepping coordinator logs.

    Inputs are the post-normalisation MB values from
    ``system_status["memoryUsage"]`` and
    ``system_status["memoryAvailable"]`` (both normalised to MB
    by ``_parse_system_status``). The v0.6.25 heuristic already
    fixes the V5 IPC's KB/MB mismatch upstream — this function
    catches the cases that fall through:

    - either field is missing / unparseable → ``None`` (unknown)
    - both fields are zero / one is zero → ``None`` (unknown)
    - ratio outside [0.1, 50] → ``True`` (anomalous)
    - any absolute value > 8 GiB → ``True`` (Hikvision devices
      don't have more than ~4-8 GiB RAM)

    Crucially: this is a *diagnostic*, not a fix. We do NOT
    modify ``_parse_system_status``'s normalisation based on
    this signal — surfacing a wrong number as "anomalous" lets
    the user inspect the device rather than silently masking the
    unit issue with a second heuristic (which itself could be
    wrong on a different firmware generation).
    """
    try:
        used = int(memory_usage_mb) if memory_usage_mb else 0
        avail = int(memory_available_mb) if memory_available_mb else 0
    except (TypeError, ValueError):
        return None
    if used == 0 or avail == 0:
        # Insufficient data to judge. Distinguish "unknown" from
        # "looks plausible" — a device reporting 0 used with 0
        # available could just be mid-restart; we don't flag it.
        return None
    if avail > used * 50:
        # ~22 years worth of free RAM for 61 MB used → definitely
        # mixed units or a parser bug. The v0.6.25 heuristic
        # already handled the common case; this is the fallback.
        return True
    if avail < used * 0.1:
        # Used > 10x available → impossible on real hardware
        # (Linux kernel would OOM long before).
        return True
    if used > 8192 or avail > 8192:
        # Hikvision's largest cameras top out at 4-8 GiB. Anything
        # > 8 GiB is parser garbage, not real memory.
        return True
    return False


def _dev_time_abnormal(
    current_device_time: str | None,
    now_utc: datetime | None = None,
) -> bool | None:
    """Return True if the device's reported clock is more than 24 h off.

    v0.6.26 (renamed v0.6.28): Hikvision V4 NVRs with a dead CMOS
    battery roll ``<currentDeviceTime>`` back to 2004-05-03 — the
    device may be perfectly reachable, recording, and streaming,
    but its clock is wrong. We expose this as a binary sensor so
    HA automations can notify (and so the dashboard surfaces it
    without users digging through raw XML).

    Inputs:
    - ``current_device_time``: raw ``<currentDeviceTime>`` string
      from ``/ISAPI/System/status`` (ISO 8601 with offset, e.g.
      ``"2004-05-03T22:54:38+08:00"``) or ``None`` if missing.
    - ``now_utc``: optional ``datetime`` injected for tests.
      Defaults to ``datetime.now(timezone.utc)``.

    Returns:
    - ``True`` if the device clock is more than 24 h off from
      ``now_utc`` (in either direction — a 2004 clock is just as
      bad as a 2099 one).
    - ``False`` if the clock is within the threshold.
    - ``None`` if the device clock isn't known (no data, parse
      failure, or missing field) — HA renders this as
      ``unknown``, distinct from a hard ``False`` / ``True``.
    """
    if current_device_time is None:
        return None
    try:
        device_dt = datetime.fromisoformat(current_device_time.strip())
    except (TypeError, ValueError):
        # Garbage in the field (some firmwares emit "0" or empty).
        # Treat as unknown rather than raising — the entity will
        # show ``unknown`` and we won't spam the log every refresh.
        return None
    if device_dt.tzinfo is None:
        # Naive datetime — assume UTC (defensive default).
        device_dt = device_dt.replace(tzinfo=timezone.utc)
    now = now_utc if now_utc is not None else datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    delta = abs((device_dt - now).total_seconds())
    return delta > DEVICE_TIME_ABNORMAL_THRESHOLD_SECONDS


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up per-channel binary sensors (online / recording / motion)."""
    coordinator: HikvisionISAPICoordinator = hass.data[DOMAIN][entry.entry_id]

    entities: list[BinarySensorEntity] = [
        HikvisionISAPIDeviceOnlineBinarySensor(coordinator, entry),
        # v0.6.26: detect dead CMOS battery / wrong time on V4 NVRs.
        # Device class PROBLEM so the entity shows up red on the device
        # card and works out-of-the-box with HA's "problem" automations.
        HikvisionISAPIDevTimeAbnormalBinarySensor(coordinator, entry),
        # v0.6.29: diagnostic — ON when memory numbers look like a
        # KB/MB mis-calibration. User-visible so obscure firmware
        # quirks (where the v0.6.25 heuristic didn't fire) are
        # surfaced on the device card instead of buried in logs.
        HikvisionISAPIMemCalibrationWarnBinarySensor(coordinator, entry),
    ]
    for ch in coordinator.channels:
        entities.extend(_entities_for_channel(coordinator, entry, ch))
    async_add_entities(entities)

    # Per-channel listener for late-arriving channels.
    if not getattr(coordinator, "_hikvision_isapi_performance_binary_added", False):
        coordinator._hikvision_isapi_performance_binary_added = False  # type: ignore[attr-defined]
        coordinator.async_add_listener(
            _make_binary_listener(hass, entry, coordinator, async_add_entities)
        )


def _make_binary_listener(
    hass: HomeAssistant,
    entry: ConfigEntry,
    coordinator: HikvisionISAPICoordinator,
    async_add_entities: AddEntitiesCallback,
):
    """Coordinator listener: add per-channel binary entities on late data.

    v0.7.3: was ``async def`` and therefore never ran — HA calls
    ``async_add_listener`` callbacks synchronously and discards the
    result, so the coroutine was never awaited. Because platforms are
    set up BEFORE the first refresh, ``coordinator.channels`` is empty
    at setup time and this listener was the only path that could
    register per-channel online/recording/motion sensors.
    """

    def _on_update() -> None:
        if getattr(coordinator, "_hikvision_isapi_performance_binary_added", False):
            return
        if coordinator.data is None:
            return
        new_entities: list[BinarySensorEntity] = []
        for ch in coordinator.channels:
            new_entities.extend(_entities_for_channel(coordinator, entry, ch))
        if not new_entities:
            return
        coordinator._hikvision_isapi_performance_binary_added = True  # type: ignore[attr-defined]
        async_add_entities(new_entities)

    return _on_update


def _entities_for_channel(
    coordinator: HikvisionISAPICoordinator,
    entry: ConfigEntry,
    channel: dict[str, Any],
) -> list[BinarySensorEntity]:
    """Build the three binary sensors for one channel."""
    ch_id = channel.get("id", "")
    ch_name = channel.get("name") or f"Channel {ch_id}"
    return [
        HikvisionISAPIChannelOnlineBinarySensor(coordinator, entry, ch_id, ch_name),
        HikvisionISAPIChannelRecordingBinarySensor(
            coordinator, entry, ch_id, ch_name
        ),
        HikvisionISAPIChannelMotionBinarySensor(
            coordinator, entry, ch_id, ch_name
        ),
    ]


class HikvisionISAPIChannelOnlineBinarySensor(
    HikvisionISAPIEntity, BinarySensorEntity
):
    """Per-channel online / reachability binary sensor."""

    _attr_translation_key = "channel_online"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    def __init__(
        self,
        coordinator: HikvisionISAPICoordinator,
        entry: ConfigEntry,
        channel_id: str,
        channel_name: str,
    ) -> None:
        super().__init__(coordinator, entry)
        self._channel_id = channel_id
        self._channel_name = channel_name
        self._attr_unique_id = f"{entry.entry_id}_channel_{channel_id}_online"
        self._attr_name = f"{channel_name} 在线"

    @property
    def is_on(self) -> bool | None:
        if self.coordinator.data is None:
            return None
        for ch in self.coordinator.data.channels:
            if ch.get("id") == self._channel_id:
                return bool(ch.get("online", False))
        return None


class HikvisionISAPIChannelRecordingBinarySensor(
    HikvisionISAPIEntity, BinarySensorEntity
):
    """Per-channel recording state binary sensor."""

    _attr_translation_key = "channel_recording"
    _attr_device_class = BinarySensorDeviceClass.RUNNING

    def __init__(
        self,
        coordinator: HikvisionISAPICoordinator,
        entry: ConfigEntry,
        channel_id: str,
        channel_name: str,
    ) -> None:
        super().__init__(coordinator, entry)
        self._channel_id = channel_id
        self._channel_name = channel_name
        self._attr_unique_id = f"{entry.entry_id}_channel_{channel_id}_recording"
        self._attr_name = f"{channel_name} 录像中"

    @property
    def is_on(self) -> bool | None:
        if self.coordinator.data is None:
            return None
        for ch in self.coordinator.data.channels:
            if ch.get("id") == self._channel_id:
                # v0.7.4: ``recording`` is tri-state. ``None`` means no
                # ISAPI endpoint reported a <recordStatus> — true for
                # every NVR in the user's fleet (InputProxyChannelList
                # and the per-channel /status both omit it, and every
                # /ContentMgmt/Recording/* path returns 404). The old
                # ``bool(ch.get("recording", False))`` turned that
                # silence into "未在运行", asserting a fact the device
                # never stated. Mirrors the motion sensor below.
                recording = ch.get("recording")
                if recording is None:
                    return None
                return bool(recording)
        return None


class HikvisionISAPIChannelMotionBinarySensor(
    HikvisionISAPIEntity, BinarySensorEntity
):
    """Per-channel motion-detection binary sensor.

    Reads ``motionDetection`` from the per-channel status endpoint
    (``/ISAPI/ContentMgmt/InputProxy/channels/<id>/status``). On
    devices that don't implement the field, the entity shows
    ``unknown``.
    """

    _attr_translation_key = "channel_motion"
    _attr_device_class = BinarySensorDeviceClass.MOTION

    def __init__(
        self,
        coordinator: HikvisionISAPICoordinator,
        entry: ConfigEntry,
        channel_id: str,
        channel_name: str,
    ) -> None:
        super().__init__(coordinator, entry)
        self._channel_id = channel_id
        self._channel_name = channel_name
        self._attr_unique_id = f"{entry.entry_id}_channel_{channel_id}_motion"
        self._attr_name = f"{channel_name} 运动检测"

    @property
    def is_on(self) -> bool | None:
        if self.coordinator.data is None:
            return None
        for ch in self.coordinator.data.channels:
            if ch.get("id") == self._channel_id:
                # The coordinator may not have enriched this channel
                # with motion data if the per-channel status endpoint
                # is unavailable. Default to None (unknown) rather
                # than False in that case.
                if "motion_detected" not in ch:
                    return None
                return bool(ch.get("motion_detected", False))
        return None


class HikvisionISAPIDeviceOnlineBinarySensor(
    HikvisionISAPIEntity, BinarySensorEntity
):
    """Device-level online / reachability binary sensor (v0.6.19).

    ON when the coordinator's latest poll returned parsed data;
    OFF when the coordinator failed (returns ``None`` data — HA
    already takes the entity ``unavailable`` on UpdateFailed, so
    OFF only fires after the device recovers then fails again
    with an empty response). Useful for HA automations like
    ``if not device_online → notify`` without polling the camera
    snapshot URL.

    Distinct from per-channel ``channel_{N}_online``: this sensor
    reflects the **device's ISAPI HTTP responder**, while the
    per-channel one reflects **each mounted IPC stream** (e.g. an
    NVR-mounted camera can be online at device level but offline
    at channel level if its RTSP feed drops).
    """

    _attr_translation_key = "device_online"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    def __init__(
        self,
        coordinator: HikvisionISAPICoordinator,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_device_online"
        self._attr_name = "设备在线"

    @property
    def is_on(self) -> bool | None:
        if self.coordinator.data is None:
            return False
        return True


class HikvisionISAPIDevTimeAbnormalBinarySensor(
    HikvisionISAPIEntity, BinarySensorEntity
):
    """Device clock abnormality detector (v0.6.26, renamed v0.6.28).

    ON when the device's reported ``<currentDeviceTime>`` from
    ``/ISAPI/System/status`` is more than 24 hours away from the
    HA host's wall-clock time. The classic trigger is a V4 NVR's
    dead CMOS battery — the device keeps recording and streaming
    normally, but its clock rolls back to 2004-05. Without this
    sensor the user has no way to notice the wrong clock short of
    digging through raw XML, and downstream issues (broken
    automations that key off timestamps, off-by-decades history
    charts) are silently wrong.

    Device class ``PROBLEM`` so HA renders it red on the device
    card when ON and wires it into the standard "this device has a
    problem" notification flow.

    The helper function ``_dev_time_abnormal`` is source-loadable
    in tests so we don't have to mock the whole HA clock stack.

    v0.6.28: renamed from ``device_time_abnormal`` to
    ``dev_time_abnormal`` to match the user's requested field
    name. The unique_id also changes — existing v0.6.26 / v0.6.27
    users will see a new entity (``binary_sensor.<device>_dev_time_abnormal``)
    alongside the old one until they delete the legacy entity
    manually. Both entities show the same value.
    """

    _attr_translation_key = "dev_time_abnormal"
    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    def __init__(
        self,
        coordinator: HikvisionISAPICoordinator,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_dev_time_abnormal"
        self._attr_name = "设备时间异常"

    @property
    def is_on(self) -> bool | None:
        if self.coordinator.data is None:
            return None
        return _dev_time_abnormal(
            self.coordinator.data.system_status.get("currentDeviceTime")
        )

class HikvisionISAPIMemCalibrationWarnBinarySensor(
    HikvisionISAPIEntity, BinarySensorEntity
):
    """Memory unit calibration diagnostic (v0.6.29).

    ON when ``memoryUsage`` and ``memoryAvailable`` from
    ``/System/status`` look like the device's firmware is mixing
    KB and MB units (or some other parser pathology). Pre-v0.6.29
    users had to grep coordinator logs to find such devices; now
    they show up as a red PROBLEM entity on the device card.

    Crucially this is a *diagnostic*, not a fix — see
    ``_memory_calibration_anomalous`` docstring for why we don't
    auto-recalibrate (a second heuristic could itself be wrong on
    a different firmware generation).
    """

    _attr_translation_key = "mem_calibration_warn"
    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    def __init__(
        self,
        coordinator: HikvisionISAPICoordinator,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_mem_calibration_warn"
        self._attr_name = "内存换算疑似异常"

    @property
    def is_on(self) -> bool | None:
        if self.coordinator.data is None:
            return None
        return _memory_calibration_anomalous(
            self.coordinator.data.system_status.get("memoryUsage"),
            self.coordinator.data.system_status.get("memoryAvailable"),
        )
