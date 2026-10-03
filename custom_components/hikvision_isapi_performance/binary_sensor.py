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
from .entity import (
    HikvisionISAPIEntity,
    channel_entities_disabled_by_default,
)

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
    # v0.8: per-disk fault sensors. Built synchronously when the storage
    # endpoint already answered; otherwise the listener below adds them.
    # Returns [] for devices without disks (every IPC).
    entities.extend(_build_per_hdd_binary_entities(coordinator, entry))
    coordinator._hikvision_isapi_performance_hdd_binary_added = bool(  # type: ignore[attr-defined]
        getattr(coordinator, "storage", None)
        and (coordinator.storage or {}).get("hdds")
    )
    # v0.8: alertStream push-event sensors (video_loss / tamper). Empty
    # until the reader task connects and the device actually pushes
    # something, so normally the listener below does the registering.
    event_entities = _build_event_binary_entities(coordinator, entry)
    entities.extend(event_entities)
    coordinator._hikvision_isapi_performance_event_added = [  # type: ignore[attr-defined]
        (e._sensor_key, e._channel_id) for e in event_entities
    ]
    async_add_entities(entities)

    # Per-channel listener for late-arriving channels.
    if not getattr(coordinator, "_hikvision_isapi_performance_binary_added", False):
        coordinator._hikvision_isapi_performance_binary_added = False  # type: ignore[attr-defined]
        coordinator.async_add_listener(
            _make_binary_listener(hass, entry, coordinator, async_add_entities)
        )

    # v0.8: separate listener for late-arriving disks. The channel
    # listener above is one-shot and gated on ``coordinator.channels``,
    # so it can fire (and latch) before the storage endpoint answers —
    # the disk sensors would then never be registered. Keeping a distinct
    # flag avoids that race.
    if not getattr(
        coordinator, "_hikvision_isapi_performance_hdd_binary_added", False
    ):
        coordinator.async_add_listener(
            _make_hdd_binary_listener(coordinator, entry, async_add_entities)
        )

    # v0.8: incremental listener for push-event sensors. Unlike the disk
    # listener this is NOT one-shot: the reader connects asynchronously,
    # and new channels can begin pushing later (a camera that comes back
    # online, or a motion event on a channel that had been silent). It
    # tracks which (sensor_key, channel_id) pairs are already registered
    # and only adds new ones.
    coordinator.async_add_listener(
        _make_event_listener(coordinator, entry, async_add_entities)
    )


def _make_event_listener(
    coordinator: HikvisionISAPICoordinator,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
):
    """Incremental listener: register push-event sensors as channels appear.

    Must stay a plain sync function: HA's ``async_add_listener`` expects
    ``Callable[[], None]`` and calls callbacks synchronously, discarding
    the return value — an ``async def`` here would never execute its body
    (the v0.7.3 dead-listener defect).
    """
    already: set[tuple[str, str]] = set(
        getattr(coordinator, "_hikvision_isapi_performance_event_added", None)
        or []
    )

    def _on_update() -> None:
        event_state = getattr(coordinator, "event_state", None) or {}
        if not event_state:
            return
        # Which (sensor_key, channel) pairs exist now but aren't registered?
        current = {
            (sensor_key, str(channel_id))
            for sensor_key in _EVENT_SENSOR_SPECS
            for channel_id in (event_state.get(sensor_key) or {})
        }
        new_pairs = current - already
        if not new_pairs:
            return
        built = _build_event_binary_entities(coordinator, entry)
        new_entities = [
            e for e in built
            if (e._sensor_key, e._channel_id) in new_pairs
        ]
        if not new_entities:
            return
        already.update(new_pairs)
        coordinator._hikvision_isapi_performance_event_added = list(already)  # type: ignore[attr-defined]
        async_add_entities(new_entities)

    return _on_update


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


