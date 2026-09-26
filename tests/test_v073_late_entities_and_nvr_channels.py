"""v0.7.3: late-arriving entity registration + NVR channel-id mapping.

Three defects, all found by probing five live devices and by observing
which entities the user actually reported missing. Each test below
reproduces one of them.

DEFECT 1 — coordinator listeners were declared ``async def``.

Home Assistant's ``DataUpdateCoordinator.async_add_listener`` contract is
``Callable[[], None]``. ``async_update_listeners()`` invokes each callback
synchronously and discards the return value, so an ``async def`` callback
produces a coroutine that is never awaited and whose body never runs.

Five of the six registered listeners were async (sensor NIC2, sensor
per-channel, binary_sensor, camera, switch). Every one of them was dead
code. ``_FakeCoordinator`` below deliberately reproduces HA's exact
semantics so this cannot silently pass again.

Consequence: NIC 2 sensors never appeared on the user's dual-NIC NVR, and
per-channel sensors never appeared on any device whose channel list
arrived after platform setup.

DEFECT 2 — storage sensors had no late-arrival listener at all.

``__init__.py`` forwards platforms BEFORE starting the first refresh as a
background task, so ``sensor.async_setup_entry`` runs while
``coordinator.device_type`` is still ``""``. The gate
``is_recorder = device_type in (NVR, DVR)`` is therefore False at setup
time, storage_* descriptors get filtered out, and nothing ever adds them
later. NIC2 and per-channel had listeners; storage did not.

DEFECT 3 — NVR channel ids don't match streaming ids.

On NVRs, ``coordinator.channels`` comes from InputProxyChannelList with
ids ``"1".."8"``, while ``/ISAPI/Streaming/channels`` returns Hikvision's
``{channel}{stream}`` numbering: ``"101"``, ``"102"``, ``"201"``... The
lookup ``ch.get("id") == channel_id`` compared ``"1"`` against ``"101"``
and never matched, so codec / resolution / frame rate / bitrate / audio
showed unknown on every NVR channel.

IPCs are unaffected: their channels come from the same streaming list, so
ids already agree (``"1"``, ``"2"``, ``"3"``).
"""

from __future__ import annotations

from typing import Any

import pytest

import tests.conftest  # noqa: F401  installs homeassistant stubs

from custom_components.hikvision_isapi_performance.const import DOMAIN
from custom_components.hikvision_isapi_performance.coordinator import (
    HikvisionISAPIData,
)
from custom_components.hikvision_isapi_performance import sensor as sensor_mod


class _FakeCoordinator:
    """Faithful stand-in for DataUpdateCoordinator's listener semantics.

    ``async_add_listener`` stores the callback and ``fire()`` invokes it
    the way HA does: synchronously, discarding the return value. An
    ``async def`` callback therefore never executes its body — which is
    exactly the bug these tests must catch. The returned coroutine is
    recorded so a test can assert none was produced.
    """

    def __init__(
        self,
        *,
        device_type: str = "",
        channels: list[dict[str, Any]] | None = None,
        network_interfaces: list[dict[str, Any]] | None = None,
        data: HikvisionISAPIData | None = None,
    ) -> None:
        self.device_type = device_type
        self.channels = channels or []
        self.network_interfaces = network_interfaces or []
        self.device_info: dict[str, Any] = {}
        self.data = data
        self.capabilities: dict[str, Any] = {}
        self.streaming_channel_detail: dict[str, Any] = {}
        self.unique_id = f"{DOMAIN}_test"
        self._listeners: list[Any] = []
        self.unawaited_coroutines: list[Any] = []

    def async_add_listener(self, listener):
        self._listeners.append(listener)

        def _remove():
            if listener in self._listeners:
                self._listeners.remove(listener)

        return _remove

    def fire(self) -> None:
        """Invoke listeners exactly as HA's async_update_listeners does."""
        for listener in list(self._listeners):
            result = listener()
            # HA discards this. A coroutine here means the callback was
            # async and its body never ran.
            if hasattr(result, "__await__") or type(result).__name__ == "coroutine":
                self.unawaited_coroutines.append(result)
                result.close()


def _entry():
    e = type("E", (), {})()
    e.entry_id = "test_entry"
    e.data = {}
    e.options = {}
    return e


def _hass(coordinator):
    h = type("H", (), {})()
    h.data = {DOMAIN: {"test_entry": coordinator}}
    return h


def _dual_nic() -> list[dict[str, Any]]:
    """Real DS-7708N-I4 NIC data (from the 2026-09-26 probe)."""
    return [
        {"id": "1", "ip_address": "10.18.176.65", "subnet_mask": "255.255.255.0",
         "default_gateway": "10.18.176.1", "mac_address": "02:00:00:00:00:0e", "mtu": 1500},
        {"id": "2", "ip_address": "192.168.10.10", "subnet_mask": "255.255.255.0",
         "default_gateway": "192.168.10.1", "mac_address": "02:00:00:00:00:0f", "mtu": 1500},
    ]