def _make_hdd_binary_listener(
    coordinator: HikvisionISAPICoordinator,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
):
    """One-shot listener: register per-disk PROBLEM sensors once disks exist.

    v0.8. Deliberately separate from ``_make_binary_listener``: that one
    is gated on ``coordinator.channels`` and latches on its first
    non-empty result, which can happen before the storage endpoint
    answers. Sharing the flag would mean the disk sensors never get
    registered on a recorder whose channels resolve first.

    No-ops for devices without disks (every IPC — ``storage.hdds`` is
    empty), so they never get a permanently-False fault sensor.

    Must stay a plain sync function: HA calls ``async_add_listener``
    callbacks synchronously and discards the return value, so an
    ``async def`` here would never execute its body (the v0.7.3
    dead-listener defect).
    """

    def _on_update() -> None:
        if getattr(
            coordinator, "_hikvision_isapi_performance_hdd_binary_added", False
        ):
            return
        storage = getattr(coordinator, "storage", None) or {}
        if not storage.get("hdds"):
            return
        new_entities = _build_per_hdd_binary_entities(coordinator, entry)
        if not new_entities:
            return
        coordinator._hikvision_isapi_performance_hdd_binary_added = True  # type: ignore[attr-defined]
        async_add_entities(new_entities)

    return _on_update


def _entities_for_channel(
    coordinator: HikvisionISAPICoordinator,
    entry: ConfigEntry,
    channel: dict[str, Any],
) -> list[BinarySensorEntity]:
    """Build the three binary sensors for one channel.

    v0.8: entities for a channel that is the same physical camera as
    another configured entry (an NVR-side duplicate) are registered
    disabled by default — see ``entity.channel_entities_disabled_by_default``.
    """
    ch_id = channel.get("id", "")
    ch_name = channel.get("name") or f"Channel {ch_id}"
    entities = [
        HikvisionISAPIChannelOnlineBinarySensor(coordinator, entry, ch_id, ch_name),
        HikvisionISAPIChannelRecordingBinarySensor(
            coordinator, entry, ch_id, ch_name
        ),
        HikvisionISAPIChannelMotionBinarySensor(
            coordinator, entry, ch_id, ch_name
        ),
    ]
    if channel_entities_disabled_by_default(coordinator, ch_id):
        for ent in entities:
            ent._attr_entity_registry_enabled_default = False
    return entities


# v0.8: per-disk fault binary sensor.
#
# Healthy states observed on the fleet are ok / normal / idle /
# unformatted (idle = spare or standby bay, still a working disk).
# Anything else — error, reparing, formatting, an unknown value —
# turns the sensor ON. ``notexist`` bays never reach here because
# ``_parse_storage`` drops them.
_HDD_HEALTHY_STATES = frozenset({"ok", "normal", "idle", "unformatted"})


def _build_per_hdd_binary_entities(
    coordinator: HikvisionISAPICoordinator,
    entry: ConfigEntry,
) -> list[BinarySensorEntity]:
    """Build one PROBLEM binary sensor per physical disk.

    Returns an empty list for devices without disks (all IPCs).
    """
    storage = getattr(coordinator, "storage", None) or {}
    hdds = storage.get("hdds") or []
    return [
        HikvisionISAPIHddProblemBinarySensor(
            coordinator, entry,
            str(h.get("id", "")),
            h.get("name") or f"硬盘 {h.get('id', '')}",
        )
        for h in hdds
    ]


# v0.8: alertStream push-event sensors.
#
# sensor_key -> (中文标签, device_class). ``motion`` is deliberately NOT
# here: the polling-based ``channel_{N}_motion`` entity already exists,
# and adding a second push-based motion sensor per channel would be
# exactly the duplicate-entity problem this release set out to remove.
# Instead that existing entity was changed to prefer push data and fall
# back to polling (see HikvisionISAPIChannelMotionBinarySensor.is_on).
_EVENT_SENSOR_SPECS = {
    "video_loss": ("视频丢失", BinarySensorDeviceClass.PROBLEM),
    "tamper": ("画面遮挡", BinarySensorDeviceClass.TAMPER),
}


def _event_channel_name(
    coordinator: HikvisionISAPICoordinator, channel_id: str
) -> str:
    """Best-effort display name for a channel that pushed an event.

    Prefers the ``channelName`` the device sent on alertStream (freshest,
    and the only source for channels the coordinator hasn't listed), then
    the coordinator's channel list, then a generic fallback. Never raises.
    """
    meta = (getattr(coordinator, "event_meta", None) or {}).get(channel_id)
    if meta and meta.get("channel_name"):
        return str(meta["channel_name"])
    for ch in getattr(coordinator, "channels", None) or []:
        if str(ch.get("id", "")) == str(channel_id) and ch.get("name"):
            return str(ch["name"])
    data = getattr(coordinator, "data", None)
    for ch in (getattr(data, "channels", None) or []):
        if str(ch.get("id", "")) == str(channel_id) and ch.get("name"):
            return str(ch["name"])
    return f"通道 {channel_id}"


def _build_event_binary_entities(
    coordinator: HikvisionISAPICoordinator,
    entry: ConfigEntry,
) -> list[BinarySensorEntity]:
    """Build video_loss / tamper sensors for channels that actually pushed.

    Capability-gated on **observed alertStream activity**, not on the
    ``/Event/triggers`` catalog. Real-fleet evidence: the catalog omits
    ``videoloss`` on 176.18 / 176.12 / 176.13 / 176.52, yet alertStream
    delivers videoloss on 10 of 11 devices — gating on the catalog would
    create no video_loss sensor anywhere.

    ``channelID=0`` (device-level events on NVRs 176.65 / 192.168.10.17)
    is kept as its own entity rather than dropped.

    Returns [] when nothing has been pushed, so a device whose stream is
    unreachable (or which pushes nothing) gets no permanently-unknown
    entities.
    """
    event_state = getattr(coordinator, "event_state", None) or {}
    entities: list[BinarySensorEntity] = []
    for sensor_key in _EVENT_SENSOR_SPECS:
        per_channel = event_state.get(sensor_key) or {}
        for channel_id in per_channel:
            name = _event_channel_name(coordinator, channel_id)
            entities.append(
                HikvisionISAPIEventBinarySensor(
                    coordinator, entry, sensor_key, str(channel_id), name,
                )
            )
    return entities


class HikvisionISAPIEventBinarySensor(
    HikvisionISAPIEntity, BinarySensorEntity
):
    """v0.8: push-driven event binary sensor (video_loss / tamper).

    Reads ``coordinator.event_state[sensor_key][channel_id]``, which the
    alertStream reader updates in sub-second time — versus the 30 s poll
    latency of the status-endpoint entities.

    Returns ``None`` (unknown) for a channel that never pushed this event
    type, rather than asserting "no problem".

    The reader calls ``async_update_listeners()`` only on a genuine state
    transition, so a fire-hose device (176.10 pushed 2.25 MB in 8 s) does
    not thrash entity updates.
    """

    def __init__(
        self,
        coordinator: HikvisionISAPICoordinator,
        entry: ConfigEntry,
        sensor_key: str,
        channel_id: str,
        channel_name: str,
    ) -> None:
        super().__init__(coordinator, entry)
        self._sensor_key = sensor_key
        self._channel_id = channel_id
        self._attr_unique_id = (
            f"{entry.entry_id}_channel_{channel_id}_{sensor_key}"
        )
        # Label and device_class come from the spec table so there is a
        # single source of truth (the builder and the entity can't drift).
        label, device_class = _EVENT_SENSOR_SPECS[sensor_key]
        self._attr_name = f"{channel_name} {label}"
        self._attr_device_class = device_class

    @property
    def is_on(self) -> bool | None:
        per_channel = (
            getattr(self.coordinator, "event_state", None) or {}
        ).get(self._sensor_key) or {}
        if self._channel_id not in per_channel:
            return None
        return bool(per_channel[self._channel_id])


class HikvisionISAPIChannelOnlineBinarySensor(
    HikvisionISAPIEntity, BinarySensorEntity
):
    """Per-channel online / reachability binary sensor."""

    # translation_key is deliberately None: in real HA it takes precedence
    # over ``_attr_name``, and every channel shares one key, so all channels
    # would render the same label and the camera names would never surface.
    # (Same conclusion sensor.py reached in v0.7.5 for per-channel sensors.)
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
                # v0.8: tri-state. ``online`` is None when no endpoint
                # reported the field — on every IPC in the fleet the
                # per-channel status endpoint returns a streaming-session
                # list with no <online>, so bool(None) used to render
                # "离线" for cameras that were actively streaming.
                online = ch.get("online")
                if online is None:
                    return None
                return bool(online)
        return None