# ── DEFECT 1: listeners must be plain sync callables ──────────────────


def test_registered_listeners_are_not_coroutine_functions():
    """No listener may be ``async def`` — HA would never await it."""
    import inspect

    coordinator = _FakeCoordinator(device_type="ipcamera")
    added: list[Any] = []

    with pytest.MonkeyPatch.context() as mp:
        # async_setup_entry is a coroutine; drive it to completion.
        import asyncio
        asyncio.run(sensor_mod.async_setup_entry(
            _hass(coordinator), _entry(), added.append if False else added.extend,
        ))

    assert coordinator._listeners, "sensor platform should register listeners"
    offenders = [
        getattr(fn, "__name__", repr(fn))
        for fn in coordinator._listeners
        if inspect.iscoroutinefunction(fn)
    ]
    assert not offenders, (
        f"these listeners are async def and HA will never run their body: {offenders}"
    )


@pytest.mark.asyncio
async def test_nic2_listener_registers_sensors_when_interfaces_arrive_late():
    """Dual-NIC NVR: NIC 2 sensors appear once the refresh populates them."""
    coordinator = _FakeCoordinator(device_type="networkvideorecorder")
    added: list[Any] = []

    await sensor_mod.async_setup_entry(_hass(coordinator), _entry(), added.extend)

    assert not any(
        e.entity_description.key.startswith("network_2_") for e in added
    ), "no NIC data yet, so NIC 2 sensors must not be registered"
    assert not coordinator.unawaited_coroutines

    # First refresh lands: two NICs now known.
    coordinator.network_interfaces = _dual_nic()
    coordinator.fire()

    assert not coordinator.unawaited_coroutines, (
        "listener returned a coroutine — its body never executed"
    )
    nic2 = [e for e in added if e.entity_description.key.startswith("network_2_")]
    assert nic2, "NIC 2 sensors should be registered after interfaces arrive"
    keys = {e.entity_description.key for e in nic2}
    assert {"network_2_ip", "network_2_subnet", "network_2_gateway",
            "network_2_mac", "network_2_mtu"} <= keys


# ── DEFECT 2: storage sensors need a late-arrival listener ────────────


@pytest.mark.asyncio
async def test_storage_sensors_registered_after_device_type_arrives_late():
    """NVR/DVR storage sensors must appear once device_type is known.

    Platform setup runs before the first refresh, so device_type is ""
    and ``is_recorder`` is False. Without a listener these four entities
    never exist — the user's reported "存储没有" symptom.
    """
    coordinator = _FakeCoordinator(device_type="")
    added: list[Any] = []

    await sensor_mod.async_setup_entry(_hass(coordinator), _entry(), added.extend)

    assert not any(
        e.entity_description.key.startswith("storage_") for e in added
    ), "device_type unknown at setup, so storage must not be registered yet"

    # First refresh: it's a DVR with a real disk.
    coordinator.device_type = "dvr"
    data = HikvisionISAPIData(
        device_info={"deviceType": "DVR", "firmwareVersion": "V4.1.18"},
        system_status={},
        channels=[],
        capabilities={},
        storage={"total_mb": 953869, "used_mb": 953869, "free_mb": 0, "status": "normal"},
        network_interfaces=[],
    )
    coordinator.data = data
    coordinator.device_info = data.device_info
    coordinator.fire()

    assert not coordinator.unawaited_coroutines
    storage = [e for e in added if e.entity_description.key.startswith("storage_")]
    assert storage, "storage sensors should be registered once device_type is known"
    keys = {e.entity_description.key for e in storage}
    assert {"storage_total_gb", "storage_used_gb", "storage_free_gb",
            "storage_usage_percent"} <= keys


@pytest.mark.asyncio
async def test_storage_sensors_stay_off_for_ipc():
    """The fix must not add storage sensors to cameras that have no HDD."""
    coordinator = _FakeCoordinator(device_type="")
    added: list[Any] = []

    await sensor_mod.async_setup_entry(_hass(coordinator), _entry(), added.extend)

    coordinator.device_type = "ipcamera"
    coordinator.data = HikvisionISAPIData(
        device_info={"deviceType": "IPCamera"}, system_status={},
        channels=[], capabilities={}, storage={}, network_interfaces=[],
    )
    coordinator.fire()

    assert not any(
        e.entity_description.key.startswith("storage_") for e in added
    ), "IPC has no HDD; storage sensors must not be registered"


# ── DEFECT 3: NVR channel id → streaming id mapping ───────────────────


def _nvr_streaming_detail() -> dict[str, Any]:
    """Real DS-8632N-I8 ``_parse_streaming_detail`` output.

    Includes the TOP-LEVEL fields the real parser mirrors from the first
    channel. Omitting them would make these tests unfaithful: in
    production the legacy ``first_channel`` fallback silently covers
    channel "1", so the visible defect is channels 2+.
    """
    return {
        "first_channel_id": "101",
        # top-level mirror of channel 101, exactly as the parser emits
        "video_codec": "H.264",
        "video_resolution": "3840x2160",
        "video_frame_rate": 25.0,
        "video_bitrate_kbps": 16384,
        "audio_codec": "G.711alaw",
        "channels": [
            {"id": "101", "video_codec": "H.264", "video_resolution": "3840x2160",
             "video_frame_rate": 25.0, "video_bitrate_kbps": 16384,
             "audio_codec": "G.711alaw"},
            {"id": "102", "video_codec": "H.264", "video_resolution": "704x576",
             "video_frame_rate": 25.0, "video_bitrate_kbps": 512,
             "audio_codec": "G.711alaw"},
            {"id": "201", "video_codec": "H.265", "video_resolution": "1920x1080",
             "video_frame_rate": 25.0, "video_bitrate_kbps": 4096,
             "audio_codec": "G.711alaw"},
        ],
    }


def _data_with(detail: dict[str, Any]) -> HikvisionISAPIData:
    return HikvisionISAPIData(
        device_info={"deviceType": "NVR"}, system_status={}, channels=[],
        capabilities={}, storage={}, network_interfaces=[],
        streaming_channel_detail=detail,
    )


def test_nvr_channel_two_maps_to_streaming_channel_201():
    """THE defect: NVR channel "2" must resolve to streaming id "201".

    Pre-fix this returned None. The lookup was ``ch["id"] == channel_id``,
    comparing InputProxy id ``"2"`` against Hikvision's ``{channel}{stream}``
    ids ``"101"/"102"/"201"``, and the legacy fallback only ever covers
    channel 1 — so every NVR channel except the first showed unknown for
    codec / resolution / frame rate / bitrate / audio.
    """
    data = _data_with(_nvr_streaming_detail())
    assert sensor_mod._channel_streaming_field(
        "2", "video_resolution")(data) == "1920x1080"
    assert sensor_mod._channel_streaming_field("2", "video_codec")(data) == "H.265"


def test_nvr_channel_three_maps_to_streaming_channel_301_absent_returns_none():
    """Channel 3 has no "301" entry here, so it must be None — not channel 1's."""
    data = _data_with(_nvr_streaming_detail())
    assert sensor_mod._channel_streaming_field("3", "video_resolution")(data) is None


def test_nvr_channel_one_reads_main_stream_not_sub_stream():
    """Regression guard: channel "1" must take MAIN stream 101, never 102.

    Channel 1 already worked pre-fix via the legacy first_channel
    fallback; this pins that the mapping fix keeps it correct and does
    not start matching sub-stream 102 (704x576).
    """
    data = _data_with(_nvr_streaming_detail())
    assert sensor_mod._channel_streaming_field(
        "1", "video_resolution")(data) == "3840x2160"
    assert sensor_mod._channel_streaming_field(
        "1", "video_bitrate_kbps")(data) == 16384


def test_ipc_streaming_ids_still_match_directly():
    """IPC ids ("1","2","3") must keep working — no regression.

    Real DS-FB2127 data: channels and streaming detail share the same
    ids, so the direct match must still win over the ×100 convention.
    """
    detail = {
        "first_channel_id": "1",
        "channels": [
            {"id": "1", "video_codec": "H.264", "video_resolution": "1920x1080",
             "video_bitrate_kbps": 2048},
            {"id": "2", "video_codec": "H.264", "video_resolution": "704x480",
             "video_bitrate_kbps": 1024},
        ],
    }
    data = HikvisionISAPIData(
        device_info={}, system_status={}, channels=[], capabilities={},
        storage={}, network_interfaces=[], streaming_channel_detail=detail,
    )
    fn = sensor_mod._channel_streaming_field("2", "video_resolution")
    assert fn(data) == "704x480"


def test_direct_match_takes_precedence_over_101_convention():
    """If both "1" and "101" exist, the exact match wins."""
    detail = {
        "first_channel_id": "1",
        "channels": [
            {"id": "1", "video_codec": "H.265"},
            {"id": "101", "video_codec": "H.264"},
        ],
    }
    data = HikvisionISAPIData(
        device_info={}, system_status={}, channels=[], capabilities={},
        storage={}, network_interfaces=[], streaming_channel_detail=detail,
    )
    fn = sensor_mod._channel_streaming_field("1", "video_codec")
    assert fn(data) == "H.265"


def test_missing_streaming_channel_returns_none_not_crash():
    """A channel with no streaming entry yields None, not an exception."""
    data = HikvisionISAPIData(
        device_info={}, system_status={}, channels=[], capabilities={},
        storage={}, network_interfaces=[],
        streaming_channel_detail={"first_channel_id": "101", "channels": []},
    )
    fn = sensor_mod._channel_streaming_field("7", "video_codec")
    assert fn(data) is None