class HikvisionISAPIChannelRecordingBinarySensor(
    HikvisionISAPIEntity, BinarySensorEntity
):
    """Per-channel recording state binary sensor.

    v0.8: now prefers ``coordinator.recording_status`` (derived from
    ``/ContentMgmt/search`` recording segments) over the channel's own
    ``recording`` field. Reason: on every NVR in the fleet the
    ``recording`` field is permanently ``None`` (InputProxyChannelList
    and the per-channel /status both omit it, and every
    /ContentMgmt/Recording/* path returns 404), so the old entity showed
    "unknown" forever — the user's reported symptom. The segment search
    is the only endpoint that yields real recording data (4/12 fleet
    devices).

    The verdict is **derived**, not device-reported: a segment's planned
    end time is compared against "now", so a channel that stops
    recording stays "on" until its last pre-allocated segment ends
    (detection lag up to one segment length, 17–358 min observed). The
    entity name marks this with "推导" so the user isn't misled, and a
    companion "最近录像时间" timestamp sensor shows how fresh the verdict
    is. Falls back to ``ch.recording`` for IPCs whose status endpoint
    does report recording directly.
    """

    # No translation_key — it would override ``_attr_name`` in real HA and
    # erase both the camera name and the "（推导）" honesty marker below.
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
        # v0.8: "推导" marks this as derived from segment timing, not a
        # device-reported recording flag.
        self._attr_name = f"{channel_name} 录像中（推导）"

    @property
    def is_on(self) -> bool | None:
        if self.coordinator.data is None:
            return None
        # Prefer the derived recording status (segment search).
        rec_status = getattr(self.coordinator, "recording_status", None) or {}
        derived = rec_status.get(self._channel_id)
        if derived is not None and "recording_active" in derived:
            active = derived.get("recording_active")
            if active is not None:
                return bool(active)
        # Fall back to the channel's own recording field (tri-state:
        # None means no endpoint reported it → render "unknown", never
        # coerce to False).
        for ch in self.coordinator.data.channels:
            if ch.get("id") == self._channel_id:
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

    # No translation_key — see the note on the online sensor above.
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
        # v0.8: prefer alertStream push data — it arrives in sub-second
        # time, versus the 30 s poll of the status endpoint. Reading push
        # here (instead of adding a second push-based motion entity) is
        # what keeps one motion sensor per channel rather than two.
        pushed = (
            getattr(self.coordinator, "event_state", None) or {}
        ).get("motion") or {}
        if self._channel_id in pushed:
            return bool(pushed[self._channel_id])
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


class HikvisionISAPIHddProblemBinarySensor(
    HikvisionISAPIEntity, BinarySensorEntity
):
    """v0.8: per-disk fault binary sensor (PROBLEM device class).

    ON when the disk's ``<status>`` is anything other than a healthy
    state. Probed on the fleet: ``ok`` / ``idle`` (spare bay) /
    ``normal`` / ``unformatted`` are all fine; ``error``, ``reparing``,
    ``formatting`` or any unknown value is a fault. Empty bays
    (``notexist``) never reach here — the parser drops them.

    PROBLEM device class so HA renders it red on the device card and
    it works with the built-in "problem" automations.
    """

    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    def __init__(
        self,
        coordinator: HikvisionISAPICoordinator,
        entry: ConfigEntry,
        hdd_id: str,
        hdd_name: str,
    ) -> None:
        super().__init__(coordinator, entry)
        self._hdd_id = hdd_id
        self._attr_unique_id = f"{entry.entry_id}_hdd_{hdd_id}_problem"
        self._attr_name = f"{hdd_name} 故障"

    @property
    def is_on(self) -> bool | None:
        if self.coordinator.data is None:
            return None
        for h in (self.coordinator.data.storage or {}).get("hdds", []):
            if str(h.get("id", "")) == self._hdd_id:
                status = (h.get("status") or "").strip().lower()
                return status not in _HDD_HEALTHY_STATES
        return None

